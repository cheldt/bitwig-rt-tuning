# Sporadic DSP spikes — investigation and resolution

**Date:** 2026-08-29
**Machine:** i9-13900K (8 P-cores / 16 E-cores), RME HDSPe AIO Pro, 32 GB,
kernel 7.2.2-cachyos-rt-bore-lto, PipeWire 1.6.8, WirePlumber 0.5.15,
Bitwig Studio (native PipeWire client), Kontakt 6 via yabridge 5.1.1 (Wine 11.15)

## Outcome

Sporadic audible glitches at quantum 256, eliminated.

| | before | after |
|---|---|---|
| Load MAX | **7.5 – 8.0 ms** (over the 5.333 ms deadline) | **1.911 ms** |
| Load AVG | 0.311 ms | 0.538 ms |
| Period jitter | 9.41 % | 0.75 % |
| Audible deadline misses | glitches | **0 in 90 s** |
| Quantum | 256 | 256 (unchanged) |

**The fix was not system tuning.** It was consolidating 8 separate Kontakt
instances into **2 instances × 4 slots**.

> **Superseded 2026-08-29.** The consolidation is no longer needed. Once the audio
> chain is split across the P- and E-cores by realtime priority (see "Second
> investigation"), **9 separate Kontakt instances run clean** — one host process per
> instance, no slot consolidation, no project restructuring. Consolidating was a way of
> reducing the number of threads competing on 16 over-subscribed cores; steering the
> non-audio threads off those cores removes the competition instead, which is both
> cheaper and invisible to how you lay out a project.

## Symptom

Sporadic spikes in Bitwig's DSP Performance Graph, audible as glitches whenever
Load MAX reached the deadline. Present with transport stopped and near-zero DSP
load. Gaps of 60–90 s between bursts.

## Root cause

> **Correction, 2026-08-29 (FM8 session).** The mechanism described below is wrong.
> `bitwig-remote-p` threads are **`SCHED_FIFO` 85**, not `SCHED_OTHER` nice −4 — verified
> with `chrt -p` and with field 41 of `/proc/<tid>/stat` on a live session. They are the
> per-instance plugin audio proxy threads, doing real work, not a sync side-channel.
> The **measured result** of the 2×4 consolidation stands; only this explanation of it
> does not. See "Second investigation" at the end of this document for what is actually
> going on, and why fewer instances helped anyway.

Each Kontakt instance carries one `bitwig-remote-p` thread doing continuous plugin
parameter/state sync. Those threads are `SCHED_OTHER` (nice −4) and live **inside
`BitwigPluginHost`**, the same process as the `SCHED_FIFO` 85 audio threads.

When such a thread holds a lock the realtime audio thread needs and is then
preempted, the audio thread blocks for milliseconds — **priority inversion**.

Thread-level CPU sampling at idle, all plugin GUIs closed
(`docs/measurements/2026-08-29_thread-profile_8x1-instances.log`):

| thread | CPU |
|---|---|
| `BitwigStudio/X11RenderComman` | 2272 ms |
| `BitwigPluginHos/bitwig-remote-p` | **1956 ms** |
| `yabridge-host/ProcessMonitor` | 293 ms |
| `yabridge-host/audio` | **98 ms** ← the actual audio work |
| `ZWorkerOld#5/#6` (JVM GC) | 67 + 67 ms, bursts to 66.8 ms |

Plugin sync burned **20× the audio work**. In **43 of 59** windows where an audio
thread burst, a `bitwig-remote-p` thread burst in the same 100 ms window.

After consolidating to 2 × 4, the profile **inverted**
(`..._2x4-instances.log`): `bitwig-remote-p` fell below the reporting threshold
entirely and `yabridge-host/audio` became the top consumer at 6870 ms — i.e. real
DSP work, which is what an audio workload should look like.

This explains every observation: sporadic, uncorrelated with DSP load, scaling
non-linearly with instance count, and immune to every system-level fix.

## Why 2 × 4 and not 1 × 8

> **Superseded.** Kept for the bisection data, which is still a useful record of how
> the fault scaled with instance count. The layout constraint itself is gone — see the
> note under Outcome.

Bisection measured the cliff:

| instances | Load MAX |
|---|---|
| 8 × 1 | 8.001 ms |
| 4 × 2 | 0.795 ms |
| 2 × 4 | 1.911 ms |

One instance would mean Bitwig sees a single plugin and can only run it on one
thread, serialising all 8 instruments.

On **Kontakt's multiprocessor support**: the original argument against it was that
yabridge elevates exactly *one* thread per host to FIFO 85 (`audio`) while every
Kontakt-spawned thread (`worker`, `SC3 TaskScheduler`) sits at FIFO 5 through Wine's
priority mapping — so enabling MP would make the FIFO-85 thread block on FIFO-5 workers
that all 64 Bitwig audio threads can preempt, the same inversion on every buffer.
`steer-threads.sh` now moves those FIFO-5 workers to the E-cores, where no FIFO-85
thread runs, which removes exactly that preemption. **The argument no longer holds as
stated, and MP has not been measured since.** Leave it off until someone does.

## System tuning — what to keep

These fixed real, *separate* problems. None of them fixed the spikes.

| change | measured effect |
|---|---|
| Idle states C2_ACPI + C3_ACPI disabled on P-cores 0–15 | jitter 9.41 % → 0.75 %. C3 exit latency is **1048 µs** against a 5333 µs deadline; `governor=performance` does not prevent C-state entry. Package stays at 43 °C, zero throttle events. E-cores untouched. |
| `api.alsa.headroom = 128` on the RME | PipeWire resyncs constant → **0** |
| IRQ steering: snd_hdspe→CPU 2, nvidia (4.4M irq) + xhci→E-cores; pipewire pinned to P-cores | part of the same jitter result |
| WirePlumber `node.disabled` on Nvidia HDMI, UR22, onboard PCH input | removed 3 resampled follower nodes from the graph (costs HDMI audio + UR22) |
| `vm.min_free_kbytes` 22762 → 262144 | free RAM 0 → 4 GB; precautionary, no measured effect on spikes |

Config lives in `~/.config/wireplumber/wireplumber.conf.d/99-pro-audio.conf`
(reference copy: `docs/reference-wireplumber-99-pro-audio.conf`).

## Correction: ntsync

An earlier revision of this document and of `start-bitwig.sh` claimed ntsync "does
not exist on this kernel". That was wrong. `CONFIG_NTSYNC=m`, the module ships at
`/lib/modules/$(uname -r)/kernel/drivers/misc/ntsync.ko.zst`, and the boot journal
shows `systemd-modules-load: Inserted module 'ntsync'` via
`/usr/lib/modules-load.d/ntsync.conf`.

The module appeared missing because the **original script's `restore()` ran
`sudo rmmod ntsync` on every exit**, unloading it system-wide and silently
downgrading every Wine process on the machine to fsync until the next reboot.
Removing that `rmmod` is the actual fix; the matching `modprobe` is redundant
because modules-load.d already handles it at boot.

ntsync is preferred over fsync: it is the mainlined NT-synchronization driver,
lower overhead and semantically more correct, and this prefix's runner
(`wine-11.14 TkG Staging NTsync`) is built for it. Wine auto-detects `/dev/ntsync`
and falls back to fsync when it is absent or unreadable, so `WINEFSYNC=1` stays set
as the fallback. `WINESYNC=1` was dropped — that variable belongs to the old
out-of-tree winesync driver that was renamed to ntsync before mainlining, and is
genuinely a no-op.

There is no udev rule for ntsync on this system, so the node can come up
`root:root 0600` and Wine will fall back to fsync without saying so. Install
`/etc/udev/rules.d/70-ntsync.rules`:

```
KERNEL=="ntsync", MODE="0660", GROUP="audio"
```

`start-bitwig.sh` now prints which mechanism is in use at startup rather than
failing silently.

## System files outside this repo

The script only covers what can be set per-session. These persist and must be
recreated by hand after a reinstall. Reference copies live in `docs/`, but the
active files are the paths below.

| path | purpose | reference copy |
|---|---|---|
| `~/.config/wireplumber/wireplumber.conf.d/99-pro-audio.conf` | `api.alsa.headroom = 128` on the RME; `node.disabled` on Nvidia HDMI, UR22 and the onboard PCH input | `docs/reference-wireplumber-99-pro-audio.conf` |
| `~/.vst/yabridge/yabridge.toml` | Kontakt hosting mode. Grouping is present but **commented out** — it measured harmful, see Dead ends | `docs/reference-yabridge.toml` |
| `/etc/udev/rules.d/70-ntsync.rules` | makes `/dev/ntsync` group-readable so Wine can actually use it | `docs/reference-udev-70-ntsync.rules` |

### udev: /dev/ntsync

`ntsync` is loaded at boot by `/usr/lib/modules-load.d/ntsync.conf`, but nothing on
this system granted non-root access to the device node, so it came up `root:root
0600` and Wine fell back to fsync **without reporting it**.

```
KERNEL=="ntsync", MODE="0660", GROUP="audio"
```

Install:

```sh
sudo install -m644 docs/reference-udev-70-ntsync.rules /etc/udev/rules.d/70-ntsync.rules
sudo udevadm control --reload && sudo udevadm trigger
```

Verified state after installing (the account must be in `audio`):

```
$ ls -l /dev/ntsync
crw-rw---- 1 root audio 10, 261 /dev/ntsync
```

`start-bitwig.sh` prints `Wine sync: ntsync` at startup when this is working, and
`Wine sync: fsync (ntsync unavailable)` with the specific cause when it is not.

### udev rules deliberately NOT installed

`60-nvme-scheduler.rules`, which would have set the nvme queue scheduler from `kyber`
to `none` for every boot. The scheduler change itself was later made — see
"Applied 2026-08-30" below — but as a session-scoped write in `start-bitwig.sh`, not a
udev rule, so the machine keeps its CachyOS default (`kyber`, from
`/usr/lib/udev/rules.d/60-ioschedulers.rules`) outside a Bitwig session.

The `nodiscard` fstab change was also not made, and that one has *not* been reversed —
see Dead ends.

## 2026-08-30: the steward was not running, and nothing said so

Found while setting up an unrelated measurement. A session started that morning had
`start-bitwig.sh` pinning Bitwig to the P-cores with `tools/steer-threads.sh` never
launched — the exact combination this document already calls harmful, running unnoticed.

**Cause.** `~/.local/bin/start-bitwig.sh` had been made a symlink into this repo.
`SCRIPT_DIR` was `$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)`, and `dirname` on a
symlink path gives the *link's* directory, so the steward resolved to
`~/.local/bin/tools/steer-threads.sh`, which does not exist. It was launched with
`>/dev/null 2>&1`, so the failure was silent and the session looked normal. The bug
predates the symlink; nothing had invoked the script through one before.

**State it produced** — 121 realtime threads *and* 355 non-realtime ones all on cores
0-15, nothing on 16-31, `wineserver` still at nice 0. `--dry-run` wanted to move 407
threads. Bitwig's DSP panel read Load MAX **16.258 ms against a 5.333 ms deadline** at
Load AVG 0.571 ms — three times the deadline, on a project using about a tenth of the
available time. Contention, not capacity, exactly as in the second investigation.

**Effect of starting the steward by hand**, same session, same project, same playhead,
nvme scheduler held constant:

| 300 s window | Bitwig node | RME playback |
|---|---|---|
| unsteered | **+11** (0.037/s) | +2 |
| interval between windows, still unsteered | +14 | — |
| steward active | **+0** | **+0** |

Full numbers in `docs/measurements/2026-08-30_steer-threads-restored.log`.

**What this does and does not show.** The capture node was the driver during the first
window and had dropped out of the graph by the second, so its 5511 → 0 is the counter
following the driver again — a topology change, not a result. The Bitwig node is never
the driver, which is why its progression is the one that carries: steady accrual across
two separate unsteered windows, exactly zero once the split was restored. Still n=1 per
arm, and the windows were sequential rather than interleaved, so treat the size of the
effect as unestablished — the direction is not in doubt.

**Fixed** in `ce750d1`: `readlink -f` before `dirname`; refuse to pin at all when the
steward is missing, falling back to `PIN_BITWIG=0` rather than starting a degraded
session; and a liveness check one second after launch, since a steward that exits on its
own leaves the tree pinned with nothing steering it.

The lesson worth keeping is not the symlink. It is that the most load-bearing fix in this
repo was disabled for a whole session and the only symptom was a number in the DSP panel
that could just as easily have been blamed on a plugin. Any future component whose absence
is this expensive should say so out loud rather than being launched into `/dev/null`.

## Applied 2026-08-30: nvme scheduler `none` for the session

`start-bitwig.sh` now writes `none` to `/sys/block/{nvme0n1,nvme1n1,nvme2n1}/queue/scheduler`
at startup and puts the recorded value back on exit, next to the governor, C-state and
sysctl knobs. Baseline goes into the state file as `NVME_<dev>_BEFORE`, so `--restore`
works after a crash. SATA `sda`/`sdb` stay on `mq-deadline`; single-queue AHCI does not
benefit from `none`.

**This is a preventive change and it produced no measurable effect.** That breaks the
usual rule in this document — a tweak is kept only when a specific number moved — so the
exception is recorded deliberately rather than dressed up as a result.

An A/B was attempted (`tools/ab-nvme-sched.sh`, arms `none`/`kyber`/`none`, 300 s each,
same project and playhead throughout) and abandoned after arm 1, because arm 1 showed
there was nothing to measure:

```
arm 1/3: none, 300s
  nvme1n1  reads      +0   read  +0.0 MB   read-wait  +0 ms
  nvme0n1  reads     +45   read  +0.3 MB   read-wait +27 ms   (writes +30.0 MB, 0.5% busy)
  nvme2n1  reads      +0   read  +0.0 MB   read-wait  +0 ms
```

**Zero reads on nvme1n1 over five minutes of playback** — the drive holding the Wine
prefix and the entire NI library. The 0 KB premise is not stale; it is exactly as true as
when it was first measured. A scheduler reorders a queue, and there is no queue. The
nvme0n1 traffic is write-side (project autosave and log churn on the root filesystem) at
0.5 % device busy, nowhere near a stall.

So the A/B cannot distinguish the arms today, and running the remaining two would only
have produced two more numbers indistinguishable from noise. The comparison is deferred
until a project actually exceeds RAM — that is the condition that creates the queue this
change is meant to shorten.

Why the earlier rejection no longer holds. It rested on one measurement: Kontakt reads
**0 KB from disk during playback**, because the sample content is fully RAM-resident. That
is a fact about *this* library set on a 31 GiB box, not a property of the workload. A
library too large to fit streams from disk (Kontakt DFD), and at that point `/media/nvme1`
is on the audio path and queue behaviour starts to matter. The change is cheap, reversible
and session-scoped, so it goes in ahead of that rather than after the first xrun it causes.

Expect no difference today. The thing worth re-checking later is the premise itself: if
Kontakt is ever observed reading non-zero KB from disk during playback, this stops being
preventive and this section needs a real before/after number.

### Generic "Wine disk I/O tuning" advice — evaluated 2026-08-30, mostly not applicable

Prompted by a generic Wine-on-Linux I/O tuning list. Measured against this machine, one
item was worth doing (above) and the rest were already done, not applicable, or wrong here.
Recorded so the list does not get re-evaluated.

| advice | verdict on this machine |
|---|---|
| `noatime` in fstab | Already on every mount — all four ext4 `/media/*` and every btrfs subvol. |
| kernel `ntfs3` over `ntfs-3g` | Not applicable — no NTFS mount exists. |
| `vm.dirty_background_ratio=5` / `vm.dirty_ratio=10` | **Would be a regression.** CachyOS already sets `dirty_bytes=268435456` / `dirty_background_bytes=67108864` (`/usr/lib/sysctl.d/70-cachyos-settings.conf`). The `*_ratio` and `*_bytes` pairs are mutually exclusive — writing a ratio zeroes the bytes value. On 31 GiB that swaps a 64 MB/256 MB threshold for roughly 1.6 GiB/3.1 GiB, about 12x *looser*. |
| `vm.swappiness=10` | Already done per session by `start-bitwig.sh`. |
| `vm.vfs_cache_pressure=50` | Already 50. |
| Wine `Temp` → tmpfs symlink | Nothing to gain: `$WINEPREFIX/drive_c/users/kenoby/AppData/Local/Temp` is **empty (4.0K)** — no runtime churn. (`users/steamuser/Temp` holds 133 MB of stale installer spill, not written during a session.) `/tmp` is already tmpfs. |
| `nofile 1048576` | Already 1048576 soft *and* hard, from `/usr/lib/systemd/user.conf.d/10-limits.conf`. |
| GPU shader cache placement | Off the audio path — GPU/UI only, and root is already NVMe. |

## Dead ends — do not re-chase

| tried | result |
|---|---|
| **yabridge plugin groups** | **Harmful.** Load AVG 0.175 → 0.229 ms, MAX 6.05 → 8.00 ms. 8 instances in one Wine process serialise on shared per-process locks. Config syntax if ever needed: file sits next to the `.so` in `~/.vst/yabridge/`, patterns match `.so` paths relative to it (`["Kontakt.so"]`, **not** `.dll`), first match wins. |
| **Raising RME playback `priority.driver` above capture** | **No effect.** The capture node (prio 2500 vs 1500) drives by default and carries a huge error count — but that count is just where PipeWire tallies *graph* xruns and it **follows whichever node is the driver**. Swapping moved the counter (capture 3494→4, playback 3→249) and fixed nothing. The capture node is not faulty. |
| **`nodiscard` on /media/nvme1** | No effect. Inline discard is genuinely slow (2.64 ms/op vs 0.67 ms/read) but Kontakt reads **0 KB from disk** during playback — samples are fully RAM-resident. |
| **nvme scheduler `kyber` → `none`** | Rejected here for the same reason — no disk activity on the audio path — then **deliberately reversed on 2026-08-30** once it was pointed out that the premise expires with a large enough library. Applied per session, not measured. See "Applied 2026-08-30" above. |
| **Pinning Bitwig to P-cores** (`taskset -c 0-15`) | Unproven *on its own*, and **harmful without `tools/steer-threads.sh`** — the mask is inherited by the whole tree, so it drags every non-audio thread onto the audio cores too. See "Second investigation". Halves Load AVG (E-cores are ~half the IPC). `PIN_BITWIG` toggle in `start-bitwig.sh`, default on, paired with `STEER_THREADS`. |
| **Quantum 512** | Works, but was only masking the inversion — doubling the deadline to 10.67 ms let a fixed-duration stall fit underneath. Unnecessary once the instance layout is fixed. |

Also ruled out with measurements: CPU contention (16.8 ms runqueue wait vs 427.6 ms
CPU over 5 s across 72 FIFO threads), wineserver serialisation (0.004 ms average
wait per wakeup), memory pressure (PSI ≈ 0), thermal throttling (0 events), and OS
scheduling latency (`cyclictest -p 90` during playback: avg 2 µs, **max 223–256 µs**
against a 5333 µs deadline — the OS contributes under 5 %).

## Methodology notes

Two traps that cost significant time:

1. **Bitwig's Load MAX is a max over the visible window**, so longer observation
   always finds a bigger peak. Readings that looked like improvements (3.334 ms
   baseline, 4.196 ms unpinned) were short windows that had not yet caught a burst.
   Two runs on an *identical* config measured 5.402 and 6.052 ms. Never compare
   Load MAX across runs of different length.
2. **Watch the right node.** The RME *capture* node has zero links and is not on the
   audible path; its error count runs in the thousands and is a red herring. The
   audible metric is the error count on `pro-output-0` + the `Bitwig Studio` node.
3. Bursts have 60–90 s gaps — sample **5+ minutes** or a short window gives a false
   "fixed" reading.

## Tools

- `tools/catch-spike.py [threshold_ms] [duration_s]` — samples
  `/proc/<tid>/schedstat` across the whole audio chain every 100 ms and reports any
  thread burning more than the threshold in one window. This is what found the root
  cause; reach for it first when the fault is known to be in the plugin chain.
- `tools/measure-xruns.sh [seconds]` — counts deadline misses on the **playback**
  path. Use instead of eyeballing Load MAX.
- `tools/catch-stall.py [threshold_ms] [duration_s] [interval_ms]` — samples *both*
  halves of `schedstat` for every thread at rtprio ≥ 50 and separates **RUN** (the
  thread genuinely took too long) from **WAIT** (it was ready but not scheduled). RUN
  points at the plugin: less work per buffer, or a bigger buffer. WAIT points at
  contention, which a bigger buffer only hides. Run it protected —
  `chrt -f 10 taskset -c 16-31 python3 tools/catch-stall.py 8.0 300 20` — and read the
  limitations in its docstring first; it did not close the Diva case.

Quick regression check after any change to the instance layout:

```sh
ps -eLo comm --no-headers | grep -c bitwig-remote-p   # one per Kontakt instance
pgrep -c -f 'yabridge-host\.exe'                      # one per instance
python3 tools/catch-spike.py 3.0 300                  # audio work should dominate
tools/measure-xruns.sh 600                            # expect 0 on the audible path
```

## Remaining, not causing glitches

- `X11RenderComman` — 3744 ms, largely rendering the DSP Performance Graph window
  itself. Close it when not reading it.
- `ZWorkerOld` — JVM ZGC bursts of 50–80 ms in the Bitwig UI process. Largest
  remaining perturbation, but a separate process from the audio engine and not
  breaching anything at current margins.

---

# Second investigation — 9× FM8, 2026-08-29

## Outcome

| | before | after |
|---|---|---|
| Load MAX | **14.808 ms** (2.8× the deadline) | **0.904 ms** |
| Load AVG | 0.412 ms | 0.450 ms |
| Period jitter | 4.22 % | **0.90 %** |
| Deadline misses, audible path | glitches | **0 in 300 s** (`Bitwig 9→9`, `pro-output-0 2→2`) |
| P-cores 0–15 busy | 95 % of 1600 % | 20–40 % |
| E-cores 16–31 busy | 9 % of 1600 % | 43–61 % |
| Quantum | 256 | 256 (unchanged) |

**The fix was one change: `tools/steer-threads.sh`.** No project edit, no plugin
reload, no quantum change, no sandbox-mode change — the two `BitwigPluginHost`
processes kept their PIDs across the whole session, confirming nothing was reloaded.

Nine FM8 instances were added to the project. FM8 is monotimbral, so the 2×4 slot
consolidation that fixed the Kontakt case cannot be applied to it. Bitwig reported
**Load MAX 14.808 ms** against the 5.333 ms deadline at quantum 256, with period
jitter back up to 4.22 % (from 0.75 %).

The cause turned out to be different from the first investigation, and it invalidates
that document's stated mechanism.

## What was actually measured

`bitwig-remote-p` is **`SCHED_FIFO` 85**, one per plugin instance, and it is the plugin
audio proxy thread — not a `SCHED_OTHER` sync thread. Over 120 s at a 2 ms threshold it
bursts **14 times**. It is a minor actor now.

Bitwig is on sandbox mode **"By Plugin"** (`EACH_PLUGIN`), so it runs *one host process
per plugin*, not per instance: one `BitwigPluginHost ... host
Native-Instruments-GmbH-FM8` holding all 9 FM8s, and a second holding the 2 Kontakts.

Burst leaderboard, `tools/catch-spike.py 2.0 120`:

| thread | bursts | worst seen |
|---|---|---|
| `wineserver/wineserver` | **431** | 7.1 ms |
| `BitwigAudioEngi/data-loop.0` | 238 | 2.2 ms |
| `yabridge-host.e/audio` | 222 | — |
| `BitwigStudio/X11RenderComman` | 153 | **26.2 ms** |
| `BitwigStudio/BitwigStudio` | 144 | 6.7 ms |
| `yabridge-host.e/DBScan` | 139 | — |
| `BitwigPluginHos/bitwig-remote-p` | 14 | — |

Thread census across the chain:

- **121 threads at realtime priority 85**: 32 `BitwigAudioEngi/audio-1..32`,
  11 `bitwig-remote-p`, 11 `yabridge-host/audio`, and 64 `PluginsThreadPo` — the last
  of which are parked and burn ~0 ms, so they are not a factor.
- **87 threads at `SCHED_FIFO` 5** — `yabridge-host/worker`, `parameters`,
  `SC3 TaskScheduler`, `URET_Worker`, `wine_sechost_de`, `BitwigPluginHost/host-callbacks`.
  Wine's priority mapping puts every plugin-spawned thread here. They are realtime in
  name only: they rank below all 121 audio threads.
- **325 `SCHED_OTHER` threads** — `wineserver`, the Bitwig JVM UI, `explorer.exe`,
  `NIHardwareService.exe`, `NIHostIntegrationAgent.exe`, and 9× FM8 `DBScan`.

**All 533 of them were on CPUs 0–15**, because `PIN_BITWIG=1` tasksets the whole tree
and the mask is inherited across fork/exec.

Per-core busy over 3 s: **P-cores 0–15 at 95 % of a possible 1600 %, E-cores 16–31 at
9 %**. Aggregate load was trivial — about 6 % per P-core. Half the machine was idle.

## Diagnosis: placement, not throughput

Any thread that a realtime thread waits on synchronously, but which cannot preempt it,
is an inversion waiting to happen. `wineserver` is the worst case in this chain: it is
single-threaded, every one of the 12 Wine processes calls into it, and it was pinned to
the same 16 CPUs as 121 FIFO-85 threads at `SCHED_OTHER` nice 0. A `yabridge-host/audio`
thread that makes a Wine call waits on wineserver; wineserver waits behind other
FIFO-85 threads. That is why it led the burst table, and why the problem scales with
instance count.

`ntsync` is confirmed live (`wineserver: NTSync up and running!`, and `/dev/ntsync`
opens as the session user), so this is not an fsync fallback.

Two secondary findings:

- `taskset -c 0-15 nproc` returns 16, yet the engine spawned `audio-1` … `audio-32` and
  passed `32` to both host processes. **Bitwig sizes its graph from the machine CPU
  count and ignores the affinity mask**, so pinning hands it a 32-wide DSP graph to run
  on 16 CPUs.
- 9× `DBScan` (the Native Instruments content-DB scanner, `SCHED_OTHER`, one per FM8
  instance) burned 433–533 ms each over ~7 minutes of *idle*.

## Fix: split by realtime priority, not by process

`tools/steer-threads.sh` re-sweeps the whole chain and pins each thread by what it
actually is:

- realtime priority ≥ 50 → P-cores 0–15
- everything else (FIFO 5 and `SCHED_OTHER`) → E-cores 16–31
- `wineserver` additionally gets `renice -10`; `-11` is the ceiling `limits.d` grants
  `@audio`/`@realtime`, so no privileges are needed. It is deliberately **not** made
  `SCHED_FIFO`: it is single-threaded and spinning there would be worse.

Splitting by *priority* rather than by policy matters — a policy-based split leaves the
87 FIFO-5 Wine helpers on the audio cores, where they are starved by the 121 FIFO-85
threads exactly like a `SCHED_OTHER` thread would be.

`start-bitwig.sh` runs it as `--watch 15` alongside Bitwig (`STEER_THREADS`, default
on) and undoes it from `restore()`. The watch is not optional: threads keep appearing
while a session runs. A one-shot sweep that had placed 533 threads correctly was
already 5 threads out of date 20 minutes later.

Two correctness details worth keeping:

- Split on **realtime priority**, not policy. `SCHED_FIFO` 5 is not "realtime enough"
  to earn a P-core here.
- Read the policy from `/proc/<tid>/stat` field 41, not by forking `chrt` 533 times.
  The `SCHED_RESET_ON_FORK` flag is *not* OR'd into that field, so Bitwig's
  `data-loop.0` (`SCHED_FIFO|SCHED_RESET_ON_FORK`, prio 83) parses as policy 1 and
  correctly keeps its P-core. Verify after any change to the sweep that no thread with
  rtprio ≥ 50 ends up outside the P-core mask.

## Also changed

`YABRIDGE_DEBUG_FILE` is now opt-in via `YABRIDGE_LOG=1`. It was set unconditionally;
every Wine STDERR line crosses a pipe that a `wine-stdio` thread in `BitwigPluginHost`
has to read, measured at 71.6 ms per 5 s across the hosts.

## Bitwig sandbox modes — reference

Enum from `bitwig.jar`, mapped to the Settings ▸ Plugins labels:

| UI label | enum | effect |
|---|---|---|
| With Bitwig | `IN_ENGINE` | no host process; plugins load inside BitwigAudioEngine |
| Together | `ALL_PLUGINS` | one host process for every plugin |
| By Vendor | `EACH_PLUGIN_VENDOR` | one host process per vendor |
| By Plugin | `EACH_PLUGIN` | one per plugin — **current** |
| Individually | `EACH_PLUGIN_INSTANCE` | one per plugin instance |

The per-plugin override list below the setting
(`plugins_to_run_in_separate_process`) only forces named plugins *towards*
Individually. There is no override to put one plugin In-Engine while the rest stay
sandboxed, so `IN_ENGINE` is all-or-nothing.

If peaks survive the placement fix, `IN_ENGINE` is the next thing to try, not
`Individually`. The audio path is currently two cross-process hops per plugin per
buffer, and the middle one costs more than the plugin itself — over 5 s,
`bitwig-remote-p` 730 ms against `yabridge-host/audio` 450 ms. In-engine deletes it.
The cost is that an FM8 crash takes the audio engine down.

Against `Individually`: the 9 `bitwig-remote-p` threads measured 66.9, 67.0, 67.3,
67.5, 67.8, 67.9, 68.2, 68.6 and 41.4 ms over the same 5 s — near-identical, i.e.
running in parallel rather than serialising on a shared process lock. It also adds
9×32 more parked pool threads, and does nothing for wineserver, `DBScan` or
`X11RenderComman`, which are Wine- and JVM-side and unaffected by the sandbox mode.

## Confirmed after the fix: the instance-layout constraint is gone

With `steer-threads.sh` in place, the project was reloaded with **9 separate Kontakt
instances** — one `yabridge-host` and one `bitwig-remote-p` per instance, no slot
consolidation. That is the exact layout the first investigation measured at Load MAX
8.001 ms and called the root cause.

Measured over 300 s at quantum 256:

| node | errors |
|---|---|
| `Bitwig Studio` (audible) | 6 → 6, **+0** |
| `pro-output-0` (audible) | 2 → 2, **+0** |

Thread census in that layout: 83 threads at rtprio ≥ 50 on the P-cores, 382 non-audio
threads on the E-cores, `wineserver` on the E-cores at nice −10.

So the 2×4 consolidation was never addressing the real fault. Both it and the placement
fix reduce the same thing — the number of threads contending on 16 over-subscribed
cores — but consolidation does it by constraining how you build a project, and the
placement fix does it by using the other half of the CPU. Only one of those is free.

**Practical consequence:** instance count is no longer a tuning parameter. Load
plugins the way the music wants.

## Still open

- **NI `DBScan` churn.** 9 scanner threads, one per FM8 instance, 139 bursts in 120 s.
  Worth checking whether `NIHostIntegrationAgent.exe` / `NIHardwareService.exe` can be
  kept out of the prefix (no NI hardware is attached), and whether the FM8 **VST3**
  build starts fewer background threads — `~/.vst3/yabridge/FM8.vst3` exists, but the
  project loads the VST2 `FM8.dll`. `steer-threads.sh` puts these on E-cores anyway,
  so this is cleanup rather than a fix.
- **yabridge grouping for FM8 only.** Recorded as harmful in Dead ends — but that was
  measured on *Kontakt*, where 8 instances serialised on shared per-process DSP locks.
  FM8's cost here is not DSP; it is 9 duplicated Wine processes, 9 sets of NI
  background threads and 9 clients on one single-threaded wineserver. The reasoning
  does not transfer, so `["FM8.so"] group = "fm8"` deserves its own A/B.

---

# Third pass — 8x Diva, 2026-08-29

Eight u-he Diva instances (**native Linux**, not yabridge) on top of 9 Kontakt.
Bitwig showed sporadic spikes and a Load MAX readout of 32.767 ms.

## Two things that are not the problem

**The 32.767 ms readout is not a measurement.** 32767 us = 2^15-1 exactly: Bitwig's DSP
graph saturated a 16-bit counter. Read it as ">= 32.767 ms", never as a value, and do
not compare it against anything.

**Diva does not spawn worker threads.** Its host process carries one
`bitwig-remote-p` (FIFO 85, P-core) per instance and 32 *parked* `PluginsThreadPo`
threads at 0 ms CPU. Multicore is off. So the "native plugin whose SCHED_OTHER workers
get exiled to the E-cores" failure mode does not apply here -- placement is correct.

## The fault is load-dependent, and structurally different

| | transport playing | transport stopped |
|---|---|---|
| Bitwig node | +10 / 300 s (0.033/s) | +1 to +2 / 300 s |
| `pro-output-0` | +2 / 300 s (0.007/s) | +0 to +1 / 300 s |

Idle is clean; playing is not. This is **genuine DSP demand**, not starvation -- a
different class of fault from the wineserver inversion, and it does not respond to
placement.

The structural point:

> 8 Diva instances give 8 parallel threads, but **one instance's per-buffer work is
> serial and unsplittable**. Bitwig cannot spread a single plugin instance across cores,
> and the buffer completes only when the *slowest* instance finishes. So P-cores idling
> at 11 % busy does not help: if one instance needs more than 5.333 ms, 15 free cores
> are irrelevant.

Measured steady state: each Diva `bitwig-remote-p` burns ~780 ms per 5 s (15.6 % duty,
0.83 ms per buffer), bursting to 55 ms per 100 ms window. P-cores 181 % of 1600.

**`catch-spike.py` is the wrong instrument for this.** It samples every 100 ms -- about
18.75 buffers -- so it cannot resolve a single-buffer overrun. During the 55 ms bursts
the per-buffer average is 2.9 ms, comfortably under deadline, while individual buffers
may still blow through it. It also measures CPU *consumed*, so a thread that stalls by
*blocking* stays invisible. It found wineserver because wineserver was genuinely
burning CPU while starved of a core.

**ZGC is visible but innocent.** `ZWorkerOld#0-3` burst to 84-108 ms, 55 times in 300 s
-- the largest CPU events in the profile. But they are `SCHED_OTHER` prio 0 on the
E-cores and cannot preempt a FIFO-85 thread, and ZGC is a *concurrent* collector: those
figures are background worker CPU, not stop-the-world pauses. Heap was 1.86 GB against
`-Xmx3g`. Biggest number in the table is not the culprit.

## The sweep was perturbing what it protects

Measured while chasing the above: **the original `steer-threads.sh` forked 5206
processes per sweep**, every 15 s, and the steward inherited affinity `0-31` -- so those
processes landed on the audio cores. Beyond the CPU, process teardown triggers TLB
shootdown IPIs to other cores, which reaches realtime threads regardless of priority.

Cause was `$(cat ...)`, `awk`, `grep` and command substitution in the per-thread hot
path -- roughly four forks per thread across ~530 threads. Rewritten to use `read <
file`, parameter expansion and globals instead of command substitution:

| | before | after |
|---|---|---|
| forks per sweep | 5206 | **10** |
| system-wide forks / 60 s | ~20800 | **128** |
| steward affinity | `0-31` | `16-31` (self-pins in `--watch`) |

Classification is byte-identical before and after (verified by diffing `--dry-run`
output). **The effect on xruns was below measurement resolution** -- idle runs gave +2
before and +1 after, which is noise at n=1. The change is justified by the fork count
and the placement bug, not by a measured xrun win.

## Levers for load-dependent Diva peaks, in order

1. **Diva's Accuracy setting.** Biggest lever, free. Divine -> Great is roughly 2-4x
   off the per-instance cost. Per-patch, so it is not readable from the prefs file.
2. **Quantum 512.** Legitimate *here*, unlike everywhere else in this document. Earlier
   it masked a fixed-duration inversion stall; these are real DSP peaks that need a
   bigger deadline. 10.67 ms instead of 5.333 ms.
3. **Freeze or bounce** finished Diva tracks.
4. **Diva multicore -- tested, no effect. Safe, but do not bother.**

## Diva multicore: measured, and the predicted risk did not materialise

The concern going in was that u-he's multicore workers would come up `SCHED_OTHER` or
`SCHED_FIFO` 5, the sweep would exile them to the E-cores, and the FIFO-85 audio thread
would end up waiting on half-IPC cores -- worse than leaving it off. That was wrong.

**Diva does not spawn worker threads at all.** It drives **Bitwig's own host thread
pool** through the VST3 thread-pool mechanism. A watcher sampling the host process every
2 s for 120 s across the transition recorded **zero thread-name or count changes**. The
only thing that changed is that the 32 `PluginsThreadPo` threads -- already `SCHED_FIFO`
85 on the P-cores, and previously parked at 0 ms CPU for the process's entire life --
started doing work:

| 5 s delta, transport playing | multicore off | multicore on |
|---|---|---|
| `PluginsThreadPo` | 0.0 ms, **0 active** | 456.3 ms, **32 active** |
| `bitwig-remote-p` | all of it | 4941 ms across 8 |

So placement is correct by construction and **no per-thread-name exception in
`steer-threads.sh` is needed**. That mechanism still does not exist, and this case did
not justify building it.

**Activation needs both a project reload and actual voices.** Toggling the setting does
nothing to already-instantiated plugins, and at idle with no notes there is nothing to
parallelize, so the pool stays at 0 ms and it looks like the setting was ignored.

**It did not help.** Measured over 300 s with the transport playing, against the
pre-multicore baseline:

| node | multicore off | multicore on |
|---|---|---|
| Bitwig | +10 (0.033/s) | **+10** (0.033/s) |
| `pro-output-0` | +2 (0.007/s) | +1 (0.003/s) |

The Bitwig node is the sound comparison and it is identical; +2 vs +1 on `pro-output-0`
is noise at that count. Consistent with the size of the offload -- 456 ms of ~5400 ms,
about 8 % -- which is not enough to move the tail.

Multicore pays off with *few* instances and *many* voices. With 8 instances the host
already provides 8-way parallelism, which is u-he's own stated caveat. Leave it off or
on; nothing measurable rides on it.

## Diva: what the RUN/WAIT split did and did not settle

`tools/catch-stall.py` was written for this, because `catch-spike.py` reads only
`schedstat` field 1 and therefore cannot see a thread that is ready but unscheduled.

**Settled:** `bitwig-remote-p` reached **12.5 ms of RUN per 20 ms window, 230 times in
300 s** — 62 % duty per instance. Diva is genuinely, heavily expensive. Placement is not
the problem and has not been since the P/E split.

**Not settled: the mechanism behind the 29.612 ms Load MAX.** Three reasons the WAIT
side could not carry the argument:

1. **The first run was contaminated by the observer.** Pinning it to the E-cores kept it
   off the audio path but left it `SCHED_OTHER` among ~400 threads including ZGC workers
   bursting to 108 ms, so it was itself descheduled and every delta in those windows
   inflated together. The tell was unrelated threads reporting matching values
   (`yabridge/audio` 564.3 beside 564.2). Fixed by timing each window and discarding
   slipped ones, and by running the observer at `SCHED_FIFO` 10.
2. **Rerunning clean did not remove the large WAIT values** — 13620 windows, 9 discarded
   (0.1 %), yet still 377 ms and 573 ms readings with the same cross-process pairing.
   There is a legitimate mechanism (the kernel adds accumulated `run_delay` in bulk at
   schedule time, so a long wait lands in one window), but a FIFO-85 thread sitting
   runnable for 377 ms against 15 idle P-cores is not credible without more evidence.
3. **Frequency rules out causation anyway.** ~266 large WAIT events per 300 s (0.89/s)
   against an xrun rate of 0.033/s. A 27:1 ratio means most large waits produce no
   audible miss.

**Also ruled out, cheaply:** page faults. The Diva audio threads showed `majflt = 0` and
only 77–125 cumulative minor faults, which cannot produce 30 ms. Worth noting separately
that `VmLck = 0` on a 512 MB RSS — Bitwig locks essentially nothing (216 kB in the
engine, 576 kB in the Kontakt host) despite `memlock` being unlimited.

**Quantum 512 does not apply here.** It gives a 10.667 ms deadline against a 29.612 ms
spike. Covering that needs quantum 2048 (42.7 ms), i.e. 43 ms of latency. Note this
29.612 ms reading is genuine, unlike the earlier 32.767 ms, which was a saturated
16-bit counter.

**Where this stops.** Two purpose-built tools and two self-corrections later, every
measurement says the same thing: Diva is expensive, everything placement-related is
already right. The remaining levers are Diva's Accuracy setting (2–4× on exactly the
quantity RUN implicates), fewer instances, or freezing tracks — not more diagnosis.
