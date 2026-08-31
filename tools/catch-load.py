#!/usr/bin/env python3
"""Sample the audio chain across a plugin-load transient.

Three rates, because the interesting quantities have very different costs:

  fine (default 2ms)   Only the few threads matching --track: schedstat plus
                       /proc/<tid>/sched. 4 threads x 2 files at 500Hz measured
                       0.8% of one core.

                       This rate exists to catch *block* time. schedstat accounts
                       for on-CPU time and runqueue-wait time only -- a thread
                       blocked on a futex is in neither, and Bitwig's reported
                       "Load" is wall-clock per callback. What does show a block is
                       nr_voluntary_switches: an audio callback normally sleeps once
                       per period, so one that also waits on the plug-in host sleeps
                       twice. At 2ms against a 5.333ms period most windows hold 0 or
                       1 switches, so the extra one is unambiguous; at 20ms it is
                       4.75 against an expected 3.75 and much harder to trust.

                       Note sum_sleep_runtime (present once
                       kernel.sched_schedstats=1) does NOT isolate this: the callback
                       sleeps ~19.5 of every 20ms between periods anyway, so the
                       inter-period sleep swamps a 1.4ms block. It is recorded for
                       completeness, along with iowait_sum, which does cleanly rule
                       IO in or out.

  fast (default 20ms)  Every RT thread (policy FIFO/RR, rtprio >= RT_MIN) plus the
                       global counters -- PSI, vmstat, per-CPU /proc/stat, IRQ 16.

  census (default 250ms) every thread of every matched process, with policy,
                       rtprio and Cpus_allowed_list. This is what shows the
                       steer-threads.sh sweep window: threads spawned during a
                       plugin load inherit the mask of whatever forked them and keep
                       it until the next sweep.

Writes JSONL to --out. Analyse with summarize-load.py; nothing is interpreted here.

Usage: chrt -f 10 taskset -c 16-31 python3 catch-load.py --dur 90 --out run.jsonl
"""

import argparse
import json
import os
import re
import sys
import time

PROC_PATTERNS = re.compile(
    r"^(BitwigStudio|BitwigAudioEngi|BitwigPluginHos|bitwig-studio|yabridge-host\.e"
    r"|wineserver|services\.exe|winedevice\.exe|plugplay\.exe|svchost\.exe|rpcss\.exe"
    r"|explorer\.exe|NIHardwareServi|NIHostIntegrati|start\.exe|conhost\.exe"
    r"|pipewire|wireplumber|pipewire-pulse|irq/16-snd_hdspe)$"
)

# The threads whose wall-clock behaviour we care about. Deliberately tiny: the
# fine rate reads /proc/<tid>/sched for each of these on every tick. Matched
# against "<process comm>/<thread comm>".
TRACK_DEFAULT = (
    r"^(BitwigAudioEngi/data-loop\.0"          # the Bitwig audio callback itself
    r"|BitwigPluginHos/bitwig-remote-p"        # the sandbox handoff
    r"|yabridge-host\.e/audio-\d+"             # the Wine side of the handoff
    r"|pipewire/data-loop\.0)$"                # the graph driver, for reference
)

# Suffixes to pull out of /proc/<tid>/sched. The prefix differs between kernel
# versions ("se.statistics.*" on older, "stats.*" on newer), so match the tail.
# Everything except nr_*_switches appears only when kernel.sched_schedstats=1.
#
# The time fields are printed as floats in *milliseconds*, so they are scaled to
# integer nanoseconds here. Truncating them to whole milliseconds instead throws
# away exactly the resolution this rate exists to measure -- a 1.4ms block in a
# 2ms window would round to noise.
SCHED_COUNTERS = (
    "nr_switches", "nr_voluntary_switches", "nr_involuntary_switches",
    "wait_count", "iowait_count",
)
# Cumulative times: sampled as deltas.
SCHED_TIMES = (
    "sum_sleep_runtime", "sum_block_runtime", "wait_sum", "iowait_sum",
)
# Lifetime maxima: a delta is meaningless, so the absolute value is recorded and
# the summarizer watches for it increasing.
SCHED_MAXES = ("sleep_max", "block_max", "wait_max")

SCHED_KEYS = SCHED_COUNTERS + SCHED_TIMES + SCHED_MAXES

