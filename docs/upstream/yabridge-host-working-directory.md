# Upstream draft — give the Wine plugin host a working directory inside the prefix

Status: **draft, not filed.** Target: `robbert-vdh/yabridge`, bug report / small feature.
Written 2026-09-01 against commit `945528cd7f898d` (`new-wine10-embedding`).
Re-checked 2026-09-04 against upstream `master` (`b580a9f7`), which now has that
branch merged (`6d81b184`) and is what this setup runs: still no `chdir`, `current_path` or `SetCurrentDirectory` anywhere under `src/wine-host/` or `src/plugin/`, so the
premise holds. Update the citations to `b580a9f7` when filing.

Before filing: re-check the code citations, and re-run the reproduction on the build being
reported. A separate Wine bug (msvcrt `tmpnam()`) is noted at the end — file that
independently, not as part of this.

---

## Title

Wine plugin host inherits the DAW's working directory, which maps to `Z:\` and breaks plugins that write relative to the current drive root

## Body

### Summary

`yabridge-host.exe` inherits its working directory from the DAW. When that directory is
outside the Wine prefix it maps to `Z:\`, whose root is the Linux filesystem root and is
not writable. Any plugin that creates a file or socket relative to the *current drive
root* then fails, and because the failure is inside the plugin it surfaces as an
unexplained host crash.

Kontakt 7 and Komplete Kontrol hit this deterministically. Setting the host's working
directory to `C:\` fixes it completely. yabridge currently has no way to do that —
`src/common/process.h`'s `Process` has no `start_dir`, and `src/wine-host/host.cpp`'s
`main()` never touches the current directory — so it needs a wrapper script on `PATH`.

### What the failure looks like

Bitwig marks `Kontakt 7.vst3` as `could_not_load_plugin`:

```
15:26:10.475  About to create a VST 3 plugin instance for … /…/.vst3/yabridge/Kontakt 7.vst3
15:27:04.247  Could not read repsonses: End of stream
15:27:04.247  Killing pluginhost process
15:27:04.247  Plugin host process exited with code: 9
```

The 53 s gap is the DAW waiting on a child that is already gone; exit code 9 is the DAW's
own `kill`, not the plugin's crash. With `YABRIDGE_DEBUG_FILE` set, the plugin side shows
the real event one second after instance creation:

```
Bad file descriptor (D:\NIBuild\3rdparty\zeromq-v4.3.4-R4\src\epoll.cpp:100)
```

The plugin loads fine (`Finished initializing …`) and dies during *instance creation*.

### Cause

Kontakt 7 bundles libzmq 4.3.4. Its Windows signaler creates its internal socketpair over
`AF_UNIX`, with a path from MSVC `tmpnam()` — `\sNNN.N/socket`, relative to the root of
the **current drive**. `WINEDEBUG=+winsock`, with the host's cwd outside the prefix:

```
WSASocketW family AF_UNIX, type SOCK_STREAM …
bind socket 0x50c, addr { family AF_UNIX, path  }, len 2
bind failed, status 0xc000000d.                     ← STATUS_INVALID_PARAMETER
closesocket 0x50c
… one retry, same result …
Bad file descriptor (…epoll.cpp:100)
```

`make_fdpair` returns an invalid fd, `epoll_ctl` rejects it with `EBADF`, and libzmq's
`errno_assert` aborts the process (`wine: Unhandled exception 0x40000015`).

With a cwd inside `$WINEPREFIX/drive_c` the same call succeeds:

```
bind socket 0x564, addr { family AF_UNIX, path \s198./socket }, len 15
bind successfully bound to address { family AF_UNIX, path \s198./socket }
```

libzmq removes the `sNNN.N` directories on close, so `drive_c/` does not accumulate them.

Two things that make this look like a Kontakt bug when it is not:

- **Standalone "works".** Kontakt 7 standalone launched from Bottles works because Bottles
  launches it with a cwd inside the bottle. Launched by hand from a shell — same runner,
  same prefix, same wineserver, Bottles' environment replicated — it aborts identically.
- **Everything else was ruled out**, each tested separately: the wineserver session,
  `WINENTSYNC`/`WINEFSYNC`, `STAGING_SHARED_MEMORY`, Wayland vs X11, CPU pinning, and
  realtime priorities. The working directory is the only variable that changes the outcome.

### Reproduction

```sh
cd ~ && WINEPREFIX=/path/to/prefix YABRIDGE_DEBUG_FILE=/tmp/yabridge-k7.log \
  carla-single vst3 ~/.vst3/yabridge/"Kontakt 7.vst3"
