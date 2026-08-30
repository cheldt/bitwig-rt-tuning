#!/bin/sh
# Wrapper around /usr/bin/yabridge-host.exe that moves the working directory into the
# Wine prefix before the plugin host starts.
#
# Kontakt 7 and Komplete Kontrol bundle libzmq 4.3.4, whose Windows signaler creates its
# socketpair over AF_UNIX using a path from MSVC tmpnam() -- "\sNNN.N/socket", relative to
# the root of the *current drive*. A working directory outside the prefix maps to Z:\,
# whose root is the Linux filesystem root and is not writable, so the bind fails with
# STATUS_INVALID_PARAMETER, make_fdpair returns an invalid fd, and libzmq's errno_assert
# aborts the whole host:
#
#   Bad file descriptor (D:\NIBuild\3rdparty\zeromq-v4.3.4-R4\src\epoll.cpp:100)
#
# The DAW then waits on a dead child; Bitwig gives up after ~53 s with "Plugin host died:
# Could not read repsonses" and marks the plug-in could_not_load_plugin.
#
# This has to happen here rather than in the DAW's launcher: Bitwig chdirs its own children
# to ~/.BitwigStudio/log, so a cwd set before `bitwig-studio` never reaches BitwigPluginHost.
#
# libzmq removes the sNNN.N directories on close, so drive_c/ does not accumulate them.
# See ~/dev/bitwig-rt-tuning/docs/kontakt7-zmq-crash.md.

if [ -w "$WINEPREFIX/drive_c" ]; then
    cd "$WINEPREFIX/drive_c" || exit 1
fi

exec /usr/bin/yabridge-host.exe "$@"
