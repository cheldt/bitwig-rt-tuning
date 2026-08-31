#!/usr/bin/env python3
"""Turn catch-load.py JSONL into the four things that decide the diagnosis.

  1. Non-RT threads sitting on the P-cores over time -- the steer-threads.sh
     sweep window. A plugin load spawns threads that inherit the P-core mask.
  2. Runqueue WAIT per RT thread -- an RT audio thread that is runnable but not
     running is a placement/inversion problem, not a throughput one.
  3. RUN bursts per thread -- who was actually on CPU during the spike.
  4. PSI / vmstat / P-core busy -- CPU contention vs IO stall vs reclaim.
  5. Block time, from the fine rate -- voluntary switches per period. schedstat
     cannot see a thread blocked on a futex; an extra voluntary switch can.

Usage: python3 summarize-load.py run.jsonl [--pcores 0-15] [--top 20]
       python3 summarize-load.py run.jsonl --window 12.0 18.0
"""

import argparse
import json
from collections import defaultdict


def parse_cpuset(spec):
    out = set()
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            out.update(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return out


def mask_hits(mask, cores):
    if not mask:
        return False
    return bool(parse_cpuset(mask) & cores)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jsonl")
    ap.add_argument("--pcores", default="0-15")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--window", nargs=2, type=float, default=None,
                    help="restrict RUN/WAIT leaderboards to [start end] seconds")
    ap.add_argument("--wait-thresh-ms", type=float, default=0.5)
    ap.add_argument("--period-ms", type=float, default=5.333,
                    help="audio period, for the expected-switches-per-period baseline")
    ap.add_argument("--track-comm", default="data-loop.0",
                    help="thread comm to report block time for")
    ap.add_argument("--track-proc", default="BitwigAudioEngi",
                    help="process comm to report block time for")
    ap.add_argument("--run-thresh-ms", type=float, default=2.0)
    a = ap.parse_args()

    pcores = parse_cpuset(a.pcores)
    meta = None
    fasts, censuses, fines = [], [], []

    with open(a.jsonl) as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r["t"] == "meta":
                meta = r
            elif r["t"] == "fast":
                fasts.append(r)
            elif r["t"] == "fine":
                fines.append(r)
            elif r["t"] == "census":
                censuses.append(r)
            elif r["t"] == "end":
                meta = meta or {}
                meta["slips"] = r.get("slips")
                meta["fine_slips"] = r.get("fine_slips")
                meta["wall_end"] = r.get("wall_end")

    if not fasts:
        print("no samples")
        return

    wall0 = (meta or {}).get("wall_start", 0)
    print("=" * 78)
    print("wall_start %.3f   fast %d   fine %d   census %d   slips fast=%s fine=%s"
          % (wall0, len(fasts), len(fines), len(censuses),
             (meta or {}).get("slips"), (meta or {}).get("fine_slips")))
    print("  (add wall_start to any ts below to correlate with engine.log)")

    # --- 1. P-core occupancy by non-RT threads --------------------------------
    print()
    print("=" * 78)
    print("1. NON-RT THREADS ON P-CORES (%s) -- steer window signature" % a.pcores)
    print("%8s %6s %6s %6s  %s" % ("ts", "nonRT", "RT", "total", "worst offenders (comm xN)"))
    prev_nonrt = None
    for c in censuses:
        rows = c["rows"]
        nonrt_on_p = [r for r in rows if not r["rt"] and mask_hits(r["mask"], pcores)]
        rt_on_p = [r for r in rows if r["rt"] and mask_hits(r["mask"], pcores)]
        n = len(nonrt_on_p)
        # print on change of >=5 or every ~2s
        show = prev_nonrt is None or abs(n - prev_nonrt) >= 5
        if show:
            byc = defaultdict(int)
            for r in nonrt_on_p:
                byc[r["c"]] += 1
            top = ", ".join("%s x%d" % (k, v) for k, v in
                            sorted(byc.items(), key=lambda kv: -kv[1])[:5])
            print("%8.2f %6d %6d %6d  %s" % (c["ts"], n, len(rt_on_p), len(rows), top))
            prev_nonrt = n

    # --- 1b. governor / C-states / P-core clocks -------------------------------
    print()
    print("=" * 78)
    print("1b. GOVERNOR, C-STATES, P-CORE CLOCKS (min/median/max MHz over cores 0-15)")
    print("%8s %-12s %-14s %7s %7s %7s" % ("ts", "gov", "C2/C3 disabled", "minMHz", "medMHz", "maxMHz"))
    prev_sig = None
    for c in censuses:
        mhz = sorted(c.get("mhz", {}).values())
        cs = c.get("cstate", {})
        sig = (c.get("gov"), cs.get("2"), cs.get("3"))
        low = mhz[0] if mhz else 0
        # print on state change, or whenever a P-core is below 2 GHz
        if sig != prev_sig:
            print("%8.2f %-12s %-14s %7d %7d %7d"
                  % (c["ts"], c.get("gov", "?"),
                     "%s/%s" % (cs.get("2", "?"), cs.get("3", "?")),
                     low, mhz[len(mhz) // 2] if mhz else 0, mhz[-1] if mhz else 0))
            prev_sig = sig

    # --- 2. runqueue WAIT bursts ----------------------------------------------
    lo, hi = (a.window if a.window else (-1e18, 1e18))
    print()
    print("=" * 78)
    print("2. RT THREAD RUNQUEUE WAIT > %.2f ms in one %d ms window"
          % (a.wait_thresh_ms, int((meta or {}).get("fast", 0.02) * 1000)))
    print("%8s %7s %7s %5s  %-18s %s" % ("ts", "wait_ms", "run_ms", "prio", "comm", "process"))
    wait_tot = defaultdict(float)
    run_tot = defaultdict(float)
    nwait = 0
    for r in fasts:
        if r["slip"]:
            continue
        for tid, comm, pcomm, prio, drun, dwait, dsl in r["th"]:
            key = "%-18s %s" % (comm, pcomm)
            if lo <= r["ts"] <= hi:
                wait_tot[key] += dwait / 1e6
                run_tot[key] += drun / 1e6
            if dwait / 1e6 > a.wait_thresh_ms:
                nwait += 1
                if nwait <= 60:
                    print("%8.2f %7.3f %7.3f %5d  %s"
                          % (r["ts"], dwait / 1e6, drun / 1e6, prio, key))
    if nwait > 60:
        print("  ... %d more" % (nwait - 60))
    if nwait == 0:
        print("  none -- no RT thread was runnable-but-unscheduled above threshold")

    # --- 3. RUN bursts --------------------------------------------------------
    print()
    print("=" * 78)
    print("3. RT THREAD ON-CPU > %.2f ms in one window (a burst is a long callback)"
          % a.run_thresh_ms)
    print("%8s %7s %7s %5s  %-18s %s" % ("ts", "run_ms", "wait_ms", "prio", "comm", "process"))
    nrun = 0
    for r in fasts:
        if r["slip"]:
            continue
        for tid, comm, pcomm, prio, drun, dwait, dsl in r["th"]:
            if drun / 1e6 > a.run_thresh_ms:
                nrun += 1
                if nrun <= 60:
                    print("%8.2f %7.3f %7.3f %5d  %-18s %s"
                          % (r["ts"], drun / 1e6, dwait / 1e6, prio, comm, pcomm))
    if nrun > 60:
        print("  ... %d more" % (nrun - 60))
    if nrun == 0:
        print("  none")

    print()
    print("-" * 78)
    print("totals over %s (ms): top %d by WAIT" %
          ("window %.1f-%.1f" % (lo, hi) if a.window else "whole run", a.top))
    for k, v in sorted(wait_tot.items(), key=lambda kv: -kv[1])[:a.top]:
        print("  %9.2f wait  %9.2f run   %s" % (v, run_tot[k], k))
    print("top %d by RUN" % a.top)
    for k, v in sorted(run_tot.items(), key=lambda kv: -kv[1])[:a.top]:
        print("  %9.2f run   %9.2f wait  %s" % (v, wait_tot[k], k))

    # --- 4. system-level: PSI, vmstat, P-core busy ----------------------------
    print()
    print("=" * 78)
    print("4. SYSTEM PRESSURE / FAULTS / P-CORE BUSY (windows above threshold)")
    print("%8s %8s %8s %8s %8s %9s %9s %6s %7s"
          % ("ts", "psi_cpu", "psi_io", "psi_mem", "pcoreBsy", "pgfault", "pgmajflt", "irq16", "dt_ms"))
    shown = 0
    for r in fasts:
        psi = r.get("psi", {})
        cpu = r.get("cpu", {})
        vm = r.get("vm", {})
        pb = [v for k, v in cpu.items() if int(k) in pcores]
        pbusy = sum(pb) / len(pb) if pb else 0.0
        interesting = (
            psi.get("cpu_some", 0) > 2000 or
            psi.get("io_full", 0) > 2000 or
            psi.get("memory_some", 0) > 500 or
            vm.get("pgmajfault", 0) > 0 or
            vm.get("pgfault", 0) > 20000 or
            pbusy > 0.35 or r["slip"]
        )
        if not interesting:
            continue
        shown += 1
        if shown > 80:
            continue
        print("%8.2f %8d %8d %8d %8.2f %9d %9d %6s %7.1f%s"
              % (r["ts"], psi.get("cpu_some", 0), psi.get("io_full", 0),
                 psi.get("memory_some", 0), pbusy,
                 vm.get("pgfault", 0), vm.get("pgmajfault", 0),
                 r.get("irq"), r["dt"] * 1000, "  SLIP" if r["slip"] else ""))
    if shown > 80:
        print("  ... %d more" % (shown - 80))
    if shown == 0:
        print("  nothing above threshold -- system was quiet")

    # --- 5. block time from the fine rate -------------------------------------
    if fines:
        key = "%s/%s" % (a.track_proc, a.track_comm)
        print()
        print("=" * 78)
        print("5. BLOCK TIME -- %s, voluntary switches vs periods (%.3f ms)"
              % (key, a.period_ms))
        print("   A callback that blocks on something sleeps more often than one that")
        print("   just processes and waits for the next period -- and blocked time is")
        print("   neither RUN nor WAIT, so schedstat cannot see it at all.")
        print("   The baseline is empirical, not 1.0: an epoll driver loop legitimately")
        print("   wakes several times per period (PipeWire's data-loop.0 sits near 5).")
        print("   What matters is deviation from this thread's own quiet baseline.")
        print()

        per_sec_vol = defaultdict(int)
        per_sec_run = defaultdict(float)
        per_sec_span = defaultdict(float)
        doubles = []
        sched_extra = defaultdict(int)
        for r in fines:
            if r["slip"]:
                continue
            sec = int(r["ts"])
            for tid, comm, pcomm, drun, dwait, dvol, dinv, extra in r["th"]:
                if "%s/%s" % (pcomm, comm) != key:
                    continue
                per_sec_vol[sec] += dvol
                per_sec_run[sec] += drun / 1e6
                per_sec_span[sec] += r["dt"]
                for k, v in extra.items():
                    sched_extra[k] += v
                doubles.append((r["ts"], dvol, drun / 1e6, dwait / 1e6, r["dt"] * 1000))

        if not per_sec_span:
            print("  no fine samples for %s -- was it running? check --track-proc/--track-comm" % key)
        else:
            rows = []
            for sec in sorted(per_sec_vol):
                span = per_sec_span[sec]
                if span < 0.2:
                    continue
                periods = span * 1000.0 / a.period_ms
                rows.append((sec, per_sec_vol[sec], periods,
                             per_sec_vol[sec] / periods if periods else 0,
                             per_sec_run[sec]))
            if rows:
                ratios = sorted(r[3] for r in rows)
                med = ratios[len(ratios) // 2]
                print("baseline sw/period for this thread (median second): %.2f" % med)
                print("%5s %9s %10s %10s %9s  %s"
                      % ("sec", "vol_sw", "periods", "sw/period", "run_ms", "vs baseline"))
                for sec, vol, periods, ratio, run in rows:
                    dev = (ratio / med - 1.0) * 100 if med else 0
                    mark = "  <<<" if abs(dev) > 25 else ""
                    print("%5d %9d %10.1f %10.2f %9.2f  %+6.0f%%%s"
                          % (sec, vol, periods, ratio, run, dev, mark))

            # The spike is a single callback, so per-second aggregates can hide it.
            # List the individual fine windows that stand out.
            print()
            for label, idx in (("voluntary switches", 1), ("on-CPU ms", 2)):
                top = sorted(doubles, key=lambda d: -d[idx])[:15]
                if not top:
                    continue
                print("top fine windows by %s:" % label)
                print("%10s %6s %9s %9s %8s" % ("ts", "vol", "run_ms", "wait_ms", "dt_ms"))
                for ts, dvol, drun, dwait, dt in top:
                    print("%10.3f %6d %9.3f %9.3f %8.2f" % (ts, dvol, drun, dwait, dt))
                print()

        if sched_extra:
            print()
            print("kernel.sched_schedstats fields seen (totals over run, ns):")
            for k in sorted(sched_extra):
                print("  %-22s %d" % (k, sched_extra[k]))
        else:
            print()
            print("no sched_schedstats fields present "
                  "(enable with: sudo sysctl -w kernel.sched_schedstats=1)")

    # aggregate
    print()
    tot = defaultdict(int)
    for r in fasts:
        for k, v in r.get("psi", {}).items():
            tot["psi_" + k] += v
        for k, v in r.get("vm", {}).items():
            tot["vm_" + k] += v
    dur = fasts[-1]["ts"] - fasts[0]["ts"]
    print("run totals over %.1fs:" % dur)
    for k in sorted(tot):
        print("  %-28s %d" % (k, tot[k]))


if __name__ == "__main__":
    main()
