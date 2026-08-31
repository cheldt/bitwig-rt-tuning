#!/bin/bash
# Run one measurement arm: start catch-load.py, then launch Bitwig through the
# normal session wrapper so its sudo prompt lands in your terminal.
#
# The sampler has to be running before the load happens, and the load happens a
# few seconds after launch -- too fast to start a sampler by hand afterwards.
# So this starts the sampler first and launches Bitwig second.
#
# Usage: run-arm.sh <arm-name> [VAR=VAL ...]
#   run-arm.sh baseline
#   run-arm.sh steer1  STEER_INTERVAL=1
#   run-arm.sh nopin   PIN_BITWIG=0 STEER_THREADS=0
#
# Quit Bitwig once the DSP graph has shown you the spike. The sampler stops on
# its own after DUR seconds regardless.
#
# Env: PROJECT, DUR, OUTDIR, START

set -uo pipefail

TOOLS=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
REPO=$(dirname "$TOOLS")

PROJECT=${PROJECT:-/media/nvme2/data/bitwig_projects/spike/spike.bwproject}
START=${START:-$REPO/start-bitwig.sh}
DUR=${DUR:-150}
OUTDIR=${OUTDIR:-${XDG_RUNTIME_DIR:-/tmp}/bitwig-arms}

ARM=${1:?usage: run-arm.sh <arm-name> [VAR=VAL ...]}
shift

mkdir -p "$OUTDIR" || exit 1
OUT="$OUTDIR/$ARM.jsonl"

# fstrim drives system-wide io_full to ~50 % for many minutes (12m03s when it was
# measured, at ~99 % device-busy on each volume in turn) and would poison the arm.
# Refuse rather than record a run that has to be thrown away.
if pgrep -x fstrim >/dev/null 2>&1; then
    echo "REFUSING: fstrim is running -- io pressure would poison this arm." >&2
    echo "  wait for it, or: sudo systemctl stop fstrim.service" >&2
    exit 1
fi

echo "arm:      $ARM"
echo "env:      ${*:-<none>}"
echo "project:  $PROJECT"
echo "sampler:  $OUT (${DUR}s)"
echo "schedstats: $(sysctl -n kernel.sched_schedstats 2>/dev/null || echo '?')"
echo

# Observer on the E-cores at FIFO 10: low enough that it can never preempt an
# audio thread, high enough that it is not starved by the SCHED_OTHER load it is
# there to observe. Same convention as catch-stall.py.
chrt -f 10 taskset -c 16-31 python3 "$TOOLS/catch-load.py" \
    --dur "$DUR" --out "$OUT" &
SAMPLER=$!

# One sample of quiet baseline before anything starts.
sleep 0.5

if [ $# -gt 0 ]; then
    env "$@" "$START" "$PROJECT"
else
    "$START" "$PROJECT"
fi

wait "$SAMPLER" 2>/dev/null

cp "$HOME/.BitwigStudio/log/engine.log" "$OUTDIR/$ARM.engine.log" 2>/dev/null

echo
echo "done."
echo "  sampler -> $OUT"
echo "  engine  -> $OUTDIR/$ARM.engine.log"
echo
echo "analyse:  python3 $TOOLS/summarize-load.py $OUT"
