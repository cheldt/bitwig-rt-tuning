#!/usr/bin/env python3
"""Find the thread responsible for sporadic idle spikes.

Samples /proc/<tid>/schedstat (nanosecond CPU time) across the whole audio chain
every 100ms and reports any thread that burns an unusual amount of CPU in one
window. At idle the entire chain uses ~0.3ms per 5.33ms buffer, so a thread eating
several ms in a single 100ms window is the outlier we are hunting.
"""
import os, sys, time, collections

THRESH_MS = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0
DURATION  = float(sys.argv[2]) if len(sys.argv) > 2 else 300.0

def targets():
    pids = {}
    for pid in os.listdir('/proc'):
        if not pid.isdigit(): continue
        try:
            comm = open(f'/proc/{pid}/comm').read().strip()
            cmd  = open(f'/proc/{pid}/cmdline').read().replace('\0', ' ')
        except OSError:
            continue
        if ('yabridge' in comm or 'wine' in comm.lower() or '.exe' in comm
                or 'Bitwig' in comm or 'NI' in comm
                or '.exe' in cmd or 'Bitwig' in cmd):
            pids[int(pid)] = comm
    return pids

def sample(pids):
    out = {}
    for pid, pcomm in pids.items():
        try: tids = os.listdir(f'/proc/{pid}/task')
        except OSError: continue
        for tid in tids:
            try:
                cpu = int(open(f'/proc/{pid}/task/{tid}/schedstat').read().split()[0])
                tc  = open(f'/proc/{pid}/task/{tid}/comm').read().strip()
            except (OSError, IndexError, ValueError):
                continue
            out[int(tid)] = (cpu, f'{pcomm}/{tc}')
    return out

pids = targets()
print(f'watching {len(pids)} processes, threshold {THRESH_MS}ms per 100ms window, '
      f'{DURATION:.0f}s', flush=True)

prev = sample(pids)
hits = collections.Counter()
t_end = time.time() + DURATION
rescan = time.time() + 30
while time.time() < t_end:
    time.sleep(0.1)
    if time.time() > rescan:
        pids = targets(); rescan = time.time() + 30
    cur = sample(pids)
    burst = []
    for tid, (cpu, name) in cur.items():
        if tid not in prev: continue
        d_ms = (cpu - prev[tid][0]) / 1e6
        if d_ms >= THRESH_MS:
            burst.append((d_ms, name))
    if burst:
        burst.sort(reverse=True)
        stamp = time.strftime('%H:%M:%S')
        print(f'{stamp}  ' + '  |  '.join(f'{n} {d:.1f}ms' for d, n in burst[:4]), flush=True)
        for _, n in burst: hits[n] += 1
    prev = cur

print('\n=== bursts per thread ===', flush=True)
for name, n in hits.most_common(15):
    print(f'  {n:5d}  {name}', flush=True)