VMSTAT_KEYS = (
    "pgfault", "pgmajfault", "pgpgin", "pgpgout", "pswpin", "pswpout",
    "allocstall_normal", "allocstall_movable", "compact_stall",
    "pgscan_direct", "pgsteal_direct", "pgscan_kswapd", "numa_pages_migrated",
)


def read(path):
    """Read a /proc file, or None. No forks anywhere in this program."""
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def sched_of(taskdir):
    """(policy, rtprio) from /proc/<tid>/stat.

    comm sits in parens and may contain spaces and parens, so strip through the
    last ') ' before counting fields -- after that policy is index 38 and
    rt_priority index 37. Same indices tools/steer-threads.sh uses.
    """
    st = read(taskdir + "/stat")
    if not st:
        return None
    f = st[st.rfind(") ") + 2:].split()
    try:
        return int(f[38]), int(f[37])
    except (IndexError, ValueError):
        return None


def schedstat_of(taskdir):
    """(on_cpu_ns, runqueue_wait_ns, timeslices)."""
    s = read(taskdir + "/schedstat")
    if not s:
        return None
    f = s.split()
    try:
        return int(f[0]), int(f[1]), int(f[2])
    except (IndexError, ValueError):
        return None


def sched_detail_of(taskdir):
    """The SCHED_KEYS fields from /proc/<tid>/sched.

    Counters come back as ints; times and maxima as integer nanoseconds (the file
    prints them as float milliseconds).
    """
    s = read(taskdir + "/sched")
    if not s:
        return None
    out = {}
    for line in s.splitlines():
        k, _, v = line.partition(":")
        k = k.strip()
        for want in SCHED_KEYS:
            if k == want or k.endswith("." + want):
                try:
                    f = float(v.strip())
                except ValueError:
                    break
                out[want] = int(f) if want in SCHED_COUNTERS else int(f * 1e6)
                break
    return out


def tracked_threads(track_re):
    """[(tid, comm, taskdir, pcomm)] for threads matching --track."""
    found = []
    for pid, pcomm in matched_pids():
        taskroot = "/proc/%s/task" % pid
        try:
            tids = os.listdir(taskroot)
        except OSError:
            continue
        for tid in tids:
            td = taskroot + "/" + tid
            comm = read(td + "/comm")
            if not comm:
                continue
            comm = comm.strip()
            if track_re.match("%s/%s" % (pcomm, comm)):
                found.append((tid, comm, td, pcomm))
    return found


def mask_of(taskdir):
    s = read(taskdir + "/status")
    if not s:
        return None
    for line in s.splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return line.split(None, 1)[1].strip()
    return None


def matched_pids():
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        comm = read("/proc/" + name + "/comm")
        if comm and PROC_PATTERNS.match(comm.strip()):
            out.append((name, comm.strip()))
    return out


def rt_threads(rt_min):
    """[(tid, comm, taskdir)] for FIFO/RR threads at or above rt_min."""
    found = []
    for pid, pcomm in matched_pids():
        taskroot = "/proc/%s/task" % pid
        try:
            tids = os.listdir(taskroot)
        except OSError:
            continue
        for tid in tids:
            td = taskroot + "/" + tid
            sch = sched_of(td)
            if not sch:
                continue
            pol, prio = sch
            if pol in (1, 2) and prio >= rt_min:
                comm = read(td + "/comm")
                found.append((tid, (comm or "?").strip(), td, pcomm, prio))
    return found


