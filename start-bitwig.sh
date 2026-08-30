#!/bin/bash
# Pro-audio session: tune the system for low-latency work, run Bitwig, restore on exit.
#
# Usage: ./start-bitwig.sh            start session
#        ./start-bitwig.sh --restore  clean up after a crash that skipped the trap

set -uo pipefail

export WINEPREFIX=/media/nvme1/native_access/Native-Access
# ntsync (the mainlined NT-sync kernel driver) is faster and more correct than
# fsync, and this prefix's runner is a TkG build made for it. Wine auto-detects
# /dev/ntsync and prefers it, falling back to fsync when the node is missing or
# unreadable -- so WINEFSYNC stays set as the fallback.
#
# ntsync is loaded at boot by /usr/lib/modules-load.d/ntsync.conf. Do NOT rmmod it
# on exit: the previous version of this script did, which unloaded it system-wide
# and silently downgraded every Wine process on the machine to fsync until reboot.
export WINEFSYNC=1

export PIPEWIRE_QUANTUM=256/48000

EE_UNIT=app-com.github.wwmm.easyeffects@autostart.service

# i9-13900K is hybrid: 0-15 are the P-core threads (5.5-5.8 GHz), 16-31 the E-cores
# (4.3 GHz, much lower IPC). An audio thread that lands on an E-core takes roughly
# twice as long, which is what shows up as Load MAX >> Load AVG in Bitwig.
PCORE_FIRST=0
PCORE_LAST=15
PCORES="$PCORE_FIRST-$PCORE_LAST"
ECORES=16-31

# snd_hdspe (RME AIO Pro) interrupt goes to a P-core; the two highest-rate noisy
# interrupts get pushed to the E-cores. nvidia alone fires several million times a
# session and is not threaded, so it preempts whatever core it lands on.
AUDIO_IRQ=16
AUDIO_IRQ_CPU=2
NOISY_IRQS=(129 211)   # xhci_hcd, nvidia

# Idle states to disable on the P-cores: C2_ACPI (127us) and C3_ACPI (1048us) exit
# latency. The deadline at 256/48000 is 5.333ms, so a C3 wake-up eats ~20% of it.
# 'performance' governor does not prevent C-state entry.
DEEP_CSTATES=(2 3)

# Kontakt's samples are RAM-resident today, so the queue scheduler is not on the audio
# path -- see docs/dsp-spike-investigation.md. That holds only while a library fits in
# 31GB. 'none' bypasses kernel queueing, so a library large enough to stream from disk
# (Kontakt DFD) does not queue behind whatever else the drive is doing. Session-scoped:
# the drives go back to their boot value on exit. SATA SSDs are left alone -- single-queue
# AHCI does not benefit from 'none'.
NVME_DEVS=(nvme0n1 nvme1n1 nvme2n1)

# Pinning Bitwig to the P-cores halves Load AVG (E-cores are ~half the IPC), but the
# mask is inherited by the whole tree, so on its own it also confines every non-audio
# thread -- the JVM UI, wineserver, the NI services -- to the same 16 cores as the
# audio threads. STEER_THREADS below undoes that half of it. Set PIN_BITWIG=0 to launch
# without any pinning and A/B the peaks. Pinning of pipewire and the IRQs is
# unaffected: that serves wake-up jitter, a separate and already-fixed problem.
PIN_BITWIG=${PIN_BITWIG:-1}

# Re-split the tree once Bitwig is up: audio threads (rtprio >= 50) stay on the
# P-cores, every other thread goes to the E-cores. Without this the P-cores carry 121
# realtime threads *and* 412 non-realtime ones, and wineserver -- which every Wine
# process calls into synchronously -- is starved behind the audio threads. Measured
# 2026-08-29 with 9x FM8 loaded: P-cores 95 % busy of 1600 %, E-cores 9 %.
# See tools/steer-threads.sh and docs/dsp-spike-investigation.md.
STEER_THREADS=${STEER_THREADS:-1}
STEER_INTERVAL=${STEER_INTERVAL:-15}
STEER_PID=""

