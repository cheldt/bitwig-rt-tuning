#!/bin/bash
# A/B the nvme queue scheduler against the audible xrun count, in one Bitwig session.
#
# The scheduler can be flipped live, so both arms run against the same loaded project,
# the same plugin set and the same playhead -- no restart, no reload, none of the
# run-to-run variance that makes Load MAX useless (5.402 vs 6.052 ms on identical
# configs). Arms alternate A-B-A so a one-way drift over the session is visible as a
# difference between the two A arms rather than being charged to B.
#
# Also samples /proc/diskstats per arm. That is the more important number here: the
# whole reason the scheduler was ruled out in the first place is the measurement
# "Kontakt reads 0 KB from disk during playback". If the read counters stay flat, this
# A/B cannot show a difference and is only evidence of no harm -- see
# docs/dsp-spike-investigation.md.
#
# Usage: ./ab-nvme-sched.sh [seconds-per-arm]   (default 300; bursts have 60-90s gaps)
#
# Run it with Bitwig playing the heaviest project you have, and leave it playing for
# the whole run: 3 arms x 300s = 15 minutes.

set -uo pipefail

DUR=${1:-300}
DEVS=(nvme0n1 nvme1n1 nvme2n1)
ARMS=(none kyber none)

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(dirname "$SCRIPT_DIR")
LOG="$REPO_DIR/docs/measurements/$(date +%F)_nvme-sched-ab.log"

SCHED_BEFORE=()
SUDO_KEEPALIVE=""
restored=0

get_sched() {
    sed -n 's/.*\[\(.*\)\].*/\1/p' "/sys/block/$1/queue/scheduler" 2>/dev/null
}

set_sched() {
    local dev=$1 sched=$2
    # -e, not -w: the file is root-owned 0644 and the write goes through sudo, so
    # testing writability as the invoking user would skip the write every time.
    [ -e "/sys/block/$dev/queue/scheduler" ] || return 0
    echo "$sched" | sudo tee "/sys/block/$dev/queue/scheduler" >/dev/null 2>&1
}

restore() {
    [ "$restored" -eq 1 ] && return
    restored=1
    local i
    for i in "${!DEVS[@]}"; do
        set_sched "${DEVS[$i]}" "${SCHED_BEFORE[$i]}"
    done
    echo "restored: ${SCHED_BEFORE[*]}" | tee -a "$LOG"
    [ -n "$SUDO_KEEPALIVE" ] && kill "$SUDO_KEEPALIVE" 2>/dev/null
}

# /proc/diskstats: $4 reads $6 sectors-read $7 ms-reading $8 writes $10 sectors-written
# $11 ms-writing $13 io_ticks. Sectors are always 512 B here regardless of block size.
diskstat_line() {
    awk -v d="$1" '$3==d {print $4, $6, $7, $8, $10, $11, $13}' /proc/diskstats
}

diskstat_delta() {
    local dev=$1 before=$2 after=$3
    awk -v dev="$dev" -v b="$before" -v a="$after" -v t="$DUR" 'BEGIN {
        split(b, B, " "); split(a, A, " ");
        printf "    %-8s reads %+7d  read %+9.1f MB  read-wait %+7d ms   writes %+7d  written %+9.1f MB  busy %+7d ms (%.1f%%)\n",
            dev, A[1]-B[1], (A[2]-B[2])*512/1048576, A[3]-B[3],
            A[4]-B[4], (A[5]-B[5])*512/1048576, A[7]-B[7], (A[7]-B[7])/(t*10);
    }'
}

pgrep -f BitwigAudioEngine >/dev/null 2>&1 || pgrep -x bitwig-studio >/dev/null 2>&1 || {
    echo "Bitwig is not running. Load the heaviest project, start playback, then re-run." >&2
    exit 1
}

echo "This needs sudo to flip the scheduler between arms."
sudo -v || exit 1
( while kill -0 "$$" 2>/dev/null; do sudo -n true 2>/dev/null; sleep 60; done ) &
SUDO_KEEPALIVE=$!

for dev in "${DEVS[@]}"; do
    SCHED_BEFORE+=("$(get_sched "$dev")")
done
trap restore EXIT INT TERM

mkdir -p "$(dirname "$LOG")"
{
    echo "=== nvme scheduler A/B  $(date -Is)"
    echo "arms: ${ARMS[*]}   ${DUR}s each   devices: ${DEVS[*]}"
    echo "baseline schedulers: ${SCHED_BEFORE[*]}"
    echo "keep the same project playing for the whole run; do not touch the transport"
    echo
} | tee -a "$LOG"

for arm in "${!ARMS[@]}"; do
    sched=${ARMS[$arm]}
    echo "--- arm $((arm+1))/${#ARMS[@]}: $sched" | tee -a "$LOG"

    for dev in "${DEVS[@]}"; do
        set_sched "$dev" "$sched"
    done
    for dev in "${DEVS[@]}"; do
        actual=$(get_sched "$dev")
        [ "$actual" = "$sched" ] || echo "  WARNING: $dev is '$actual', wanted '$sched'" | tee -a "$LOG"
    done

    declare -A ds_before
    for dev in "${DEVS[@]}"; do
        ds_before[$dev]=$(diskstat_line "$dev")
    done

    # The xrun tool does the sleeping, so the disk window matches the xrun window.
    "$SCRIPT_DIR/measure-xruns.sh" "$DUR" 2>&1 | tee -a "$LOG"

    echo "  disk over the same window:" | tee -a "$LOG"
    for dev in "${DEVS[@]}"; do
        diskstat_delta "$dev" "${ds_before[$dev]}" "$(diskstat_line "$dev")" | tee -a "$LOG"
    done
    echo | tee -a "$LOG"
done

echo "log: $LOG"