def cpufreq(cores):
    """scaling_cur_freq in MHz. Each read is an MSR access, so census rate only.

    The session sets the 'performance' governor, but intel_pstate is in HWP mode
    with hwp_dynamic_boost=0 -- the hardware can still drop the clock, and a
    P-core coming back up from 800 MHz mid-callback looks exactly like a spike.
    """
    out = {}
    for c in cores:
        s = read("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % c)
        if s:
            try:
                out[c] = int(s.strip()) // 1000
            except ValueError:
                pass
    return out


def census(rt_min):
    rows = []
    for pid, pcomm in matched_pids():
        taskroot = "/proc/%s/task" % pid
        try:
            tids = os.listdir(taskroot)
        except OSError:
            continue
        for tid in tids:
            td = taskroot + "/" + tid
            sch = sched_of(td)
            if not sch:
                continue
            pol, prio = sch
            comm = read(td + "/comm")
            rows.append({
                "pid": int(pid), "tid": int(tid),
                "p": pcomm, "c": (comm or "?").strip(),
                "pol": pol, "prio": prio,
                "mask": mask_of(td),
                "rt": pol in (1, 2) and prio >= rt_min,
            })
    return rows


def pressure():
    out = {}
    for kind in ("cpu", "io", "memory"):
        s = read("/proc/pressure/" + kind)
        if not s:
            continue
        for line in s.splitlines():
            parts = line.split()
            for p in parts[1:]:
                if p.startswith("total="):
                    out["%s_%s" % (kind, parts[0])] = int(p[6:])
    return out


def vmstat():
    s = read("/proc/vmstat") or ""
    want = set(VMSTAT_KEYS)
    out = {}
    for line in s.splitlines():
        k, _, v = line.partition(" ")
        if k in want:
            out[k] = int(v)
    return out


def cpustat():
    """Per-CPU jiffy totals: {cpu_index: (busy, total)}."""
    s = read("/proc/stat") or ""
    out = {}
    for line in s.splitlines():
        if not line.startswith("cpu") or line[3] == " ":
            continue
        f = line.split()
        n = int(f[0][3:])
        v = [int(x) for x in f[1:]]
        idle = v[3] + v[4]          # idle + iowait
        total = sum(v[:8])
        out[n] = (total - idle, total)
    return out


def audio_irq_count(irq="16"):
    s = read("/proc/interrupts") or ""
    for line in s.splitlines():
        f = line.split()
        if f and f[0] == irq + ":":
            tot = 0
            for x in f[1:]:
                if x.isdigit():
                    tot += int(x)
                else:
                    break
            return tot
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=90.0, help="seconds")
    ap.add_argument("--fine", type=float, default=0.002,
                    help="fine interval s -- tracked threads only, catches block time")
    ap.add_argument("--fast", type=float, default=0.020, help="fast interval s")
    ap.add_argument("--track", default=TRACK_DEFAULT,
                    help="regex on '<process comm>/<thread comm>' for the fine rate")
    ap.add_argument("--census", type=float, default=0.250, help="census interval s")
    ap.add_argument("--rt-min", type=int, default=50)
    ap.add_argument("--out", required=True)
    ap.add_argument("--rescan", type=float, default=1.0,
                    help="how often to re-discover RT threads (plugins spawn them)")
    a = ap.parse_args()

    fh = open(a.out, "w", buffering=1 << 20)

    t0 = time.time()
    mono0 = time.monotonic()
    fh.write(json.dumps({
        "t": "meta", "wall_start": t0, "argv": sys.argv,
        "fine": a.fine, "fast": a.fast, "census": a.census, "rt_min": a.rt_min,
        "track": a.track,
    }) + "\n")

    track_re = re.compile(a.track)
    rts = rt_threads(a.rt_min)
    trk = tracked_threads(track_re)
    prev_ss = {}
    prev_fine = {}
    prev_cpu = cpustat()
    prev_psi = pressure()
    prev_vm = vmstat()
    prev_irq = audio_irq_count()
    prev_mono = mono0

    next_fine = mono0
    next_fast = mono0
    next_census = mono0
    next_rescan = mono0 + a.rescan
    end = mono0 + a.dur
    slips = 0
    fine_slips = 0
    prev_fine_mono = mono0

    while True:
        now = time.monotonic()
        if now >= end:
            break

        if now >= next_rescan:
            rts = rt_threads(a.rt_min)
            trk = tracked_threads(track_re)
            next_rescan = now + a.rescan

        if now >= next_census:
            fh.write(json.dumps({
                "t": "census", "ts": now - mono0, "rows": census(a.rt_min),
                "mhz": cpufreq(range(0, 16)),
                "cstate": {
                    s: read("/sys/devices/system/cpu/cpu0/cpuidle/state%d/disable" % s).strip()
                    for s in (2, 3)
                    if read("/sys/devices/system/cpu/cpu0/cpuidle/state%d/disable" % s)
                },
                "gov": (read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "?").strip(),
            }) + "\n")
            next_census = now + a.census

        # --- fine sample: tracked threads only -------------------------------
        # Every tick. This is the block-time instrument; see the module docstring
        # for why nr_voluntary_switches and not sum_sleep_runtime.
        fdt = now - prev_fine_mono
        frows = []
        for tid, comm, td, pcomm in trk:
            ss = schedstat_of(td)
            sd = sched_detail_of(td)
            if not ss or not sd:
                continue
            key = tid + ":" + comm
            p = prev_fine.get(key)
            prev_fine[key] = (ss, sd)
            if p is None:
                continue
            pss, psd = p
            drun, dwait = ss[0] - pss[0], ss[1] - pss[1]
            if drun < 0 or dwait < 0:          # tid reused
                continue
            dvol = sd.get("nr_voluntary_switches", 0) - psd.get("nr_voluntary_switches", 0)
            dinv = sd.get("nr_involuntary_switches", 0) - psd.get("nr_involuntary_switches", 0)
            extra = {}
            for k in SCHED_TIMES:
                if k in sd and k in psd:
                    d = sd[k] - psd[k]
                    if d:
                        extra[k] = d
            for k in SCHED_MAXES:
                # Absolute, not a delta: these are lifetime maxima. Only record a
                # change, so the summarizer sees exactly when a new max was set.
                if k in sd and sd.get(k) != psd.get(k):
                    extra[k] = sd[k]
            if not (drun or dwait or dvol or dinv or extra):
                continue
            frows.append([tid, comm, pcomm, drun, dwait, dvol, dinv, extra])

        if frows:
            fslip = fdt > a.fine * 3
            if fslip:
                fine_slips += 1
            fh.write(json.dumps({
                "t": "fine", "ts": now - mono0, "dt": round(fdt, 6),
                "slip": fslip, "th": frows,
            }) + "\n")
        prev_fine_mono = now

        if now < next_fast:
            next_fine += a.fine
            sleep = next_fine - time.monotonic()
            if sleep > 0:
                time.sleep(sleep)
            else:
                next_fine = time.monotonic()
            continue

        # --- fast sample -----------------------------------------------------
        dt = now - prev_mono
        threads = []
        for tid, comm, td, pcomm, prio in rts:
            ss = schedstat_of(td)
            if not ss:
                continue
            key = tid + ":" + comm
            p = prev_ss.get(key)
            prev_ss[key] = ss
            if p is None:
                continue
            drun, dwait = ss[0] - p[0], ss[1] - p[1]
            dsl = ss[2] - p[2]
            if drun < 0 or dwait < 0:      # tid reused
                continue
            if drun == 0 and dwait == 0:
                continue
            threads.append([tid, comm, pcomm, prio, drun, dwait, dsl])

        cpu = cpustat()
        percpu = {}
        for n, (b, t) in cpu.items():
            pb, pt = prev_cpu.get(n, (b, t))
            dtot = t - pt
            if dtot > 0:
                percpu[n] = round((b - pb) / dtot, 4)
        prev_cpu = cpu

        psi = pressure()
        dpsi = {k: psi[k] - prev_psi.get(k, psi[k]) for k in psi}
        prev_psi = psi

        vm = vmstat()
        dvm = {k: v - prev_vm.get(k, v) for k, v in vm.items() if v - prev_vm.get(k, v)}
        prev_vm = vm

        irq = audio_irq_count()
        dirq = (irq - prev_irq) if (irq is not None and prev_irq is not None) else None
        prev_irq = irq

        # The observer itself can be preempted; record it so the summarizer can
        # discard windows where our own sampling slipped.
        slipped = dt > a.fast * 3
        if slipped:
            slips += 1

        fh.write(json.dumps({
            "t": "fast", "ts": now - mono0, "dt": round(dt, 6),
            "slip": slipped, "th": threads, "cpu": percpu,
            "psi": dpsi, "vm": dvm, "irq": dirq,
        }) + "\n")

        prev_mono = now
        next_fast += a.fast
        if next_fast <= now:               # fell behind; resync rather than spin
            next_fast = now + a.fast
        next_fine += a.fine
        sleep = next_fine - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_fine = time.monotonic()

    fh.write(json.dumps({"t": "end", "ts": time.monotonic() - mono0,
                         "slips": slips, "fine_slips": fine_slips,
                         "wall_end": time.time()}) + "\n")
    fh.close()
    print("wrote %s (%d fast slips, %d fine slips)" % (a.out, slips, fine_slips),
          file=sys.stderr)


if __name__ == "__main__":
    main()
