#!/usr/bin/env python3
"""Find the gaps in Bitwig's audio callback, and say which thread filled them.

This exists because the other two stall tools could not answer the question. Bitwig's
Load is wall-clock per callback, so a spike means the callback thread was not running
between two periods -- but `tools/catch-spike.py` samples on-CPU time and so cannot see
a thread that is not running at all, and `tools/catch-stall.py` reads schedstat's
runqueue-wait field, which on this kernel reports values that are not physical (see the
warning in that file). What is left is the one thing in schedstat that is a plain event
count rather than a time: field 3, the number of timeslices the thread has been given.

The method:

  1. Poll ONLY the callback's schedstat field 3 in a tight loop. One file, no parsing
     beyond a split, so the loop runs at ~2 kHz. When the count changes, the callback
     has been scheduled; the wall-clock time since the previous change is the gap.

  2. Guard the observer. If this process is itself descheduled, the gap it measures is
     its own, not the callback's. So count loop iterations within each gap: a real
     2 kHz loop turns over about twice per millisecond, and a window that produced far
     fewer iterations than its own length means the observer was asleep and the sample
     is discarded. This is the check `tools/catch-stall.py` does with a timed window;
     doing it by iteration count is strictly better, because it cannot be fooled by a
     clock read that happens to land either side of the stall.

  3. Attribute. Snapshot every thread in the audio chain's schedstat field 1 (on-CPU
     time) at each callback, and diff across a gap. A thread that accumulated 89 ms of
     CPU inside a 174 ms gap was running for the whole stall, and that is what the
     callback was waiting for. Do not sample these at the fast rate -- the boundaries
     are the only places the value is needed, and reading ~300 files at 2 kHz would
     make the observer the problem.

Plugin hosts are labelled by the instance id Bitwig gives them (`157939-2`), which is
argv[3] of BitwigPluginHost, so the output names the plug-in rather than a pid. Run
`ps -ef | grep BitwigPluginHost` to map ids to names.

Run the observer protected and off the audio cores, or it will measure itself:

    chrt -f 10 taskset -c 16-31 python3 tools/catch-gap.py --dur 240

Reading the result:

  * A gap with a thread burning CPU for most of it is a plug-in taking too long. That
    is a DSP problem: the fix is that plug-in's settings, or fewer instances of it.
  * A gap with almost no CPU accumulated anywhere is NOT a glitch. The callback simply
    was not called -- the transport is stopped, or the engine is idle. Discount these;
    they are why the aggregate at the end is more trustworthy than any single gap.
  * The aggregate is the finding. One gap is an anecdote.

Usage: catch-gap.py [--dur S] [--gap-ms MS] [--min-cpu MS]
       defaults: 240 s, 8 ms (1.5 periods at 256/48000), 1.0 ms
"""
import argparse
import collections
import os
import time

CHAIN_PROCS = ("BitwigPluginHos", "yabridge-host.e", "BitwigAudioEngi", "wineserver")


def read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def find_callback():
    """(pid, tid) of BitwigAudioEngine's data-loop.0 -- the thread Load is measured on."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        comm = read(f"/proc/{pid}/comm")
        if not comm or comm.strip() != "BitwigAudioEngi":
            continue
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            tcomm = read(f"/proc/{pid}/task/{tid}/comm")
            if tcomm and tcomm.strip() == "data-loop.0":
                return pid, tid
    return None, None


def chain_threads():
    """[(label, open schedstat fd)] for every thread that can hold up a callback."""
    out = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        comm = read(f"/proc/{pid}/comm")
        if not comm or comm.strip() not in CHAIN_PROCS:
            continue
        pcomm = comm.strip()
        # BitwigPluginHost's argv[3] is the instance id ("157939-2"), which is the only
        # thing that tells two plug-in hosts apart from inside /proc.
        cmd = (read(f"/proc/{pid}/cmdline") or "").replace("\0", " ").split()
        tag = cmd[3][:28] if pcomm == "BitwigPluginHos" and len(cmd) > 3 else pcomm
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            tcomm = read(f"/proc/{pid}/task/{tid}/comm")
            if not tcomm:
                continue
            try:
                fh = open(f"/proc/{pid}/task/{tid}/schedstat")
            except OSError:
                continue
            out.append((f"{tag}:{tcomm.strip()}", fh))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dur", type=float, default=240.0, help="seconds")
    ap.add_argument("--gap-ms", type=float, default=8.0,
                    help="report gaps longer than this (8 ms = 1.5 periods at 256/48000)")
    ap.add_argument("--min-cpu", type=float, default=1.0,
                    help="ignore threads accumulating less than this inside a gap")
    a = ap.parse_args()

    pid, tid = find_callback()
    if not tid:
        raise SystemExit("BitwigAudioEngine/data-loop.0 not found - is the engine running?")

    chain = chain_threads()
    cb = open(f"/proc/{pid}/task/{tid}/schedstat")

    def timeslices():
        """None once the engine exits -- the run then ends and reports what it has."""
        try:
            cb.seek(0)
            return int(cb.read().split()[2])
        except (OSError, IndexError, ValueError):
            return None

    def snapshot():
        out = []
        for _label, fh in chain:
            try:
                fh.seek(0)
                out.append(int(fh.read().split()[0]))
            except (OSError, IndexError, ValueError):
                out.append(0)
        return out

    print(f"callback tid={tid}, {len(chain)} chain threads, {a.dur:.0f}s, "
          f"gap > {a.gap_ms} ms", flush=True)

    prev = timeslices()
    if prev is None:
        raise SystemExit("callback thread went away before sampling started")
    base = snapshot()
    t0 = time.monotonic()
    iters = 0
    gaps = []
    slipped = 0
    total = collections.Counter()

    end = time.time() + a.dur
    while time.time() < end:
        time.sleep(0.0005)
        iters += 1
        cur_count = timeslices()
        if cur_count is None:
            print("\nengine exited - ending the run and reporting what was collected.")
            break
        if cur_count == prev:
            continue
        now = time.monotonic()
        gap = (now - t0) * 1000.0
        if gap > a.gap_ms:
            # The observer must have stayed awake for the gap to mean anything. At
            # 2 kHz a real window turns over ~2 iterations per ms; anything under 0.3
            # per ms is this process having been descheduled, not the callback.
            if iters > gap * 0.3:
                cur = snapshot()
                deltas = sorted(
                    ((cur[i] - base[i]) / 1e6, chain[i][0]) for i in range(len(chain))
                )
                top = [(round(v, 1), lab) for v, lab in deltas[-3:] if v >= a.min_cpu]
                gaps.append((gap, top))
                for v, lab in deltas:
                    if v >= a.min_cpu:
                        total[lab] += v
            else:
                slipped += 1
        prev = cur_count
        base = snapshot()
        t0 = time.monotonic()
        iters = 0

    print(f"\nreal gaps > {a.gap_ms} ms: {len(gaps)}   "
          f"({slipped} discarded as observer slip)")
    if gaps:
        print("\nworst gaps, and what was on CPU during them:")
        for gap, top in sorted(gaps, reverse=True)[:10]:
            print(f"  {gap:8.1f} ms  {top if top else '(nothing running - not a glitch)'}")
    if total:
        print("\nCPU (ms) accumulated inside stall windows, by host:thread -- THE FINDING:")
        for lab, v in total.most_common(10):
            print(f"  {lab:44s} {v:8.1f}")
    else:
        print("\nNo thread accumulated CPU inside any stall window.")


if __name__ == "__main__":
    main()
