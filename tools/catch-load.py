#!/usr/bin/env python3
"""Sample the audio chain across a plugin-load transient.

Two rates, because the interesting quantities have different costs:

  fast (default 20ms)  RT threads only (policy FIFO/RR, rtprio >= RT_MIN) plus
                       global counters. ~25 threads x 2 files, cheap enough to
                       run at 50Hz without perturbing what it watches.
  census (default 250ms) every thread of every matched process, with policy,
                       rtprio and Cpus_allowed_list. This is what shows the
                       steer-threads.sh sweep window: threads spawned during a
                       plugin load inherit the P-core mask and keep it until the
                       next sweep.

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
    ap.add_argument("--fast", type=float, default=0.020, help="fast interval s")
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
        "fast": a.fast, "census": a.census, "rt_min": a.rt_min,
    }) + "\n")

    rts = rt_threads(a.rt_min)
    prev_ss = {}
    prev_cpu = cpustat()
    prev_psi = pressure()
    prev_vm = vmstat()
    prev_irq = audio_irq_count()
    prev_mono = mono0

    next_fast = mono0
    next_census = mono0
    next_rescan = mono0 + a.rescan
    end = mono0 + a.dur
    slips = 0

    while True:
        now = time.monotonic()
        if now >= end:
            break

        if now >= next_rescan:
            rts = rt_threads(a.rt_min)
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
        sleep = next_fast - time.monotonic()
        if sleep > 0:
            time.sleep(sleep)
        else:
            next_fast = time.monotonic()

    fh.write(json.dumps({"t": "end", "ts": time.monotonic() - mono0,
                         "slips": slips, "wall_end": time.time()}) + "\n")
    fh.close()
    print("wrote %s (%d observer slips)" % (a.out, slips), file=sys.stderr)


if __name__ == "__main__":
    main()
