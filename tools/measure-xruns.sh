#!/bin/bash
# Count deadline misses on the PLAYBACK path over a window.
#
# Bitwig's Load MAX is a single-sample peak: two identical runs measured 5.402 and
# 6.052 ms, so it cannot resolve a change smaller than that. This counts the errors
# that correspond to what you actually hear.
#
# The RME *capture* node has zero links and is not on the audible path -- its error
# count runs thousands high and is a red herring. Watch output + Bitwig.
#
# Usage: ./measure-xruns.sh [seconds]   (default 300; bursts have 60-90s gaps, so
#                                        shorter windows give false "fixed" readings)
set -uo pipefail
DUR=${1:-300}

snap() {
    pw-top -b -n 2 2>/dev/null | tail -25 \
      | grep -E 'pro-output-0|pro-input-0|Bitwig Studio' \
      | awk '{print $NF"="$9}' | tail -3
}

before=$(snap)
[ -z "$before" ] && { echo "no nodes found - is Bitwig running?" >&2; exit 1; }
echo "measuring ${DUR}s ..."; echo "$before" | sed 's/^/  start /'
sleep "$DUR"
after=$(snap)
echo "$after" | sed 's/^/  end   /'

echo
get() { echo "$1" | grep -oE "$2=[0-9]+" | cut -d= -f2 | tail -1; }
for n in Studio pro-output-0 pro-input-0; do
    a=$(get "$before" "$n"); b=$(get "$after" "$n")
    [ -z "$a" ] || [ -z "$b" ] && continue
    lbl="$n"; case "$n" in
      Studio)       lbl="Bitwig       (AUDIBLE)";;
      pro-output-0) lbl="RME playback (AUDIBLE)";;
      pro-input-0)  lbl="RME capture  (unused, ignore)";;
    esac
    awk -v a="$a" -v b="$b" -v t="$DUR" -v l="$lbl" \
      'BEGIN{printf "  %-30s %5d -> %-5d  %+5d   %.3f/s\n", l, a, b, b-a, (b-a)/t}'
done