# readlink -f first: this is normally invoked through a symlink in ~/.local/bin, and
# dirname on the symlink path yields the link's directory, not the repo. That silently
# broke the tools/steer-threads.sh launch below -- the session pinned Bitwig to the
# P-cores and then never steered anything off them, which is the one combination
# docs/dsp-spike-investigation.md calls harmful.
SCRIPT_DIR=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
STEER_SH="$SCRIPT_DIR/tools/steer-threads.sh"
STATE_FILE="${XDG_RUNTIME_DIR:-/tmp}/start-bitwig.state"

restored=0
EE_WAS_RUNNING=0
SUDO_KEEPALIVE=""

# --- helpers ----------------------------------------------------------------

# set_cstate <state> <1=disable|0=enable>
set_cstate() {
    local state=$1 want=$2 c

    if [ "$want" -eq 1 ]; then
        sudo cpupower -c "$PCORES" idle-set -d "$state" >/dev/null 2>&1
    else
        sudo cpupower -c "$PCORES" idle-set -e "$state" >/dev/null 2>&1
    fi

    # cpupower's --cpu list handling varies between versions, so confirm it took
    # effect on the last P-core and fall back to sysfs if it did not.
    local probe="/sys/devices/system/cpu/cpu$PCORE_LAST/cpuidle/state$state/disable"
    [ "$(cat "$probe" 2>/dev/null)" = "$want" ] && return 0

    for c in $(seq "$PCORE_FIRST" "$PCORE_LAST"); do
        echo "$want" | sudo tee "/sys/devices/system/cpu/cpu$c/cpuidle/state$state/disable" >/dev/null 2>&1
    done
}

# Kernel-managed interrupts (nvme queues, some MSI-X) reject affinity writes with
# EIO. That is expected; never let it abort the session.
set_irq_affinity() {
    echo "$2" | sudo tee "/proc/irq/$1/smp_affinity_list" >/dev/null 2>&1
}

# Guarded like set_irq_affinity: a missing or read-only queue must never abort a session.
set_nvme_sched() {
    local dev=$1 sched=$2
    # -e, not -w: the file is root-owned 0644 and the write goes through sudo, so
    # testing writability as the invoking user would skip the write every time.
    [ -e "/sys/block/$dev/queue/scheduler" ] || return 0
    echo "$sched" | sudo tee "/sys/block/$dev/queue/scheduler" >/dev/null 2>&1
}

# The active scheduler is the bracketed one: "none mq-deadline [kyber] adios bfq"
get_nvme_sched() {
    sed -n 's/.*\[\(.*\)\].*/\1/p' "/sys/block/$1/queue/scheduler" 2>/dev/null
}

set_proc_affinity() {
    local pid
    pid=$(pgrep -x "$1" 2>/dev/null | head -1)
    [ -n "${pid:-}" ] && taskset -acp "$2" "$pid" >/dev/null 2>&1
    return 0
}

# --- restore ----------------------------------------------------------------

restore() {
    [ "$restored" -eq 1 ] && return
    restored=1

    echo "Restoring system to normal power saving mode..."

    # Baseline recorded at startup. On the --restore path this is the only source
    # of truth; without it we would be guessing at the values to put back.
    if [ -f "$STATE_FILE" ]; then
        # shellcheck disable=SC1090
        . "$STATE_FILE"
    fi

    sudo cpupower frequency-set -g "${GOV_BEFORE:-powersave}" >/dev/null

    if [ "${CSTATES_CHANGED:-0}" -eq 1 ]; then
        for s in "${DEEP_CSTATES[@]}"; do
            set_cstate "$s" 0
        done
    fi

    # Hand the whole CPU set back.
    for name in pipewire wireplumber pipewire-pulse; do
        set_proc_affinity "$name" 0-31
    done
    irq_thread=$(pgrep snd_hdspe 2>/dev/null | head -1)
    [ -n "${irq_thread:-}" ] && sudo taskset -acp 0-31 "$irq_thread" >/dev/null 2>&1

    for irq in "$AUDIO_IRQ" "${NOISY_IRQS[@]}"; do
        var="IRQ_${irq}_BEFORE"
        mask="${!var:-}"
        [ -n "$mask" ] && set_irq_affinity "$irq" "$mask"
    done

    # Restore Nvidia GPU to adaptive power
    nvidia-settings -a '[gpu:0]/GpuPowerMizerMode=0' >/dev/null 2>&1

    # Bring EasyEffects back only if we were the one who stopped it.
    # On the --restore path this comes from the state file, so a crash recovery
    # that never stopped it leaves it alone.
    if [ "${EE_WAS_RUNNING:-0}" -eq 1 ]; then
        systemctl --user start "$EE_UNIT" 2>/dev/null
    fi

    sudo sysctl -q vm.swappiness="${SWAPPINESS_BEFORE:-150}"
    sudo sysctl -q vm.min_free_kbytes="${MINFREE_BEFORE:-22762}"

    for dev in "${NVME_DEVS[@]}"; do
        var="NVME_${dev}_BEFORE"
        set_nvme_sched "$dev" "${!var:-kyber}"
    done

    if [ -n "$STEER_PID" ]; then
        kill "$STEER_PID" 2>/dev/null
        "$STEER_SH" --restore >/dev/null 2>&1
    fi

    [ -n "$SUDO_KEEPALIVE" ] && kill "$SUDO_KEEPALIVE" 2>/dev/null
    rm -f "$STATE_FILE"
}

