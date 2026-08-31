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
#        steer-threads.sh --watch 10   apply, then re-sweep every 10s, and immediately
#                                      (every POLL for BURST seconds) whenever a new
#                                      matched process appears -- see POLL/BURST below
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

# A plugin load is the one event the slow sweep cannot cover. Bitwig forks
# BitwigPluginHost from a JVM *worker* thread, which this script has already moved to
# the E-cores, so the host is born inside 16-31 and creates its 33 SCHED_FIFO 85 audio
# threads there. They stay until the next sweep. Measured 2026-08-31, one Kontakt 6
# loading cold with --watch 15: the host appeared at t=7.54 and its audio threads sat
# on 16-31 until the sweep at t=16.22 -- 8.7s during which Bitwig's data-loop.0 worst
# callback ran 0.49-2.08ms against a 0.20ms baseline, dropping back to 0.20ms in the
# same second they reached the P-cores.
#
# Fixing the inherited mask does not work: the forking thread is not the main thread,
# so there is nothing to pre-set. Instead, watch for new processes cheaply and sweep as
# soon as one shows up, then keep sweeping for BURST seconds because the audio threads
# are created progressively over the ~6s of Wine startup, not all at fork time.
POLL=${POLL:-0.25}
BURST=${BURST:-12}

# Audio threads that are not yet realtime. yabridge names its per-plugin audio
# thread audio-N when it creates it, but only elevates it to SCHED_FIFO 85 when the
# host activates the plugin. In between it is FIFO 5, which RT_MIN correctly reads
# as "not an audio thread" and sends to the E-cores -- and Kontakt's first
# process() call then runs there.
#
# Measured 2026-08-31 at 2ms resolution, one Kontakt 6 loading cold with the burst
# sweep already in place: yabridge-host.e/audio-0 was FIFO 5 on 16-31 at t=16.33 and
# only became FIFO 85 on 0-15 at t=17.33. The activation callback landed at t=17.128,
# inside that window, and burned 1.619ms on-CPU in a single 2ms window against a
# 0.028ms median -- on cpu31, an E-core. Bitwig's own data-loop.0 never exceeded
# 0.353ms; it was blocked waiting, and reported the sum as Load MAX 1.861ms.
#
# So: promote by name, scoped to the processes that do this, and leave every other
# FIFO-5 Wine thread (BGLoading, Disk, worker, ProcessMonitor) on the E-cores where
# it belongs.
LATE_RT_NAMES=${LATE_RT_NAMES:-'^audio-[0-9]+$'}
LATE_RT_PROCS=${LATE_RT_PROCS:-'^yabridge-host\.e$'}

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

# True if this thread is an audio thread by name -- checked only for the few
# processes in LATE_RT_PROCS, so it costs one extra comm read for ~20 threads per
# Wine host rather than one for every thread in the tree.
thread_is_late_rt() {
    local tcomm
    read -r tcomm < "$1/comm" 2>/dev/null || return 1
    [[ $tcomm =~ $LATE_RT_NAMES ]]
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
    local moved=0 skipped=0 audio=0 other=0 late=0 want mask tid task comm pid

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
                    elif [[ $comm =~ $LATE_RT_PROCS ]] && thread_is_late_rt "$task"; then
                        want=$PCORES; late=$((late+1))
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
        dry)     printf 'dry run: %d audio (rtprio >= %s) -> %s, %d by name -> %s, %d other -> %s, %d already correct\n' \
                     "$audio" "$RT_MIN" "$PCORES" "$late" "$PCORES" "$other" "$ECORES" "$skipped" ;;
        restore) printf 'restored %d threads to %s (%d already there)\n' \
                     "$moved" "$ALLCORES" "$skipped" ;;
        *)       printf 'steered %d threads (%d audio + %d by-name on %s, %d other on %s, %d unchanged)\n' \
                     "$moved" "$audio" "$late" "$PCORES" "$other" "$ECORES" "$skipped" ;;
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

    # POLL -> centiseconds with string ops only, so it can be fractional without bc. printf '%.2f' is locale-dependent
    # (this box is de_DE and rejects "0.25"), and 10# is needed because a two-digit
    # fraction like "05" would otherwise be read as octal.
    poll_i=${POLL%%.*}
    poll_f=${POLL#*.}
    [ "$poll_f" = "$POLL" ] && poll_f=0
    poll_f=${poll_f}00
    poll_cs=$(( 10#${poll_i:-0} * 100 + 10#${poll_f:0:2} ))
    [ "$poll_cs" -gt 0 ] || poll_cs=25
    ticks_slow=$(( watch_interval * 100 / poll_cs ))
    [ "$ticks_slow" -gt 0 ] || ticks_slow=1
    ticks_burst=$(( BURST * 100 / poll_cs ))

    declare -A MATCHED=()
    burst=0
    tick=0

    # One glob of /proc plus one comm read per pid, at 1/POLL Hz: ~400 reads a tick,
    # no per-thread work and no forks. Sets NEW=1 if a matched process appeared and
    # ENGINE=1 if the audio engine is alive.
    #
    # comm is re-read every tick rather than cached per pid, because a pid can change
    # identity without dying: start.exe execs into yabridge-host.exe.so and keeps its
    # pid, so a cached "not one of ours" would be wrong for the rest of the session.
    # Keying on (pid, comm) also makes pid reuse a miss rather than a false negative.
    scan_procs() {
        local pid p comm
        local -A now=()
        NEW=0; ENGINE=0
        for pid in /proc/[0-9]*; do
            p=${pid#/proc/}
            read -r comm < "$pid/comm" 2>/dev/null || continue
            [[ $comm =~ $PROC_PATTERNS ]] || continue
            now[$p]=$comm
            [ "$comm" = "BitwigAudioEngi" ] && ENGINE=1
            [ "${MATCHED[$p]:-}" = "$comm" ] || NEW=1
        done
        # Rebuild wholesale so processes that exited drop out on their own.
        MATCHED=()
        for p in "${!now[@]}"; do MATCHED[$p]=${now[$p]}; done
    }

    while sleep "$POLL"; do
        scan_procs
        if [ "$ENGINE" -eq 1 ]; then
            engine_seen=1
        elif [ "$engine_seen" -eq 1 ]; then
            break
        fi

        # A new plugin host is the case worth reacting to immediately.
        [ "$NEW" -eq 1 ] && burst=$ticks_burst

        if [ "$burst" -gt 0 ]; then
            burst=$((burst-1))
            sweep >/dev/null
        elif [ $(( tick % ticks_slow )) -eq 0 ]; then
            sweep >/dev/null
        fi
        tick=$((tick+1))
    done
else
    sweep
fi
