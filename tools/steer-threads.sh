#!/bin/bash
# Split the audio chain across the hybrid CPU by realtime *priority*, not by process.
#
# start-bitwig.sh pins the whole Bitwig tree to the P-cores with one taskset. That mask
# is inherited across fork/exec, so the P-cores end up carrying the audio threads and
# every non-audio thread in the chain alike -- the Bitwig JVM UI, wineserver,
# explorer.exe, the NI service processes and one NI DBScan thread per plugin instance.
# Measured 2026-08-29 with 9x FM8 + 2x Kontakt loaded: 121 threads at rtprio 85 and 412
# non-realtime ones, all on CPUs 0-15, P-cores 95% busy of a possible 1600%, E-cores 9%.
#
# That is a placement problem, not a throughput problem -- aggregate load was about 6%
# per P-core. A thread that a realtime thread waits on synchronously, but which cannot
# preempt it, is an inversion waiting to happen. wineserver is the worst case: single
# threaded, SCHED_OTHER nice 0, every one of the 12 Wine processes calls into it, and
# it led the burst table for the whole chain (431 bursts >= 2ms per 100ms window over
# 120s, worst 7.1ms).
#
# So: only the threads that actually carry audio keep the P-cores, everything else is
# moved to the E-cores. Split on *priority*, not policy -- yabridge elevates exactly
# one thread per host to FIFO 85 ('audio') while Wine's priority mapping leaves every
# plugin-spawned thread ('worker', 'parameters', 'SC3 TaskScheduler', 'URET_Worker') at
# FIFO 5. Those 87 FIFO-5 threads are realtime in name only: they rank below all 121
# audio threads and are starved by them exactly like a SCHED_OTHER thread would be.
#
# Result on that session: Bitwig Load MAX 14.808ms -> 0.904ms against a 5.333ms
# deadline, period jitter 4.22% -> 0.90%, with nothing else changed. Full write-up in
# docs/dsp-spike-investigation.md.
#
# Usage: steer-threads.sh              apply once
#        steer-threads.sh --dry-run    report what would change, touch nothing
#        steer-threads.sh --watch 10   apply, then re-sweep every 10s (plugins that get
#                                      loaded later spawn new threads)
#        steer-threads.sh --restore    hand every thread back to all 32 CPUs

set -uo pipefail

PCORES=${PCORES:-0-15}
ECORES=${ECORES:-16-31}
ALLCORES=${ALLCORES:-0-31}

# wineserver is on the synchronous path of every realtime Wine call, so it must never
# wait behind a FIFO thread. The E-cores have none. -10 is inside the -11 ceiling that
# limits.d grants @audio and @realtime, so this needs no privileges.
WINESERVER_NICE=${WINESERVER_NICE:--10}

# Realtime priority at or above this keeps a P-core. Bitwig and yabridge both use 85
# for audio threads; Wine maps everything else to 5.
RT_MIN=${RT_MIN:-50}

# Matched against /proc/<pid>/comm, which the kernel truncates to 15 characters.
PROC_PATTERNS='^(BitwigStudio|BitwigAudioEngi|BitwigPluginHos|bitwig-studio|yabridge-host\.e|wineserver|services\.exe|winedevice\.exe|plugplay\.exe|svchost\.exe|rpcss\.exe|explorer\.exe|NIHardwareServi|NIHostIntegrati|start\.exe|conhost\.exe)$'

mode=apply
watch_interval=0
POL=""; PRIO=""; MASK=""

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) mode=dry ;;
        --restore) mode=restore ;;
        --watch)   mode=watch; watch_interval=${2:-10}; shift ;;
        -h|--help) sed -n '2,/^$/p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
    shift
done

# --- helpers ----------------------------------------------------------------

# Everything below reads /proc with bash builtins and returns through globals. That is
# not style preference: the obvious version -- $(cat), awk, grep, and command
# substitution -- forked about four times per thread, which measured **5206 forks per
# sweep** across ~530 threads. At --watch 15 that is 5200 short-lived processes landing
# on the audio cores every 15 seconds, i.e. the sweep perturbing the thing it exists to
# protect. `read < file` and parameter expansion fork zero times.

target_pids() {
    local pid comm
    for pid in /proc/[0-9]*; do
        read -r comm < "$pid/comm" 2>/dev/null || continue
        [[ $comm =~ $PROC_PATTERNS ]] && echo "${pid#/proc/} $comm"
    done
}

