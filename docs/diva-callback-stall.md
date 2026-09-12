# A plug-in that stalls the callback, and a counter that lied about it

Investigated 2026-09-12, on the session described in the README: quantum 256/48000,
9 Kontakt instances through yabridge plus one u-he Diva, kernel 7.2.4-cachyos-rt.

Audible glitches returned. Bitwig reported **Load MAX 32.767 ms** against a 5.333 ms
deadline, at **Load AVG 0.968 ms** and **period jitter 0.71 %** — a healthy steady
state with rare, enormous spikes. 32.767 ms is `INT16_MAX` microseconds, so the figure
is saturated: the real stall is *at least* that, with no upper bound reported.

**The cause was Diva.** Its plug-in host's `bitwig-remote-p` threads occasionally ran
for 89 ms and 91 ms simultaneously at `SCHED_FIFO` 85 while the audio callback waited.
Removing the single Diva instance took callback stalls from 26 to 0 over identical
240 s windows.

The route there is worth recording, because the tooling in this repo actively pointed
the wrong way for several rounds.

## What it was not

Every one of these was measured and eliminated before the cause was found.

| Ruled out | Evidence |
|---|---|
| Thread placement regression | 532 managed threads, 0 misplaced, steward alive |
| DSP compute in the callback | `data-loop.0` max RUN **0.681 ms** per 2 ms window |
| CPU starvation | all P-cores 86–98 % idle; whole Bitwig tree 0.27 cores |
| Disk I/O | **0 reads/s** on all three NVMe over 8 s |
| Memory pressure | `psi_mem` 0, `pgmajfault` 0 across a 90 s run |
| Block time / futex waits | voluntary switches per period flat at ~4.0, max +9 % |
| RT throttling | `sched_rt_runtime_us == sched_rt_period_us`, disabled |
| Thermal throttling | 0 throttle events, cores at 5.13 GHz |
| PipeWire graph driver | `pipewire/data-loop.0` max WAIT **0.005 ms** over 90 s |

`iowait` deserves its own line, because it wasted a round. One P-core read 97 %
non-idle, which looks like a pegged core until you notice 85 % of it is `iowait` —
which *is* idle time, and which was backed by literally zero disk traffic.

## The counter that lied

`tools/catch-stall.py` reported stalls of 192 ms and 210 ms, and `catch-load.py`
agreed. Both read schedstat field 2, the kernel's `sched_info.run_delay`. The values
are not physical:

```
210.08 ms of runqueue wait   inside a 1.91 ms window   (single tid)
219.00 ms                    inside a 5.00 ms window   (independent raw /proc loop)
 17.90 ms of on-CPU time     inside a 2.00 ms window
```

A thread cannot wait longer than the window that contains it. Two independent readers
produced these, so it is the counter and not the tooling. The decisive cross-check:
schedstat attributed **98,497 ms of CPU in 90 s** to the Diva host, while
`/proc/<pid>/stat` — a counter with no such problem — puts the same process at **0.06
cores**. The likely mechanism is a stale `sched_info.last_queued` after toggling
`kernel.sched_schedstats` from 0 to 1, so the first enqueue after enabling computes
its delta against an ancient timestamp.

This does **not** retroactively break `docs/dsp-spike-investigation.md`. That
investigation used `wait_max = 0.538 ms` to rule scheduling delay *out*, and a
spurious jump cannot fabricate a small maximum. What it breaks is using the field to
locate a large spike, which is exactly what was attempted here.

A caution that generalises past this one field: **when a measurement implies something
arithmetically impossible, stop and check the instrument.** Three separate rounds were
spent explaining a 200 ms stall that never happened.

## The technique that worked

`tools/catch-gap.py`. Three ideas, none of which need a tracepoint — `perf sched` was
unavailable because `/sys/kernel/tracing` is root-only regardless of
`perf_event_paranoid`.

1. **Trigger on a counter, not a timer.** schedstat field 3 is the number of
   timeslices the thread has been given — a plain event count, not a duration, and so
   not subject to the field-2 defect. Poll only that one file, at ~2 kHz. When it
   changes, the callback ran; the wall-clock since the last change is the gap.

