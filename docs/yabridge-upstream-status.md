# Which of these workarounds does an upstream fix delete?

Written 2026-09-01, updated 2026-09-04. This repo carries several workarounds whose cause
sits in yabridge or Wine, not in this machine's configuration. This document asks, per
workaround, whether a real fix exists upstream — so the repo stops maintaining local hacks
for problems that are solved, or solvable, elsewhere.

Source citations below are to yabridge at commit **`b580a9f7`** — upstream `master`, which
is what this setup now runs. They were originally written against `945528cd7f898d` on the
`new-wine10-embedding` branch and re-checked against master on 2026-09-04, where every
claim below still holds. They name a file and a symbol rather than a line number, because
the tree moves. Re-check them before relying on any of this.

**None of this was measured.** It was written on a different machine (16 cores, no
`/dev/ntsync`, no yabridge installed), so every claim that needs the i9 is tagged
`UNVERIFIED` and has a check in the last section.

## Summary

| workaround | cause belongs to | real fix | status |
|---|---|---|---|
| `YABRIDGE_LOG=0` (STDERR pipe cost) | yabridge | **`disable_pipes`** | **ships since 3.3.0** — A/B it, then apply |
| `LATE_RT_NAMES` `audio-N` promotion | yabridge | configurable fallback RT priority | none — [issue draft](upstream/yabridge-audio-thread-priority.md) |
| `yabridge-host.exe` cwd wrapper | yabridge, and arguably Wine msvcrt | chdir the Wine host into the prefix | none — [issue draft](upstream/yabridge-host-working-directory.md) |
| FIFO-5 yabridge helpers → E-cores | yabridge | its own `SCHED_RESET_ON_FORK` TODO | none — **doc attribution corrected** |
| `wineserver` renice −10 | Wine architecture | none plausible | upstream reached the same conclusion independently |
| `/etc/udev/rules.d/70-ntsync.rules` | Linux kernel | `.mode = 0666` in `ntsync.c` | **shipped in Linux 6.14 — our rule is probably redundant** |
| yabridge grouping commented out | — | not a workaround; measured harmful | leave as is |

One yes, two no-but-small, two not-yabridge's-problem, and two claims of ours that the
upstream source contradicts.

## First: which yabridge is this — and the branch was never necessary

**Corrected 2026-09-04.** This section previously argued that Wine 11 support depended on
staying on the `new-wine10-embedding` branch, and that upgrading was therefore not a route
to anything. Both halves were wrong.

The setup used to run commit `945528cd7f898d` — 2026-01-11, Juuso Kaitila, *"Fix cursor
offset after moving the plugin window"*, touching `src/wine-host/editor.cpp` — on the
**`new-wine10-embedding`** branch. It now runs upstream `master`, `b580a9f7`
(`5.1.1-57-gb580a9f7`).

- **The branch is merged.** `6d81b184 Merge branch 'new-wine10-embedding'` is in master, so
  master carries the whole Wine 9.22+ embedding rework *plus* four months of fixes on top
  of it. Verified with `git ls-remote https://github.com/robbert-vdh/yabridge.git master`.
- **The Wine caveat is about the release, not about master.** master's README says *"Wine
  9.22 and Wine 10.x currently don't work with yabridge 5.1.1 or below"* and points at a
  development build. A build of master *is* that development build. Wine 11.16 never
  depended on the branch.
- The last tagged release is still **5.1.1, 2024-11-04**, and master's `[Unreleased]`
  section is still open — so *"install the latest release"* remains wrong. *"Build master"*
  is the right answer, and is what this setup does now.

`new-wine10-embedding` was about **window** embedding — the Wine 9.22+ child-window change
that fixed mouse clicks not registering in plugin GUIs, credited in the changelog to Rémi
Bernon. Worth stating plainly because the branch name reads like an architectural
rewrite: it is not. `yabridge-host` still exists, `wineserver` still exists, and the audio
path still makes two cross-process hops per plugin per buffer. Nothing in this repo's
tuning becomes unnecessary because of it, and none of the items below are affected by the
move to master — their code citations were re-checked there.

