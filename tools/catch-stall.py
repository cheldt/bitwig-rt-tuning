#!/usr/bin/env python3
"""Tell a long *run* apart from a long *wait* on the audio threads.

catch-spike.py samples /proc/<tid>/schedstat field 1 -- time spent on-CPU. That finds
threads burning CPU, which is how wineserver was caught. It cannot see a thread that
is ready to run and simply is not scheduled, and it samples at 100 ms (~19 buffers at
256/48000) so it cannot resolve a single-buffer overrun at all.

This samples both halves of schedstat at buffer-ish resolution:

    field 1  time on CPU        (ns)  -> the thread ran long
    field 2  time on runqueue   (ns)  -> the thread was ready but not scheduled
    field 3  timeslices                -> how many times it was picked

A DSP spike that shows up as RUN is the plugin genuinely taking too long: the fix is
less work per buffer (voice count, quality setting) or a bigger buffer. A spike that
shows up as WAIT is a scheduling or contention problem: something is holding a core or
a lock, and a bigger buffer only hides it.

Only threads at realtime priority >= RT_MIN are watched, so the output is the audio
path and nothing else.

BROKEN ON THIS KERNEL (7.2.4-cachyos-rt, found 2026-09-12) -- DO NOT TRUST THE WAIT
COLUMN. schedstat field 2 is the kernel's sched_info.run_delay, and on this box it
reports values that are not physical: a single tid accumulating 210 ms of runqueue
wait inside a 1.91 ms window, and 219 ms inside a 5.0 ms one. A thread can wait at
most as long as the window is wide, so these are the counter, not the machine. Two
independent readers -- this tool and a separate raw /proc loop -- produce them, and
the reliable counters flatly disagree: schedstat once attributed 98,497 ms of CPU in
90 s to a process that /proc/<pid>/stat puts at 0.06 cores. The likely cause is a
stale sched_info.last_queued after toggling kernel.sched_schedstats, so the first
enqueue after enabling computes its delta against an ancient timestamp.

What this does NOT invalidate: small values, and the RUN column. The original
investigation used wait_max = 0.538 ms to rule scheduling delay *out*, and a bogus
jump cannot fake a small maximum. RUN (field 1) stayed consistent with
/proc/<pid>/stat throughout.

For locating a large spike, use tools/catch-gap.py instead. It triggers on schedstat
field 3, a plain event count rather than a time, and attributes the stall by diffing
on-CPU time across the gap. That is what actually found the cause; this tool sent the
investigation after phantom 200 ms stalls for several rounds first.

IMPORTANT -- the observer can lie. This process is SCHED_OTHER, and if it is itself
descheduled the sample window stretches and *every* delta inside it inflates by the
same amount. The tell is unrelated threads reporting near-identical huge values in one
sample. So each window is timed, and any window longer than SLIP_FACTOR x nominal is
discarded rather than reported. Discarded windows are counted and printed: if that
count is not small, the numbers that did survive are still suspect and the observer
needs a core of its own.

Run it with the observer protected, or it will lie to you:

    chrt -f 10 taskset -c 16-31 python3 tools/catch-stall.py 8.0 300 20

Limitations -- read these before believing the output:

  * RESOLUTION. 20 ms is about 3.75 buffers at 256/48000, so this CANNOT resolve a
    single-buffer overrun. A thread reading 12.5 ms of RUN per 20 ms window is at 62 %
    duty, which averages ~3.3 ms per buffer -- under a 5.333 ms deadline even though
    individual buffers may have blown through it. Treat RUN as "this thread is
    expensive", never as "this thread missed a deadline".

  * WAIT DELTAS CAN EXCEED THE WINDOW, legitimately. The kernel accumulates
    sched_info.run_delay and adds it in bulk when the thread is finally scheduled, so a
    377 ms wait appears entirely inside whichever 20 ms window the thread woke in. The
    number describes the preceding period, not the window.

  * TID REUSE is only partly handled. Threads are keyed on (tid, starttime) so a
    recycled tid is dropped rather than producing a garbage delta, but a thread that
    exits and is replaced within one window is simply missed.

  * FREQUENCY IS NOT CAUSATION. Measured on 8x Diva: ~266 large WAIT events per 300 s
    (0.89/s) against an xrun rate of 0.033/s -- a 27:1 ratio. Most large waits cause no
    audible miss. Do not infer a stall from a WAIT value alone; correlate it with
    tools/measure-xruns.sh.

Usage: catch-stall.py [threshold_ms] [duration_s] [interval_ms]
       defaults: 5.0 ms, 120 s, 20 ms
"""
import os, sys, time, collections