2. **Guard the observer by counting iterations.** A 2 kHz loop turns over about twice
   per millisecond. If a 300 ms "gap" contains three iterations, the observer was
   asleep and the gap is its own. This is strictly better than timing the window,
   which cannot distinguish the two cases. It matters: the first version of this
   detector, without the guard, reported 8 gaps up to 308 ms that all vanished on a
   guarded re-run.

3. **Attribute by diffing on-CPU across the gap.** Snapshot every chain thread's
   schedstat field 1 at each callback and diff across a stall. A thread that
   accumulated 89 ms inside a 174 ms gap was running for the whole thing. Sample these
   only at gap boundaries — reading ~300 files at 2 kHz would make the observer the
   problem.

Two threshold mistakes cost a round each, and are worth stating so they are not
repeated. A detector set at `>12 ms` missed an 11.47 ms spike by half a millisecond.
And a "burst > 10 ms of CPU in one 2 ms sample" test can never fire, because a thread
running 89 ms straight accumulates only ~2 ms per 2 ms sample — sustained load has to
be measured across a window, not within one.

## The result

Identical 240 s windows, same detector, same thresholds:

| | with Diva | without Diva |
|---|---|---|
| callback stalls > 8 ms | **26** | **0** |
| CPU in stall windows | 793 ms, Diva's threads | none |
| xruns / 300 s, Bitwig + playback | 0 | 0 |
| xruns / 300 s, capture node | +305 | **+0** |
| `SCHED_FIFO` 85 threads | 122 | ~66 |

Attribution with Diva loaded, aggregated over all stall windows:

```
  Diva : bitwig-remote-p      706.3 ms
  Diva : PluginsThreadPo       86.4 ms
  wineserver                   17.8 ms
  yabridge : audio             17.5 ms
  Kontakt : MemExpander        13.0 ms
```

Diva is a native Linux VST, so it does not go through yabridge: `bitwig-remote-p` in
that host *is* Diva's `process()` call. Nine Kontakt instances contributed two orders
of magnitude less than one Diva.

**Confound, stated plainly:** removing Diva removed both its DSP *and* halved the
`SCHED_FIFO` 85 population, 122 → 66. The per-thread attribution points at Diva's own
threads rather than at generic crowding, but this test does not fully separate the
two. Re-adding Diva and varying its multicore and quality settings would; that has not
been done. **No claim is made here about which Diva setting is responsible** — only
that the instance was.

## The dead end worth keeping

Bitwig's audio callback runs at `SCHED_FIFO` **83**, below the 122 threads at **85**
that it dispatches to — 64 `PluginsThreadPool`, 32 `audio-N`, 17 `bitwig-remote-p`,
9 yabridge `audio` — all confined to the same 16 P-cores. Sampling run-state at 2 ms
over 15 s: 79.7 % of samples had zero runnable RT threads, but 0.9 % had *more
runnable than there are P-cores*, peaking at 21. In those bursts the lowest-priority
RT thread loses its core, and that is the one carrying the deadline.

The cost was real and measurable: `data-loop.0` migrating **1192 times a second**,
6.4 per buffer, each cache-cold plus an IPI, against a 40× IPI imbalance on the
P-cores (CAL 17,108/s vs 410/s on the E-cores).

So `steer-threads.sh` grew `CALLBACK_PRIO`, which lifts the callback to 86 — the
ordering the chain should have had, PipeWire's driver 88 > callback 86 > workers 85.
It works: migrations went 1192/s → **0**.

**It did not fix the glitches.** Load MAX stayed at 32.767 ms and the spikes
continued. The inversion is real, the fix is real, and it was not the cause. It stays
off by default.

It also failed silently at first, which is its own lesson: `chrt -f` clears
`SCHED_RESET_ON_FORK`, which an unprivileged caller may not do, so every sweep
returned `EPERM` — invisible, because `start-bitwig.sh` sends the steward's output to
`/dev/null` and a failed promotion is simply not counted. `--dry-run` cheerfully
reported a promotion that could never happen, because it prints intent without
attempting it. `chrt -f -R` fixes it.

## Correction to the README

The README calls the RME **capture** node's error count "a red herring" because the
node has no links. Its count is certainly inflated, but it is not pure noise: it ran
at 1–4.7 errors/s throughout the glitching and dropped to **exactly zero over 300 s**
the moment the stalls stopped. It tracked graph health here. Watch the output node and
Bitwig for the number that matters, but a capture count that suddenly stops moving is
information, not nothing.