if [ "${1:-}" = "--restore" ]; then
    restore
    exit 0
fi

# --- baseline ---------------------------------------------------------------

echo "Optimizing system for Pro Audio..."
sudo -v || exit 1

# A session outlasts sudo's 15 minute timestamp, and restore() runs at the end of
# it. Without this the cleanup either blocks on a password prompt or silently fails.
( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 60; done ) &
SUDO_KEEPALIVE=$!

if systemctl --user is-active --quiet "$EE_UNIT"; then
    EE_WAS_RUNNING=1
fi

{
    echo "GOV_BEFORE=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)"
    echo "SWAPPINESS_BEFORE=$(sysctl -n vm.swappiness)"
    echo "MINFREE_BEFORE=$(sysctl -n vm.min_free_kbytes)"
    for irq in "$AUDIO_IRQ" "${NOISY_IRQS[@]}"; do
        echo "IRQ_${irq}_BEFORE=$(cat "/proc/irq/$irq/smp_affinity_list" 2>/dev/null)"
    done
    for dev in "${NVME_DEVS[@]}"; do
        echo "NVME_${dev}_BEFORE=$(get_nvme_sched "$dev")"
    done
    echo "EE_WAS_RUNNING=$EE_WAS_RUNNING"
    echo "CSTATES_CHANGED=1"
} > "$STATE_FILE"

# Armed only now: before this point nothing has been changed, so there is nothing
# to put back and restore() would have no baseline to read.
trap restore EXIT INT TERM

# --- power ------------------------------------------------------------------

sudo cpupower frequency-set -g performance >/dev/null

echo "Disabling deep idle states on P-cores $PCORES..."
for s in "${DEEP_CSTATES[@]}"; do
    set_cstate "$s" 1
done

nvidia-settings -a '[gpu:0]/GpuPowerMizerMode=1' >/dev/null 2>&1

if [ "$EE_WAS_RUNNING" -eq 1 ]; then
    echo "Stopping EasyEffects for the session..."
    systemctl --user stop "$EE_UNIT"
fi

sudo sysctl -q vm.swappiness=10
# 22MB of reserve on a 31GB box that runs with no free pages means an RT audio
# thread can end up in direct reclaim. 256MB keeps kswapd ahead of it.
sudo sysctl -q vm.min_free_kbytes=262144

echo "Setting NVMe queue scheduler to none..."
for dev in "${NVME_DEVS[@]}"; do
    set_nvme_sched "$dev" none
done

# --- wine sync -------------------------------------------------------------

if [ -r /dev/ntsync ]; then
    echo "Wine sync: ntsync"
else
    echo "Wine sync: fsync (ntsync unavailable)" >&2
    if [ -e /dev/ntsync ]; then
        echo "  /dev/ntsync exists but is not readable by $USER." >&2
        echo "  Fix: /etc/udev/rules.d/70-ntsync.rules -> KERNEL==\"ntsync\", MODE=\"0660\", GROUP=\"audio\"" >&2
    else
        echo "  module not loaded. Fix: sudo modprobe ntsync" >&2
    fi