```

`ps -o stat= -p $(pgrep -x yabridge-host.e)` shows `Z` within a second, and the log carries
the `epoll.cpp:100` line. Any cwd outside the prefix reproduces it; that is what a DAW
gives its plugin hosts.

### Why this cannot be worked around in the DAW's launcher

The working directory is inherited across fork/exec, so a `cd` before launching the DAW
looks like it should reach the plugin hosts. It does not: Bitwig chdirs
`BitwigAudioEngine` and `BitwigPluginHost` to `~/.BitwigStudio/log` itself, and the crash
comes back unchanged. Verified. The only place left is between yabridge and the Wine host.

### Current workaround

A wrapper that shadows the real host on `PATH`, at `~/.local/bin/yabridge-host.exe`:

```sh
#!/bin/sh
if [ -w "$WINEPREFIX/drive_c" ]; then
    cd "$WINEPREFIX/drive_c" || exit 1
fi
exec "$HOME/.local/share/yabridge/yabridge-host.exe" "$@"
```

The fragile part is that shadowing the host's name on `PATH` also moves where yabridge
looks for everything else. Each wrapped plugin is a chainloader that takes the directory of
the *first* `yabridge-host.exe` on `PATH` and `dlopen`s `libyabridge-vst3.so` from there
(`src/chainloader/utils.h`) — so the libraries have to be duplicated or symlinked next to
the wrapper, and `find_plugin_library`'s fallback to
`${XDG_DATA_HOME:-$HOME/.local/share}/yabridge` never gets a chance, because the search
already matched. A user who wraps the host binary the obvious way and puts everything else
in the standard location gets a plugin that fails to load, with the real host sitting right
there. That is a second reason to do this inside yabridge rather than around it.

### The ask

Set the Wine host's current directory at startup, in `src/wine-host/host.cpp::main()`.
`C:\` needs no prefix-path resolution and is always present, so it is a few lines and no
new configuration.

That seems better than adding a `start_dir` to `Process` on the plugin side, which would
need the prefix path resolved first — but I do not know whether there is a reason the host
should keep the DAW's cwd, so I have not sent a patch. Happy to if the approach is
acceptable.

Framing it as general rather than Kontakt-specific: the current behaviour means every
Wine-hosted plugin runs with `Z:\` as its current drive, i.e. `/`. libzmq is one caller
that trips over it; anything using `tmpnam()`, `_mktemp`, or a relative path from the drive
root has the same exposure. `C:\` is what a plugin would see on Windows.

### Scope observed here

| plugin | libzmq | affected |
|---|---|---|
| Kontakt 7 | 4.3.4-R4 | yes |
| Komplete Kontrol | 4.3.4-R4 | yes — same code path, untested |
| Kontakt 6, FM8 | none | no |

### Environment

- yabridge `5.1.1-57-gb580a9f7` (upstream `master`)
- Wine 11.16 TkG staging, ntsync
- Bitwig Studio 5.x and `carla-single` (both reproduce)
- Linux 7.2.4 (CachyOS)

---

## Separate Wine report — do not fold into the above

The trace shows `bind … { family AF_UNIX, path  }, len 2` — msvcrt's `tmpnam()` returned an
**empty** name rather than failing or returning something usable, when the current drive
root is not writable. That is arguably a Wine bug independent of yabridge: on Windows
`tmpnam()` returns `NULL` if it cannot create the name, and libzmq checks for that; an
empty string passes the check and produces the invalid bind instead.

No existing winehq bug was found for it. Worth a separate report against `msvcrt`, with
the same `+winsock` trace, because fixing it would resolve this class of failure for every
Wine caller — not just yabridge-hosted plugins.