Related, if GUI responsiveness is ever looked at here: yabridge
[#469](https://github.com/robbert-vdh/yabridge/issues/469) (open, 2026-03-09) reports
Kontakt UI lag specifically on this branch versus Wine 9.21 on master. Since the branch is
now merged, that report applies to master too.

## 1. `YABRIDGE_LOG=0` — a real fix, shipping since 2021

**The workaround.** `start-bitwig.sh` leaves `YABRIDGE_DEBUG_FILE` unset by default:
Wine STDERR lines measured 71.6 ms per 5 s across the plugin hosts with 11 instances
loaded.

**What is actually spending that time.** `wine-stdio` is **yabridge's own thread**, not
Bitwig's. `src/plugin/bridges/common.h` names it —
`pthread_setname_np(pthread_self(), "wine-stdio")` — and it runs the asio loop servicing

```cpp
logger.async_log_pipe_lines(stdout_pipe_, stdout_buffer_, "[Wine STDOUT] ");
logger.async_log_pipe_lines(stderr_pipe_, stderr_buffer_, "[Wine STDERR] ");
```

It appears inside `BitwigPluginHost` because that is the process `libyabridge-vst3.so` is
loaded into, which is why it looked like a Bitwig thread in the profile. Same file also
documents that this thread is deliberately *not* realtime: *"We no longer run this thread
with realtime scheduling because plugins that produce a lot of FIXMEs could in theory
cause dropouts that way."*

**The fix: `disable_pipes`.** Added in yabridge **3.3.0** (2021-06-03) as *"a
compatibility option to redirect the Wine plugin host's STDOUT and STDERR output streams
directly to a file."* In `src/plugin/host-process.cpp` it picks a different spawn
outright:

```cpp
// HACK: If the `disable_pipes` option is enabled, then we'll redirect
//       the plugin's output to a file instead of using pipes to blend
//       it in with the rest of yabridge's output. This is for some
//       reason necessary for ujam's plugins and all other plugins made
//       with Gorilla Engine to function. Otherwise they'll print a
//       nondescriptive `JS_EXEC_FAILED` error message.
config.disable_pipes
    ? child.spawn_child_redirected(*config.disable_pipes)
    : child.spawn_child_piped(stdout_pipe_, stderr_pipe_)
```

No pipes are created, so there is nothing to relay. Note what that comment is: the
option exists because pipes *break* some plugins. It is a fix, not a degradation. Kontakt,
FM8 and Diva are not Gorilla Engine, so the comment does not apply either way here.

**What to write, next to the `.so` — not in `~/.config/yabridge/`**, per the finding
already recorded in [`kontakt7-zmq-crash.md`](kontakt7-zmq-crash.md) (yabridge reads
`yabridge.toml` from the plugin directory; the file in `~/.config` was never read at all):

```toml
# ~/.vst/yabridge/yabridge.toml and ~/.vst3/yabridge/yabridge.toml
["*"]
disable_pipes = "/tmp/yabridge-plugin-output.log"
```

`true` also works and writes to `$XDG_RUNTIME_DIR/yabridge-plugin-output.log`; the
upstream README's own example uses the boolean form (`["Loopcloud*"] disable_pipes = true`).

Two placement rules from the upstream README, both easy to get wrong:

- The file may sit in the plugin's directory **or any of its parent directories** — so
  `~/.vst/yabridge/yabridge.toml` works. `~/.config/yabridge/` does not, because it is not
  a parent of `~/.vst/yabridge/`. (`kontakt7-zmq-crash.md` says "the plugin directory";
  "or any parent" is the fuller rule.)
- *"only the first `yabridge.toml` file found and only the first matching glob pattern
  within that file will be considered."* So a `["*"]` stanza placed above a specific one
  **shadows it entirely** — relevant here, since `reference-yabridge.toml` holds a
  commented `["Kontakt.so"] group` stanza. If grouping is ever re-enabled, `["*"]` must
  come last, or `disable_pipes` has to be repeated in each specific stanza.

**How much this is worth — three caveats, all of them load-bearing.**

- The 71.6 ms was measured with `YABRIDGE_LOG=1`. At today's default Wine still emits
  FIXME and err lines down the same pipe, but **that volume was never measured**. The win
  at the current default is unknown and could be near zero. `UNVERIFIED`.
- The unconditional win is different and better: **the trade-off disappears**. Debug
  output stops costing anything, so `YABRIDGE_LOG` stops being a switch to remember.
- Whether the `wine-stdio` thread is still *created* when pipes are disabled is not
  determinable from the source read here. The per-line work goes away; the thread may or
  may not.

Also worth keeping in mind: `steer-threads.sh` already exiles this thread to the E-cores
(`BitwigPluginHos` is in `PROC_PATTERNS`), so its cost is off the audio cores today
regardless.

**Follow-up, gated on the A/B:** update `docs/reference-yabridge.toml`, and consider
flipping the `YABRIDGE_LOG` default in `start-bitwig.sh`.

## 2. `LATE_RT_NAMES` — no upstream fix, but the mechanism is now exact

**The workaround.** `tools/steer-threads.sh` promotes threads named `^audio-[0-9]+$`
inside `^yabridge-host\.e$` to the P-cores *by name*, because the rtprio rule reads them
as non-audio during a plugin load.

**The mechanism, from the source.** More precise than the comment currently in
`steer-threads.sh`:

- `src/common/utils.h` declares
  `bool set_realtime_priority(bool sched_fifo, int priority = 5) noexcept`. The default
  is **5**, and the doc comment says why: *"The exact value usually doesn't really matter
  unless there are a lot of other active `SCHED_FIFO` background tasks. We'll use 5 as a
  default, but we'll periodically copy the priority set by the host on the audio
  threads."*
- `src/wine-host/bridges/vst3.cpp`, in `register_object_instance()`, the dedicated audio
  handler thread starts with `set_realtime_priority(true);` — i.e. `SCHED_FIFO` **5** —
  and only *then* names itself:

  ```cpp
  set_realtime_priority(true);
  const std::string thread_name = "audio-" + std::to_string(instance_id);
  pthread_setname_np(pthread_self(), thread_name.c_str());
  ```

- The host's real priority reaches that thread only through
  `request.new_realtime_priority`, applied in the `YaAudioProcessor::Process` handler:

  ```cpp
  if (request.new_realtime_priority) {
      set_realtime_priority(true, *request.new_realtime_priority);
  }
  ```

- And `src/plugin/bridges/vst3-impls/plugin-proxy.cpp` populates that field **only inside
  `Vst3PluginProxyImpl::process()`**:

  ```cpp
  // We'll synchronize the scheduling priority of the audio thread on the Wine
  // plugin host with that of the host's audio thread every once in a while
  std::optional<int> new_realtime_priority = std::nullopt;
  time_t now = time(nullptr);
  if (now > last_audio_thread_priority_synchronization_ +
                audio_thread_priority_synchronization_interval) {
      new_realtime_priority = get_realtime_priority();
      last_audio_thread_priority_synchronization_ = now;
  }
  ```

  with `audio_thread_priority_synchronization_interval = 10` in `src/common/utils.h`.
  `setActive()`, `setProcessing()` and `setupProcessing()` never carry the priority.

**So the window is exactly thread creation → first `process()` call.** Kontakt's
`setActive(true)` — the expensive activation this repo measured at 1.619 ms on cpu31
against a 0.028 ms median — falls inside it by construction, because it is dispatched on
the `audio-N` thread but is not a `Process` request. That corroborates the timeline in
[`dsp-spike-investigation.md`](dsp-spike-investigation.md) down to the ordering: FIFO 5 on
16-31 at t=16.33, activation at t=17.128, FIFO 85 on 0-15 at t=17.33.

**No fix, and no knob.** The `5` is a hardcoded default argument, not configurable by env
var or `yabridge.toml`. No upstream issue covers it.

**The workaround is also the right local policy**, which is worth saying: the thread name
is set at essentially the same instant as the FIFO 5, one statement later. Name-matching
is the earliest signal that exists. There is nothing better to key on.

Upstream ask drafted in
[`upstream/yabridge-audio-thread-priority.md`](upstream/yabridge-audio-thread-priority.md).

## 3. The `yabridge-host.exe` cwd wrapper — no upstream fix, two viable patch shapes

**The workaround.** `~/.local/bin/yabridge-host.exe` `cd`s into `$WINEPREFIX/drive_c` so
Kontakt 7's bundled libzmq 4.3.4 can bind its AF_UNIX signaler socket. Full trace and
cause in [`kontakt7-zmq-crash.md`](kontakt7-zmq-crash.md).

**Confirmed: yabridge has no working-directory support anywhere.**

- `src/common/process.h`'s `Process` exposes `arg()`, `environment()`,
  `spawn_get_stdout_line()`, `spawn_get_status()`, `spawn_child_piped()` and
  `spawn_child_redirected()`. There is no `start_dir` and no chdir.
- `src/wine-host/host.cpp`'s `main()` contains no `chdir` or `SetCurrentDirectory`, and no
  mention of `Z:` or `drive_c`.
- A tracker search for `"working directory" OR "current directory" OR chdir OR drive_c OR
  Z:` returns nothing related. The Kontakt 7 issues that do exist — #338 (Bitwig 5,
  ShellExecuteEx), #356 (DXVK), #372 (rack) — are different faults.