THRESH_MS = float(sys.argv[1]) if len(sys.argv) > 1 else 5.0
DURATION  = float(sys.argv[2]) if len(sys.argv) > 2 else 120.0
INTERVAL  = (float(sys.argv[3]) if len(sys.argv) > 3 else 20.0) / 1000.0
RT_MIN    = int(os.environ.get('RT_MIN', '50'))
SLIP_FACTOR = float(os.environ.get('SLIP_FACTOR', '2.0'))

PROCS = ('BitwigAudioEngi', 'BitwigPluginHos', 'yabridge-host.e', 'BitwigStudio')


def rt_threads():
    """tid -> label, for every thread at realtime priority >= RT_MIN."""
    out = {}
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            pcomm = open(f'/proc/{pid}/comm').read().strip()
        except OSError:
            continue
        if pcomm not in PROCS:
            continue
        try:
            tids = os.listdir(f'/proc/{pid}/task')
        except OSError:
            continue
        for tid in tids:
            try:
                stat = open(f'/proc/{pid}/task/{tid}/stat').read()
                rest = stat[stat.rindex(') ') + 2:].split()
                policy, rtprio = int(rest[38]), int(rest[37])
                if policy not in (1, 2) or rtprio < RT_MIN:
                    continue
                starttime = rest[19]
                tcomm = open(f'/proc/{pid}/task/{tid}/comm').read().strip()
            except (OSError, ValueError, IndexError):
                continue
            # starttime is part of the identity: a recycled tid is a different thread,
            # and diffing its counters against the dead one's yields nonsense.
            out[f'{pid}/{tid}/{starttime}'] = f'{pcomm}/{tcomm}'
    return out


def sample(threads):
    out = {}
    for key, label in threads.items():
        pid, tid, _start = key.split('/')
        try:
            f = open(f'/proc/{pid}/task/{tid}/schedstat').read().split()
            out[key] = (int(f[0]), int(f[1]), int(f[2]), label)
        except (OSError, IndexError, ValueError):
            continue
    return out


threads = rt_threads()
print(f'watching {len(threads)} realtime threads (rtprio >= {RT_MIN}), '
      f'threshold {THRESH_MS} ms, interval {INTERVAL*1000:.0f} ms, {DURATION:.0f} s',
      flush=True)

prev = sample(threads)
prev_t = time.monotonic()
slipped = 0
windows = 0
worst_window = 0.0
run_hits = collections.Counter()
wait_hits = collections.Counter()
worst_run = collections.defaultdict(float)
worst_wait = collections.defaultdict(float)

t_end = time.time() + DURATION
rescan = time.time() + 30
while time.time() < t_end:
    time.sleep(INTERVAL)
    if time.time() > rescan:
        threads = rt_threads()
        rescan = time.time() + 30
    cur = sample(threads)
    now = time.monotonic()
    elapsed = now - prev_t
    prev_t = now
    windows += 1
    worst_window = max(worst_window, elapsed)
    # The observer was starved: every delta in this window is inflated by the same
    # amount, so none of it is usable.
    if elapsed > INTERVAL * SLIP_FACTOR:
        slipped += 1
        prev = cur
        continue
    events = []
    for key, (run, wait, slices, label) in cur.items():
        if key not in prev:
            continue
        d_run = (run - prev[key][0]) / 1e6
        d_wait = (wait - prev[key][1]) / 1e6
        if d_run >= THRESH_MS:
            events.append((d_run, 'RUN ', label))
            run_hits[label] += 1
            worst_run[label] = max(worst_run[label], d_run)
        if d_wait >= THRESH_MS:
            events.append((d_wait, 'WAIT', label))
            wait_hits[label] += 1
            worst_wait[label] = max(worst_wait[label], d_wait)
    if events:
        events.sort(reverse=True)
        stamp = time.strftime('%H:%M:%S')
        print(f'{stamp}  ' + '  |  '.join(f'{k} {n} {d:.1f}ms' for d, k, n in events[:4]),
              flush=True)
    prev = cur

print(f'\n{windows} windows, {slipped} discarded for observer slip '
      f'({100.0*slipped/max(windows,1):.1f} %), longest window {worst_window*1000:.1f} ms',
      flush=True)
if slipped > windows * 0.05:
    print('  WARNING: observer was starved often; surviving numbers are suspect.',
          flush=True)

print('\n=== RUN (thread genuinely took too long) ===', flush=True)
for label, n in run_hits.most_common(12):
    print(f'  {n:6d}  worst {worst_run[label]:7.1f} ms   {label}', flush=True)
if not run_hits:
    print('  none', flush=True)

print('\n=== WAIT (thread was ready but not scheduled) ===', flush=True)
for label, n in wait_hits.most_common(12):
    print(f'  {n:6d}  worst {worst_wait[label]:7.1f} ms   {label}', flush=True)
if not wait_hits:
    print('  none', flush=True)
