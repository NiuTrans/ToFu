#!/usr/bin/env bash
# tests/cache_deploy_verdict.sh — one-command prefix-cache deploy verdict.
#
# Zero guesswork, no human interpretation: prints the THREE deploy-gate facts
# and a single READY / WAIT / FAIL line, then exits with a scriptable code.
#
#   (a) the REAL :15000 listener PID's start time (from ss/lsof, NOT a log
#       banner) and whether it postdates the newest cache fix (8ecbbcf 02:11);
#   (b) the number of post-boot CacheStats samples vs the >=150 floor;
#   (c) the already-cached-prefix miss counts (inside_prior_cached_prefix=True
#       only — benign editable-tail flips are excluded).
#
# READY prints only when ALL THREE hold. Otherwise WAIT (not deployed / too few
# samples) or FAIL (deployed + enough traffic but a miss class still fires — a
# real drift face remains; the field-level tracer field=[...] names it).
#
# Usage:
#   bash tests/cache_deploy_verdict.sh                # defaults (>=150 samples)
#   CACHE_ACCEPT_MIN_SAMPLES=50 bash tests/cache_deploy_verdict.sh
#   FIX_COMMIT_TS='2026-07-18 02:11:04' bash tests/cache_deploy_verdict.sh
#
# Exit codes: 0=READY, 2=WAIT, 3=FAIL (so CI / a timer can branch on it).
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="$(python3 tests/cache_acceptance_check.py --verbose "$@" 2>/dev/null)"
echo "$OUT"

VERDICT="$(printf '%s\n' "$OUT" | sed -n 's/^ACCEPTANCE: \([A-Z]*\).*/\1/p' | tail -1)"
case "$VERDICT" in
  READY) exit 0 ;;
  FAIL)  exit 3 ;;
  *)     exit 2 ;;   # WAIT or anything unexpected
esac