fi

# --- cpu placement ----------------------------------------------------------

echo "Pinning audio chain to P-cores, interference to E-cores..."

# The PipeWire daemon's data-loop is the most timing-critical thread in the system.
set_proc_affinity pipewire "$PCORES"
# Session managers do no audio processing; keep them off the audio cores.
set_proc_affinity wireplumber "$ECORES"
set_proc_affinity pipewire-pulse "$ECORES"

set_irq_affinity "$AUDIO_IRQ" "$AUDIO_IRQ_CPU"
for irq in "${NOISY_IRQS[@]}"; do
    set_irq_affinity "$irq" "$ECORES"
done

# The threaded handler normally follows the affinity hint, but pin it explicitly.
irq_thread=$(pgrep snd_hdspe 2>/dev/null | head -1)
[ -n "${irq_thread:-}" ] && sudo taskset -acp "$AUDIO_IRQ_CPU" "$irq_thread" >/dev/null 2>&1

# --- bitwig -----------------------------------------------------------------

# Off by default: every Wine STDERR line crosses a pipe that a 'wine-stdio' thread in
# BitwigPluginHost has to read, which measured 71.6 ms per 5 s across the hosts with 11
# plugin instances loaded. Set YABRIDGE_LOG=1 when actually debugging a plugin.
if [ "${YABRIDGE_LOG:-0}" -eq 1 ]; then
    export YABRIDGE_DEBUG_FILE=${YABRIDGE_DEBUG_FILE:-/tmp/yabridge.log}
    echo "yabridge debug log: $YABRIDGE_DEBUG_FILE"
fi

# Pinning without the steward is measurably worse than not pinning at all, so refuse the
# combination rather than starting a silently degraded session. Checked before launch so
# the message is not buried under Bitwig's own output.
if [ "$PIN_BITWIG" -eq 1 ] && [ "$STEER_THREADS" -eq 1 ] && [ ! -x "$STEER_SH" ]; then
    echo "WARNING: $STEER_SH not found or not executable." >&2
    echo "  Pinning to P-cores without it confines every non-audio thread to the audio" >&2
    echo "  cores too -- worse than no pinning. Falling back to PIN_BITWIG=0." >&2
    echo "  Set STEER_THREADS=0 to silence this and pin anyway." >&2
    PIN_BITWIG=0
fi

# Kontakt 7 needs its Wine host to run with a working directory inside the prefix, or it
# aborts on load -- see docs/kontakt7-zmq-crash.md. That cannot be arranged from here:
# Bitwig chdirs BitwigAudioEngine and BitwigPluginHost to ~/.BitwigStudio/log, so a cwd set
# before `bitwig-studio` never reaches the plugin hosts. It is done in the
# ~/.local/bin/yabridge-host.exe wrapper instead.
if [ "$PIN_BITWIG" -eq 1 ]; then
    echo "Starting Bitwig (pinned to P-cores $PCORES)..."
    # Affinity is inherited across fork/exec, so this one mask covers
    # BitwigAudioEngine, BitwigPluginHost and every yabridge-host.exe.so.
    taskset -c "$PCORES" bitwig-studio &
else
    echo "Starting Bitwig (unpinned, all 32 cores)..."
    bitwig-studio &
fi
BITWIG_PID=$!

# Threads appear as plugins are loaded, so keep re-sweeping rather than steering once.
# The steward exits on its own when the audio engine goes away. Gated on PIN_BITWIG so
# that PIN_BITWIG=0 stays a clean "no CPU placement at all" arm for A/B testing.
if [ "$STEER_THREADS" -eq 1 ] && [ "$PIN_BITWIG" -eq 1 ]; then
    echo "Steering non-audio threads to E-cores $ECORES every ${STEER_INTERVAL}s..."
    "$STEER_SH" --watch "$STEER_INTERVAL" >/dev/null 2>&1 &
    STEER_PID=$!
    # A steward that dies on its own leaves the tree pinned with nothing steering it.
    sleep 1
    kill -0 "$STEER_PID" 2>/dev/null || echo "WARNING: steer-threads.sh exited immediately." >&2
fi

wait "$BITWIG_PID"

# restore() runs from the EXIT trap
