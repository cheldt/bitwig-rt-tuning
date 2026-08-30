# Kontakt 7 aborts on load under yabridge — the working directory decides it

Investigated 2026-08-30. Fixed by one `cd` in `start-bitwig.sh`.

## Symptom

Bitwig marks `Kontakt 7.vst3` as `could_not_load_plugin`. `engine.log`:

```
15:26:10.475  About to create a VST 3 plugin instance for … /home/kenoby/.vst3/yabridge/Kontakt 7.vst3
15:27:04.247  Could not read repsonses: End of stream
15:27:04.247  Killing pluginhost process
15:27:04.247  Plugin host process exited with code: 9
```

The 53 s gap is Bitwig waiting on a child that is already gone. With `YABRIDGE_LOG=1` the
plugin side shows what actually happened, one second after instance creation:

```
Bad file descriptor (D:\NIBuild\3rdparty\zeromq-v4.3.4-R4\src\epoll.cpp:100)
```

The plugin loads fine (`Finished initializing …`); it dies during *instance creation*.
Exit code 9 is Bitwig's own `kill`, not the plugin's crash — `yabridge-host` was already
a zombie by then.

## Cause

Kontakt 7 bundles **libzmq 4.3.4**. Its Windows signaler creates the internal socketpair
over **AF_UNIX**, with a path from MSVC `tmpnam()` — `\sNNN.N/socket`, relative to the root
of the **current drive**. Traced with `WINEDEBUG=+winsock`:

```
WSASocketW family AF_UNIX, type SOCK_STREAM …
bind socket 0x50c, addr { family AF_UNIX, path  }, len 2
bind failed, status 0xc000000d.                     ← STATUS_INVALID_PARAMETER
closesocket 0x50c
… one retry, same result …
Bad file descriptor (…epoll.cpp:100)
```

`make_fdpair` returns an invalid fd, `epoll_ctl` rejects it with `EBADF`, and libzmq's
`errno_assert` aborts the process: `wine: Unhandled exception 0x40000015`.

The current directory is inherited from whoever launched the DAW. Any cwd outside the
prefix maps to `Z:\`, whose root is the Linux filesystem root — not writable — so the
`tmpnam()` name comes back empty and the bind gets `len 2`. With a cwd inside
`$WINEPREFIX/drive_c` the same call succeeds:

```
bind socket 0x564, addr { family AF_UNIX, path \s198./socket }, len 15
bind successfully bound to address { family AF_UNIX, path \s198./socket }
```

libzmq removes the `sNNN.N` directories on close, so `drive_c/` does not accumulate them.

## Why standalone "worked"

Kontakt 7 standalone launched from Bottles works because Bottles launches it with a cwd
inside the bottle. Launched by hand from a shell — same runner, same prefix, same
wineserver, Bottles' full environment replicated — it aborts exactly like the plugin.
The wineserver session, `WINENTSYNC`/`WINEFSYNC`, `STAGING_SHARED_MEMORY`,
Wayland-vs-X11, the P-core pinning and the RT priorities were all tested and are all
irrelevant.

## Fix

A wrapper at `~/.local/bin/yabridge-host.exe` that `cd`s into `$WINEPREFIX/drive_c` and
execs `/usr/bin/yabridge-host.exe`. Copy kept as `docs/reference-yabridge-host-wrapper.sh`.

Two things about it are not obvious:

- **It cannot be done in `start-bitwig.sh`.** cwd is inherited across fork/exec, so a `cd`
  before `bitwig-studio` looks like it should reach the plugin hosts — but Bitwig chdirs
  `BitwigAudioEngine` and `BitwigPluginHost` to `~/.BitwigStudio/log` itself, and the crash
  comes back unchanged. Verified 2026-08-30; the launcher now only carries a pointer comment.
- **The wrapper directory also needs `yabridge-host.exe.so` beside it**, as a symlink to
  `/usr/bin/yabridge-host.exe.so`. yabridge resolves the host through `PATH` and rejects a
  directory that has the `.exe` without the `.so` — `yabridgectl status` reports
  `yabridge-host.exe: <not found>` until the symlink is there.

`PATH` is read when a host is spawned, so new plugin instances pick the wrapper up without
restarting Bitwig. A yabridge package update replaces `/usr/bin/yabridge-host.exe` and
leaves the wrapper alone, but re-check the symlink if `yabridge-host.exe.so` ever moves.

## Scope

Only plugins that link libzmq are affected. In this prefix:

| plugin | libzmq | affected |
|---|---|---|
| Kontakt 7 | 4.3.4-R4 | yes |
| Komplete Kontrol | 4.3.4-R4 | yes (same code path, untested) |
| Kontakt 6, FM8 | none | no |
| NTKDaemon (not a plugin) | 4.3.5-R2 | no — and it is the endpoint Kontakt 7 talks to, `tcp://127.0.0.1:7865` |

## Verification

Reproduce the original condition without Bitwig — a host launched from a cwd outside the
prefix, which is what Bitwig gives its plugin hosts:

```bash
cd ~ && WINEPREFIX=/media/nvme1/native_access/Native-Access YABRIDGE_LOG=1 \
  YABRIDGE_DEBUG_FILE=/tmp/yabridge-k7.log \
  carla-single vst3 ~/.vst3/yabridge/"Kontakt 7.vst3"
```

With the wrapper in place the host runs in the prefix and holds the RPC connection:

```
readlink /proc/$(pgrep -x yabridge-host.e)/cwd
  → /media/nvme1/native_access/Native-Access/drive_c
ss -tnp | grep :7865
  → ESTAB 127.0.0.1:60916  127.0.0.1:7865  users:(("yabridge-host.e",…))
grep -c 'Bad file descriptor' /tmp/yabridge-k7.log
  → 0
```

Without it the host is a zombie within a second and the log carries the `epoll.cpp:100`
line. `ps -o stat= -p <pid>` showing `Z` is the quickest tell.

## Also cleaned up

`~/.config/yabridge/yabridge.toml` held a commented `["Kontakt 7.vst3"] isolated = true`.
It was never read — the log says `config from: '<defaults>'` — for two reasons: yabridge
reads `yabridge.toml` from the plugin directory (`~/.vst3/yabridge/`), and `isolated` is not
a yabridge option. The real set, from `strings` on `libyabridge-vst3.so`: `group`,
`hide_daw`, `disable_pipes`, `editor_disable_host_scaling`, `editor_force_dnd`,
`frame_rate`, `vst3_prefer_32bit`. Renamed to `yabridge.toml.unused`.
