#!/bin/bash
# Standalone one-shot tweaks, for when you want the power settings without a full
# session. Nothing here is restored -- that is the whole difference from
# start-bitwig.sh, which records a baseline and puts it all back on exit. Everything
# below survives until you undo it or reboot.
#
# Usage: ./perf.sh
set -euo pipefail

sudo -v || exit 1

sudo cpupower frequency-set -g performance

nvidia-settings -a '[gpu:0]/GpuPowerMizerMode=1' >/dev/null 2>&1 || true

sudo sysctl vm.swappiness=10

sudo modprobe ntsync

# SMT: opt-in, because turning it off renumbers every CPU on the machine and this
# repo hard-codes the numbering everywhere.
#
# With SMT on, the i9-13900K presents 32 CPUs: 0-15 are the P-cores' two threads
# each, 16-31 the E-cores. Off, it presents 24: 0-7 P, 8-23 E. So PCORES=0-15 would
# then span the P-cores *and* the first eight E-cores, and ECORES=16-31 would point
# at eight CPUs that no longer exist -- the placement the whole investigation is
# about, silently inverted. Nothing in this repo reads the topology at runtime.
#
# It is also not restored, and start-bitwig.sh never touches SMT, so a session
# started after this one runs against the renumbered machine with no warning.
#
# Set PERF_DISABLE_SMT=1 to do it anyway, and fix the core ranges in
# start-bitwig.sh and tools/steer-threads.sh before running a session.
if [ "${PERF_DISABLE_SMT:-0}" -eq 1 ]; then
    echo "disabling SMT: CPU numbering becomes 0-7 P / 8-23 E." >&2
    echo "  PCORES/ECORES in start-bitwig.sh and tools/steer-threads.sh are now wrong." >&2
    echo "  Undo with: echo on | sudo tee /sys/devices/system/cpu/smt/control" >&2
    echo off | sudo tee /sys/devices/system/cpu/smt/control >/dev/null
fi
