#!/usr/bin/env sh
# Replays an in-place upgrade to 0.4.2 on a scratch data directory and prints what each step returns.
# Nothing here touches a real data directory. Needs GitHub access for the pinned OpenFootball files.
#
# Usage: upgrade_replication.sh <repo checkout at 0.4.2> <python with httpx, numpy, scipy> <empty scratch dir> [old commit]
#   old commit defaults to ea58105 (0.4.1). Use 12d1717 for 0.3.0.
#
# Use the same interpreter for both versions: the built-in recipe includes the numpy/scipy versions.
set -eu
REPO=$1
PY=$2
WORK=$3
FROM=${4:-ea58105}

OLD="$WORK/wt-old"
git -C "$REPO" worktree add --detach "$OLD" "$FROM" >/dev/null 2>&1
DATA="$WORK/data"
HISTORY="$WORK/history"
old() { PYTHONPATH="$OLD/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }
new() { PYTHONPATH="$REPO/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }
release() { new season readiness --year 2026 --round 12 | "$PY" -c 'import json,sys; d=json.load(sys.stdin); r=d["release"]; print(json.dumps({"checks.release_executable": d["checks"]["release_executable"], **{k: r.get(k) for k in ("state","ready","active_version","approval","baseline_sha256","comparison_sha256","actions")}}, indent=1, ensure_ascii=False))'; }

echo "== old version ($FROM, $(PYTHONPATH="$OLD/src" "$PY" -c 'import polymarket861; print(polymarket861.__version__)')): initialise, fetch the pinned history, seal one early forecast"
old data refresh --first 2022-23 --last 2026-27 >/dev/null
old football fetch "$HISTORY" --from-season 2010-11 --to-season 2026-27 >/dev/null
ROOT=$(ls -d "$DATA"/datasets/football-inputs/*/sources)
old forecast make epl/2026-27/leeds-united/coventry-city "$ROOT" --horizon early | tee "$WORK/old-seal.json" | grep -E '"model_version"'
OLD_SEAL=$(sed -n 's/^ *"digest": "\([0-9a-f]*\)".*/\1/p' "$WORK/old-seal.json" | tail -1)

echo "== 0.4.2 on the same data directory, before any baseline: season readiness (release part)"
release

echo "== 0.4.2 forecast make before approval (expected: exit 2)"
new forecast make epl/2026-27/manchester-united/brentford "$ROOT" --horizon early || echo "exit=$?"

echo "== 0.4.2 release-compare without a baseline saved by the old version (expected: error)"
new forecast release-compare "$HISTORY" || echo "exit=$?"

echo "== the documented order: baseline with the old version, compare with 0.4.2"
old forecast release-baseline "$HISTORY" | grep -E '"(version|recipe|study_sha256|matches)"'
new forecast release-compare "$HISTORY" | tee "$WORK/release-compare.json" | grep -E '"(report_sha256|changed_predictions|changed_training_views|paired_count|rounds|candidate_score|baseline_score|to_version)"'

echo "== 0.4.2 readiness after compare, before approval"
release

echo "== approve in this scratch directory only, then the same forecast succeeds"
SHA=$(sed -n 's/.*"report_sha256": "\([0-9a-f]*\)".*/\1/p' "$WORK/release-compare.json")
new forecast release-approve "$SHA" --reviewer "scratch replication" --reason "isolated scratch copy; verifies the gate only" | grep -E '"(version|effective_at|reviewer)"' || true
new forecast make epl/2026-27/manchester-united/brentford "$ROOT" --horizon early | grep -E '"model_version"'
release

if [ -n "$OLD_SEAL" ]; then
  echo "== the seal written by the old version reopens byte-identically under 0.4.2"
  old forecast get "$OLD_SEAL" > "$WORK/seal-old.json"
  new forecast get "$OLD_SEAL" > "$WORK/seal-new.json"
  cmp "$WORK/seal-old.json" "$WORK/seal-new.json" && echo "identical ($OLD_SEAL)"
fi

git -C "$REPO" worktree remove --force "$OLD"
