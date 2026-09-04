# Upstream draft — make the Wine-side audio thread's fallback realtime priority configurable

Status: **draft, not filed.** Target: `robbert-vdh/yabridge`, feature request.
Written 2026-09-01 against commit `945528cd7f898d` (`new-wine10-embedding`).
Re-checked 2026-09-04 against upstream `master` (`b580a9f7`), which now has that
branch merged (`6d81b184`) and is what this setup runs: the hardcoded fallback is still there — `set_realtime_priority(bool sched_fifo, int priority = 5)` in `src/common/utils.h`, and no caller passes a priority, so the
premise holds. Update the citations to `b580a9f7` when filing.

Before filing: re-check the code citations against current `master`/branch, and re-run
the measurement so the numbers quoted are from the build being reported.

---

## Title

Make the audio thread's fallback `SCHED_FIFO` priority configurable (currently hardcoded 5)

## Body

### Summary

The Wine-side audio handler thread is created at `SCHED_FIFO` **5** and only learns the
host's real audio priority when the first `process()` call arrives. Everything that runs
on that thread before then — `setupProcessing()`, `setActive(true)`, `setProcessing(true)`
— runs at priority 5. For Kontakt that window contains the expensive activation, and on a
system that uses realtime priority as a scheduling signal it lands in the wrong place.

I am not asking for the window to be closed — that looks harder than it sounds, see below.
I am asking for the fallback `5` to be configurable, which is a much smaller change and
enough to fix it from the outside.

### Where the 5 comes from

`src/common/utils.h`:

```cpp
bool set_realtime_priority(bool sched_fifo, int priority = 5) noexcept;
```

with the doc comment: *"The exact value usually doesn't really matter unless there are a
lot of other active `SCHED_FIFO` background tasks. We'll use 5 as a default, but we'll
periodically copy the priority set by the host on the audio threads."*

`src/wine-host/bridges/vst3.cpp`, in `register_object_instance()` — the audio handler
thread takes the default, then names itself:

```cpp
set_realtime_priority(true);
const std::string thread_name = "audio-" + std::to_string(instance_id);
pthread_setname_np(pthread_self(), thread_name.c_str());
```

The host's priority arrives only via the `Process` request:

```cpp
// src/wine-host/bridges/vst3.cpp, YaAudioProcessor::Process handler
if (request.new_realtime_priority) {
    set_realtime_priority(true, *request.new_realtime_priority);
}
```

and `src/plugin/bridges/vst3-impls/plugin-proxy.cpp` populates that field **only** in
`Vst3PluginProxyImpl::process()`, gated on
`audio_thread_priority_synchronization_interval` (= 10). `setActive()`,
`setProcessing()` and `setupProcessing()` never carry it.

So the priority-5 window is exactly *thread creation → first `process()` call*.

### Why it matters here

This is a hybrid CPU (Intel i9-13900K: 16 P-core threads at 5.5–5.8 GHz, 16 E-cores at
4.3 GHz with much lower IPC). Bitwig's process tree is pinned to the P-cores and a helper
re-splits it by realtime priority: threads at rtprio ≥ 50 keep the P-cores, everything
else goes to the E-cores. That is the split that made the session viable at all — it took
Bitwig's Load MAX from 14.8 ms to 0.9 ms against a 5.333 ms deadline, purely by placement,
at about 6 % aggregate load per P-core.

The `audio-N` thread at FIFO 5 reads as "not an audio thread" to that rule, so it starts
on an E-core. Measured at 2 ms sampling resolution, one Kontakt 6 loading cold:

| | |
|---|---|
| `audio-0` FIFO 5 on E-cores | t = 16.33 |
| activation callback lands | t = 17.128 |
| `audio-0` becomes FIFO 85 on P-cores | t = 17.33 |

That activation burned **1.619 ms on-CPU in a single 2 ms window against a 0.028 ms
median**, on an E-core. Bitwig's own audio thread never exceeded 0.353 ms in the same
window — it was blocked waiting on the plugin, and reported the sum as Load MAX 1.861 ms.

Promoting the thread by name before activation brought that to 0.945 ms, and Load MAX
1.861 → **1.192 ms**. Quantum unchanged at 256/48000.

Full write-up, including the sampling-rate finding (at 20 ms a window holds ~3.75
callbacks and averages the expensive one away, which is why this was invisible for a
while): <https://github.com/cheldt/bitwig-rt-tuning/blob/main/docs/dsp-spike-investigation.md>
(confirm the repo is public before filing — otherwise paste the relevant section inline).

### The ask

**Preferred: make the fallback priority configurable.** An environment variable
(`YABRIDGE_FALLBACK_RT_PRIORITY`) or a `yabridge.toml` key would both work. Then a tuned
system can have the thread created above its own realtime threshold and the window stops
mattering, without yabridge having to change any of its priority logic. Roughly: thread
one value through to `set_realtime_priority`'s default at the call site in
`register_object_instance()`, plus a README entry.

Happy to send a PR for this shape if the interface is acceptable — mainly I do not want to
guess at whether you would rather have it as an env var or a config key.

**Alternative, if you would rather close the window properly:** carry the last known host
audio priority on `SetupProcessing` / `SetActive` / `SetProcessing` requests as well as
`Process`. Worth flagging that the obvious implementation does not work:
`get_realtime_priority()` reads the *calling* thread, and `setActive()` is not called from
the host's audio thread, so it would return `nullopt` there. It needs a value cached from
a previous `process()` — which means the first instance created in a session still has
nothing to send. That is why I am asking for the configurable fallback instead.

### Not asking for

Raising the default from 5 for everyone. The reasoning in the existing comment is sound
for a normal setup; this only bites when realtime priority is being used as a placement
signal.

### Environment

- yabridge `5.1.1-57-gb580a9f7` (upstream `master`)
- Wine 11.16 TkG staging, ntsync
- Bitwig Studio, plugin sandboxing "By Plugin"
- Kontakt 6 / Kontakt 7 / FM8 via yabridge
- Linux 7.2.2 (CachyOS, RT/BORE), PipeWire 1.6.8, quantum 256/48000
