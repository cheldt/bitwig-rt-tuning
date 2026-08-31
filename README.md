# bitwig-rt-tuning

Pro-audio session tuning for Bitwig Studio + Kontakt/yabridge on Linux.

> **This is a personal project, heavily optimized for one specific machine.**
> It is published as a record of an investigation, not as a tool to install.
> Nearly every value in it — core numbers, IRQ numbers, device names, the Wine
> prefix path, the sysctl baselines — is hard-coded for the hardware below.
> Running it unmodified on any other system will, at best, do nothing useful,
> and at worst pin your audio chain to cores that do not exist. Read it, take
> the ideas, and rewrite the constants for your own box.

## The machine it was written for

| | |
|---|---|
| CPU | Intel i9-13900K — hybrid, CPUs 0–15 P-cores, 16–31 E-cores |
| Audio interface | RME HDSPe AIO Pro (`snd_hdspe`, IRQ 16) |
| RAM | 32 GB |
| Kernel | 7.2.2-cachyos-rt-bore-lto |
| Audio stack | PipeWire 1.6.8 / WirePlumber 0.5.15, quantum 256/48000 |
| Host | Bitwig Studio, native PipeWire client |
| Plugins | Kontakt 6, FM8, Diva via yabridge 5.1.1 (Wine 11.15, ntsync) |
| GPU | Nvidia (proprietary driver, IRQ 211) |
| Storage | `/media/nvme1` = **nvme0n1** (wine prefix + NI library, ext4) · `/` `/home` = **nvme1n1** (btrfs) · `/media/nvme2` = nvme2n1 (projects, ext4) |

The mount names do not match the device names: `/media/nvme1` is `nvme0n1`, and
`nvme1n1` is the root/home drive. An earlier A/B measured the wrong device because
of it — see F1 in the investigation.

## What it does

`start-bitwig.sh` takes a baseline, applies session-scoped tuning, launches
Bitwig, and restores everything on exit (including after a crash, via
`--restore` and a state file under `$XDG_RUNTIME_DIR`).

- **CPU placement.** Bitwig's tree is pinned to the P-cores; a background
  steward (`tools/steer-threads.sh`) then re-splits it by realtime priority —
  threads at rtprio ≥ 50 keep the P-cores, every other thread is pushed to the
  E-cores. This is the fix that actually mattered; see below.
- **Power.** `performance` governor, deep C-states (C2/C3 ACPI) disabled on the
  P-cores only, Nvidia PowerMizer to max.
- **Interrupts.** `snd_hdspe` IRQ pinned to one P-core; the two noisiest IRQs
  (xhci, nvidia) pushed to the E-cores.
- **Memory.** `vm.swappiness=10`, `vm.min_free_kbytes=256M` so a realtime thread
  never lands in direct reclaim.
- **Storage.** NVMe queue scheduler set to `none` for the session — preventive,
  for streaming libraries; measured null on RAM-resident samples.
- **Noise.** EasyEffects stopped for the session (and restarted after, only if
  the script was the one that stopped it). yabridge STDERR logging off by
  default: it cost 71.6 ms per 5 s across the plugin hosts with 11 instances.

## The finding

Sporadic audible glitches at quantum 256, with Load MAX hitting 7.5–8.0 ms
against a 5.333 ms deadline, at near-zero DSP load.

It was not throughput and not the buffer size. It was **CPU placement**: the
single `taskset` that pinned Bitwig to the P-cores was inherited by the whole
process tree, so 16 cores carried 121 realtime audio threads *and* 412
non-realtime ones — the JVM UI, `wineserver`, `explorer.exe`, the NI services.
Aggregate load was about 6 % per P-core. `wineserver` is single-threaded,
`SCHED_OTHER` nice 0, and every Wine process calls into it synchronously: a
textbook priority inversion.

Splitting the tree by rtprio instead of by process fixed it. Load MAX 1.911 ms,
period jitter 9.41 % → 0.75 %, zero deadline misses in 90 s, quantum unchanged
at 256.