# Sets POL and PRIO from /proc/<tid>/stat fields 41 and 40. comm sits in parens and can
# contain spaces and parens, so strip through the last ') ' first -- field numbering is
# only stable after that, which leaves policy at field 39 and rt_priority at 38 (array
# indices 38 and 37). SCHED_RESET_ON_FORK is NOT OR'd into this field, so Bitwig's
# data-loop.0 (SCHED_FIFO|SCHED_RESET_ON_FORK, prio 83) correctly reads as policy 1.
thread_sched() {
    local st rest
    local -a f
    read -r st < "$1/stat" 2>/dev/null || return 1
    rest=${st##*) }
    f=($rest)
    POL=${f[38]:-}
    PRIO=${f[37]:-}
    [ -n "$POL" ] && [ -n "$PRIO" ]
}

# Sets MASK from /proc/<tid>/status.
current_mask() {
    local k v
    MASK=
    while read -r k v; do
        if [ "$k" = "Cpus_allowed_list:" ]; then
            MASK=$v
            return 0
        fi
    done < "$1/status" 2>/dev/null
    return 1
}

# --- sweep ------------------------------------------------------------------

sweep() {
    local moved=0 skipped=0 audio=0 other=0 want mask tid task comm pid

    while read -r pid comm; do
        [ -d "/proc/$pid" ] || continue
        for task in /proc/"$pid"/task/*; do
            [ -d "$task" ] || continue
            tid=${task##*/}
            thread_sched "$task" || continue

            case "$mode" in
                restore) want=$ALLCORES ;;
                *)
                    # 1 = SCHED_FIFO, 2 = SCHED_RR.
                    if { [ "$POL" = "1" ] || [ "$POL" = "2" ]; } && [ "$PRIO" -ge "$RT_MIN" ]; then
                        want=$PCORES; audio=$((audio+1))
                    else
                        want=$ECORES; other=$((other+1))
                    fi ;;
            esac

            current_mask "$task" || continue
            mask=$MASK
            if [ "$mask" = "$want" ]; then
                skipped=$((skipped+1))
                continue
            fi

            if [ "$mode" = dry ]; then
                printf '  would move %-16s tid=%-8s %s -> %s\n' "$comm" "$tid" "$mask" "$want"
            else
                taskset -cp "$want" "$tid" >/dev/null 2>&1 && moved=$((moved+1))
            fi
        done
    done < <(target_pids)

    if [ "$mode" != restore ]; then
        for pid in $(pgrep -x wineserver 2>/dev/null); do
            if [ "$mode" = dry ]; then
                printf '  would renice wineserver pid=%s to %s (now %s)\n' \
                    "$pid" "$WINESERVER_NICE" "$(ps -o ni= -p "$pid" | tr -d ' ')"
            else
                renice -n "$WINESERVER_NICE" -p "$pid" >/dev/null 2>&1 \
                    || sudo -n renice -n "$WINESERVER_NICE" -p "$pid" >/dev/null 2>&1
            fi
        done
    else
        for pid in $(pgrep -x wineserver 2>/dev/null); do
            [ "$mode" = dry ] || renice -n 0 -p "$pid" >/dev/null 2>&1
        done
    fi

    case "$mode" in
        dry)     printf 'dry run: %d audio (rtprio >= %s) -> %s, %d other -> %s, %d already correct\n' \
                     "$audio" "$RT_MIN" "$PCORES" "$other" "$ECORES" "$skipped" ;;
        restore) printf 'restored %d threads to %s (%d already there)\n' \
                     "$moved" "$ALLCORES" "$skipped" ;;
        *)       printf 'steered %d threads (%d audio on %s, %d other on %s, %d unchanged)\n' \
                     "$moved" "$audio" "$PCORES" "$other" "$ECORES" "$skipped" ;;
    esac
}

if [ "$mode" = watch ]; then
    # Keep the steward itself off the audio cores. It is launched from start-bitwig.sh
    # outside the taskset that wraps bitwig-studio, so without this it inherits 0-31 and
    # does its own work on the P-cores.
    taskset -cp "$ECORES" $$ >/dev/null 2>&1
    sweep
    mode=apply
    # start-bitwig.sh launches this at the same moment as Bitwig, and the audio engine
    # takes several seconds to appear -- so only treat a missing engine as "session
    # over" once one has actually been seen.
    engine_seen=0
    while sleep "$watch_interval"; do
        if [ -n "$(pgrep -x BitwigAudioEngi 2>/dev/null)" ]; then
            engine_seen=1
        elif [ "$engine_seen" -eq 1 ]; then
            break
        fi
        sweep >/dev/null
    done
else
    sweep
fi