- libzmq [#4084](https://github.com/zeromq/libzmq/issues/4084) looks close ("Assertion
  failure in epoll.cpp due to failing AF_UNIX bind()") but is Windows 10 1803's own
  AF_UNIX implementation, not this.

**Two independent upstream avenues.**

- **yabridge.** Have the Wine-side host set its current directory to `C:\` at startup — a
  few lines in `src/wine-host/host.cpp::main()`, needing no prefix-path resolution.
  Cleaner than adding `start_dir` to `Process` on the plugin side. The framing that makes
  it a general fix rather than a Kontakt patch: a Wine process that inherits a cwd
  outside the prefix maps to `Z:\`, whose root is `/` and is not writable, so *any*
  plugin writing relative to the current drive root breaks. Drafted in
  [`upstream/yabridge-host-working-directory.md`](upstream/yabridge-host-working-directory.md).
- **Wine.** The `WINEDEBUG=+winsock` trace in `kontakt7-zmq-crash.md` shows `tmpnam()`
  coming back **empty** — `bind socket 0x50c, addr { family AF_UNIX, path  }, len 2`.
  msvcrt returning an unusable empty name when the current drive root is not writable is
  arguably a Wine bug in its own right. No winehq bug was found for it. This is a
  separate report and should not be folded into the yabridge one.

**Until either lands the wrapper is load-bearing**, including the part that is easy to
lose. As of 2026-09-04 that part has changed shape: yabridge is built from source rather
than packaged, and the `libyabridge-*.so` files — not `yabridge-host.exe.so` — are what must
sit next to the wrapper, because each wrapped plugin is a chainloader that loads them from
the directory of the first `yabridge-host.exe` on `PATH`. See the layout block in
`kontakt7-zmq-crash.md`.

## 4. Correction — the 87 `SCHED_FIFO` 5 threads are yabridge's, not Wine's

`tools/steer-threads.sh` (header) and `dsp-spike-investigation.md` (the "Why 2 × 4"
discussion, the thread census, and "Fix: split by realtime priority") all say that
**"Wine's priority mapping puts every plugin-spawned thread"** at FIFO 5. The upstream
source says that is not where it comes from.

- `set_realtime_priority()` is `sched_setscheduler(0, SCHED_FIFO, &params)` — pid 0 is the
  *calling thread* — and a new thread inherits its creator's policy and priority.
- `src/wine-host/bridges/vst3.cpp` wraps plugin construction in it:

  ```cpp
  set_realtime_priority(true);
  switch (request.requested_interface) { /* plugin factory instantiation */ }
  set_realtime_priority(false);
  ```

  So every thread a plugin creates during construction is born `SCHED_FIFO` 5. The same
  applies to threads a plugin creates later from the `audio-N` thread, which is also
  FIFO by then.
- `src/common/utils.h` carries the matching TODO, which makes the inheritance explicit
  and intentional: *"At some point, consider using `SCHED_RESET_ON_FORK` instead of
  manually disabling this when we don't want realtime scheduling to propagate. That would
  require a bit of careful analysis because we do want it to propagate to a Windows
  plugin's audio threads, and I don't think there's a way to go back once you've set
  `SCHED_RESET_ON_FORK`."*

Upstream Wine does not touch the Unix scheduler for `SetThreadPriority`. wine-staging's
`server-Realtime_Priority` patchset *would* do roughly what our docs describe — but only
when `STAGING_RT_PRIORITY_BASE` is set, and it is not set here.

**Why this is not a nitpick.** If that TODO is ever acted on, those threads become
`SCHED_OTHER`, and the rule this repo fought hardest for — *"split on **priority**, not
policy"* — stops being necessary. The rule stays correct and harmless either way, but the
reason it exists would have moved from Wine to yabridge and then disappeared. Anyone
re-deriving the tuning later needs to know that.

Two doc edits are pending on the check in the last section:
`tools/steer-threads.sh` header, and the three passages in `dsp-spike-investigation.md`.

## 5. Correction — the ntsync udev rule is probably redundant

`docs/reference-udev-70-ntsync.rules` exists because `/dev/ntsync` came up `root:root
0600` and Wine fell back to fsync without saying so.

Mainline `drivers/misc/ntsync.c` carries **`.mode = 0666`** on its `miscdevice` — a patch
by Mike Lothian, acked by the driver's author Elizabeth Figura, merged for **Linux 6.14**
(February 2025) for exactly this reason: to make ntsync usable out of the box with no
udev rule. The rationale on the list was that world-readable/writable is fine here
because this is not real hardware, and objects created on one file descriptor can only be
used with objects from that same instance. This box runs **7.2.2-cachyos**, well past
6.14.

So either the `0600` observation predates the current kernel, or something on this system
resets the node. Both outcomes are worth writing down:

- **Redundant.** The rule can be dropped — or kept and re-labelled honestly, since
  `MODE="0660", GROUP="audio"` *tightens* the kernel's world-writable default rather than
  enabling anything. That is defensible, but it is not what our doc claims it does.
- **Still needed.** Then the interesting question is what is overriding the kernel's
  mode, and that answer belongs in the doc.

Either way, `start-bitwig.sh`'s `[ -r /dev/ntsync ]` check and its specific error
messages stay valuable. They are what caught the silent fsync fallback in the first place,
and they still catch the module-not-loaded case.

## 6. `wineserver` renice −10 — no fix, and upstream reached the same conclusion

No upstream fix, and none plausible: `wineserver` is single-threaded by design and every
Wine process calls into it synchronously. ntsync — already in use — is the real
mitigation, since it removes most of the synchronisation round-trips that would otherwise
reach the server.

Worth recording because it independently confirms a call this repo made on its own.
`src/common/utils.h`, on `set_realtime_priority`:

> *"Set the scheduling policy to `SCHED_FIFO` with priority 5 for this process. We
> explicitly don't do this for wineserver itself since from my testing that can actually
> increase latencies."*

Which is the same conclusion as ours: *"It is deliberately **not** made `SCHED_FIFO`: it
is single-threaded and spinning there would be worse."* Two independent measurements,
same answer. `renice -10` stands.

**A hazard to record while in the area.** wine-staging's `server-Realtime_Priority`
patchset — present in TkG staging builds like this one — exposes two environment
variables, and they are not symmetric:

- `STAGING_RT_PRIORITY_SERVER` makes `wineserver` `SCHED_FIFO`. That is the exact thing
  both this repo and yabridge upstream measured as harmful. A/B it if curious; do not
  assume it helps.
- **`STAGING_RT_PRIORITY_BASE` would break the rtprio split outright.** It raises the
  base priority of *all* programs running in Wine, so `RT_MIN=50` would stop
  discriminating between audio and non-audio threads and `steer-threads.sh` would send
  the whole Wine tree back to the P-cores — reproducing the original fault. Anyone
  working from a generic "Wine realtime priority" tuning list needs this warning.

## 7. Two native yabridge knobs we do not use — flagged, not recommended

Neither replaces a workaround. Both are cheap A/Bs never run here.

- **`frame_rate`** (`yabridge.toml`, default 60) — *"The rate at which Win32 events are
  being handled and usually also the refresh rate of a plugin's editor GUI."* Lowering it
  cuts the host's main-thread cost. Every measurement in this repo was taken with plugin
  GUIs closed, so the expected effect is small. Unmeasured.
- **`YABRIDGE_NO_WATCHDOG`** — mentioned only because
  `measurements/2026-08-29_thread-profile_8x1-instances.log` shows
  `yabridge-host/ProcessMonitor` at 293 ms against the audio thread's 98 ms at idle.
  **The attribution is unverified and probably wrong**: yabridge names its own watchdog
  thread `"watchdog"` (`src/plugin/bridges/common.h`), not `ProcessMonitor`, so this is
  plausibly a Native Instruments thread and the env var would do nothing. Resolve it with
  a `comm` census before reaching for the knob.

## Verification

All of this runs on the i9, not on the machine this was written on. Each item turns one
`UNVERIFIED` tag into a number.

1. **`disable_pipes`.** Add the stanza to `~/.vst/yabridge/yabridge.toml` and
   `~/.vst3/yabridge/yabridge.toml`. First confirm it is read at all: with
   `YABRIDGE_LOG=1`, check that the log's `config from:` line names the plugin-directory
   file and not `<defaults>` — the failure mode `kontakt7-zmq-crash.md` already
   documents. Then, with 11 instances loaded, `tools/catch-spike.py` for the `wine-stdio`
   threads' CPU per 5 s, and `tools/measure-xruns.sh` for at least 300 s per arm (the
   README's warning about 60–90 s glitch gaps applies). Run both arms at
   `YABRIDGE_LOG=1` to size the effect, then at `YABRIDGE_LOG=0` to size it at today's
   default.
2. **The FIFO-5 attribution.** ~~`env | grep STAGING_RT_PRIORITY` — expect empty — and
   `chrt -p <tid>` on a Kontakt-spawned worker, expect `SCHED_FIFO` 5. Together those
   rule out wine-staging's patchset and leave yabridge's inheritance as the only source.
   Then make the two doc edits in item 4.~~ **DONE** — doc edits applied to
   `tools/steer-threads.sh` header and `docs/dsp-spike-investigation.md` (three passages:
   thread census, "Fix: split by realtime priority" section, and Kontakt MP discussion).
3. **The ntsync rule.** Move `/etc/udev/rules.d/70-ntsync.rules` aside,
   `udevadm control --reload`, reboot, then `stat -c '%a %U %G' /dev/ntsync`. `666` means
   redundant. If it is not 666, `udevadm info --attribute-walk /dev/ntsync` and
   `grep -r ntsync /usr/lib/udev/rules.d/` to find what is overriding the kernel.
   Confirm the outcome against `start-bitwig.sh`'s own `Wine sync: ntsync` line.
4. **`ProcessMonitor`.** With a session up:

   ```sh
   for t in /proc/$(pgrep -x yabridge-host.e | head -1)/task/*; do cat "$t/comm"; done
   ```

   Check whether `ProcessMonitor` sits alongside the yabridge-owned names (`audio-N`,
   `watchdog`, `wine-stdio`). If it does not, it is NI's and `YABRIDGE_NO_WATCHDOG` is
   irrelevant.
5. **This document.** Every upstream claim cites a file and a symbol, not a line number.
   Re-check each against commit `b580a9f7` before acting on it — master moves.
