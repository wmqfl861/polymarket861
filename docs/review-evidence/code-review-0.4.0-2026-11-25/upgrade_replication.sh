#!/usr/bin/env sh
# Replays the 0.3.0 -> 0.4.0 upgrade on a scratch data directory and prints what each step returns.
# Nothing here touches a real data directory. Needs GitHub access for the pinned OpenFootball files.
#
# Usage: upgrade_replication.sh <repo checkout> <python with httpx, numpy, scipy> <empty scratch dir>
#
# Use the same interpreter for both versions: the built-in recipe includes the numpy/scipy versions.
set -eu
REPO=$1
PY=$2
WORK=$3

OLD="$WORK/wt-030"
git -C "$REPO" worktree add --detach "$OLD" 12d1717
DATA="$WORK/data"
HISTORY="$WORK/history"
old() { PYTHONPATH="$OLD/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }
new() { PYTHONPATH="$REPO/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }

echo "== 0.3.0: initialise, fetch the pinned history, seal one early forecast"
old data refresh --first 2022-23 --last 2026-27 >/dev/null
old football fetch "$HISTORY" --from-season 2010-11 --to-season 2026-27 >/dev/null
ROOT=$(ls -d "$DATA"/datasets/football-inputs/*/sources)
old forecast make epl/2026-27/leeds-united/coventry-city "$ROOT" --horizon early >/dev/null

echo "== 0.4.0 on the same data directory, before any approval (expected: exit 2)"
new forecast make epl/2026-27/manchester-united/brentford "$ROOT" --horizon early || echo "exit=$?"

echo "== 0.4.0 release-compare without a baseline saved by 0.3.0 (expected: error)"
new forecast release-compare "$HISTORY" || echo "exit=$?"

echo "== the documented order: baseline with 0.3.0, compare with 0.4.0"
old forecast release-baseline "$HISTORY"
new forecast release-compare "$HISTORY" | tee "$WORK/release-compare.json" | grep -E '"(report_sha256|changed_predictions|paired_count|rounds|candidate_score|baseline_score)"'

echo "== approve in this scratch directory only, then the same forecast succeeds"
SHA=$(sed -n 's/.*"report_sha256": "\([0-9a-f]*\)".*/\1/p' "$WORK/release-compare.json")
new forecast release-approve "$SHA" --reviewer "scratch replication" --reason "isolated scratch copy; verifies the gate only"
new forecast make epl/2026-27/manchester-united/brentford "$ROOT" --horizon early | grep -E '"model_version"'

git -C "$REPO" worktree remove --force "$OLD"