Two consequences worth repeating:

- **Pinning without the steward is worse than not pinning at all.** The script
  refuses that combination rather than starting a silently degraded session.
- **A 512 quantum only masked this.** 256 holds with 11 plugin instances once
  the chain is split correctly.

### And a second one: the plugin-load spike

Steady state healthy, but one spike every time a plugin is instantiated — Load
MAX 2.05–2.12 ms at 0.08–0.12 ms average.

Same class of cause, opposite direction. `fork()` gives a child the *calling
thread's* affinity, and Bitwig forks `BitwigPluginHost` from a JVM worker thread
that the steward has already moved to the E-cores. So the host is born inside
`16-31` and creates its 33 `SCHED_FIFO` 85 audio threads there — where they stay
until the next 15 s sweep. Measured: 8.7 s of a 6.8 s plugin load with the audio
threads on 4.3 GHz cores.

Not fixable by pre-setting the inherited mask: there is no main thread to
pre-set, because the JVM does not fork from main. `--watch` now polls `/proc`
every 0.25 s and sweeps in a 12 s burst when a matched process appears. E-core
window 8.7 s → 0.52 s, Load MAX 2.054 → 1.867 ms, Load AVG 0.104 → 0.064 ms,
worst callback on-CPU 2.075 → 0.455 ms.

The residual 1.867 ms is *block* time in the synchronous cross-process call at
plugin activation, which `schedstat` cannot see at all. Ruled out along the way,
each with numbers: priority inversion, disk, clocks, Bitwig's graph rebuild, a
2.66 M minor-fault storm, and DXVK/lavapipe.

The full write-up — including the corrections, the dead ends, and the
hypotheses that measured as wrong — is in
[`docs/dsp-spike-investigation.md`](docs/dsp-spike-investigation.md).

## Layout

```
start-bitwig.sh                 session: tune, launch, restore
perf.sh                         standalone one-shot tweaks (no restore)
tools/steer-threads.sh          the rtprio split; --watch loop and --restore
tools/measure-xruns.sh          deadline misses on the playback path
tools/catch-spike.py            find the thread burning CPU in a spike
tools/catch-stall.py            tell a long *run* apart from a long *wait*
tools/catch-load.py             sample the audio chain across a plugin load
tools/summarize-load.py         reduce a catch-load.py run to the four decisive views
tools/faultgen.c                controlled minor-fault storm, to test memory pressure
tools/ab-nvme-sched.sh          A/B the NVMe scheduler in one live session
docs/dsp-spike-investigation.md the investigation
docs/kontakt7-zmq-crash.md      why the yabridge host needs a cwd inside the wine prefix
docs/measurements/              raw logs behind the claims
docs/reference-*                config files and wrappers this setup depends on, for reference
```

## Usage

```bash
./start-bitwig.sh              # start a session
./start-bitwig.sh --restore    # clean up after a crash that skipped the trap
```

Environment knobs, mainly for A/B testing:

| var | default | effect |
|---|---|---|
| `PIN_BITWIG` | `1` | `0` = no CPU placement at all |
| `STEER_THREADS` | `1` | `0` = pin, but do not re-split by rtprio |
| `STEER_INTERVAL` | `15` | seconds between steward sweeps |
| `YABRIDGE_LOG` | `0` | `1` = enable yabridge debug log (costs DSP) |

Requires passwordless-ish `sudo` (the script keeps the timestamp alive for the
length of the session), `cpupower`, `taskset`, `nvidia-settings`, and `pw-top`.

## Measuring

Don't trust Bitwig's Load MAX for small differences — two identical runs
measured 5.402 and 6.052 ms. Use `tools/measure-xruns.sh`, watch the *output*
node (the RME capture node has no links and its error count is a red herring),
and run for at least 300 s: glitch bursts have 60–90 s gaps, so short windows
give false "fixed" readings.

Verify placement before trusting any measurement. The steward can fail to
launch with no symptom other than a bad Load MAX.
