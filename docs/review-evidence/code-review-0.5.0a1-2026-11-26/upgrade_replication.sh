#!/usr/bin/env sh
# Replays an in-place upgrade from 0.4.2 to 0.5.0a1 on a scratch data directory and prints what each step returns.
# Nothing here touches a real data directory. Needs GitHub access for the pinned OpenFootball files.
#
# Usage: upgrade_replication.sh <repo checkout at 0.5.0a1> <python 3.12 with httpx, numpy, scipy> <empty scratch dir> \
#            [old commit, default 8125d57 = 0.4.2] [a second interpreter that is not 3.12, optional]
#
# Beyond the 0.4.2 version of this script it also checks:
#   - that the old installation still seals on the data directory after the new one has opened it (rollback);
#   - what the new installation does with release-baseline / release-compare under an interpreter other than 3.12;
#   - whether a recipe approved under 3.12 is accepted when the same build runs under the other interpreter.
# Set OLDPY to run the old version under a different interpreter than the new one (a deployment that was on
# 3.11 or 3.13 and has to compare under 3.12).
set -eu
REPO=$1
PY=$2
WORK=$3
FROM=${4:-8125d57}
OTHER=${5:-}

OLD="$WORK/wt-old"
git -C "$REPO" worktree add --detach "$OLD" "$FROM" >/dev/null 2>&1
DATA="$WORK/data"
HISTORY="$WORK/history"
old() { PYTHONPATH="$OLD/src" "${OLDPY:-$PY}" -m polymarket861 --data-dir "$DATA" "$@"; }
new() { PYTHONPATH="$REPO/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }
other() { PYTHONPATH="$REPO/src" "$OTHER" -m polymarket861 --data-dir "$DATA" "$@"; }
pick='import json,sys; d=json.load(sys.stdin); r=d["release"]; print(json.dumps({"checks.release_executable": d["checks"]["release_executable"], **{k: r.get(k) for k in ("state","ready","active_version","approval","baseline_sha256","comparison_sha256","reference_python","running_python","actions")}}, indent=1, ensure_ascii=False))'
release() { new season readiness --year 2026 --round 12 | "$PY" -c "$pick"; }
version() { PYTHONPATH="$1/src" "$PY" -c 'import polymarket861; print(polymarket861.__version__)'; }
# seal <old|new|other> <fixture>: prints the version lines of the seal, or pm861's own exit code and message.
seal() {
  if "$1" forecast make "$2" "$ROOT" --horizon early > "$WORK/seal.out" 2> "$WORK/seal.err"; then
    grep -E '"(model_version|release_provenance_sha256|news_recipe_sha256)"' "$WORK/seal.out"
  else
    echo "exit=$?"; cat "$WORK/seal.err"
  fi
}

echo "== old version ($FROM, $(version "$OLD")) on $("${OLDPY:-$PY}" -V): initialise, fetch the pinned history, seal one early forecast"
old data refresh --first 2022-23 --last 2026-27 >/dev/null
old football fetch "$HISTORY" --from-season 2010-11 --to-season 2026-27 >/dev/null
ROOT=$(ls -d "$DATA"/datasets/football-inputs/*/sources)
old forecast make epl/2026-27/leeds-united/coventry-city "$ROOT" --horizon early | tee "$WORK/old-seal.json" | grep -E '"model_version"'
OLD_SEAL=$(sed -n 's/^ *"digest": "\([0-9a-f]*\)".*/\1/p' "$WORK/old-seal.json" | tail -1)

echo "== new version ($(version "$REPO")) on the same data directory, before any baseline: season readiness (release part)"
release

echo "== new version: forecast make before approval (expected: exit 2)"
new forecast make epl/2026-27/manchester-united/brentford "$ROOT" --horizon early || echo "exit=$?"

echo "== rollback check: the old version seals again on the directory the new version has just opened"
seal old epl/2026-27/everton/liverpool
old season readiness --year 2026 --round 12 | "$PY" -c 'import json,sys; r=json.load(sys.stdin)["release"]; print(json.dumps({k: r.get(k) for k in ("state","ready","active_version")}))'

echo "== new version: release-compare without a baseline saved by the old version (expected: error)"
new forecast release-compare "$HISTORY" || echo "exit=$?"

if [ -n "$OTHER" ]; then
  echo "== new version under $("$OTHER" -V): release-baseline and release-compare (expected: refused, reference interpreter is 3.12)"
  other forecast release-baseline "$HISTORY" || echo "exit=$?"
  other forecast release-compare "$HISTORY" || echo "exit=$?"
fi

echo "== the documented order: baseline with the old version, compare with the new one"
old forecast release-baseline "$HISTORY" | grep -E '"(version|recipe|study_sha256|matches)"'
new forecast release-compare "$HISTORY" | tee "$WORK/release-compare.json" | grep -E '"(report_sha256|changed_predictions|changed_training_views|paired_count|rounds|candidate_score|baseline_score|to_version)"'

echo "== new version: readiness after compare, before approval"
release

echo "== approve in this scratch directory only, then the same forecast succeeds"
SHA=$(sed -n 's/.*"report_sha256": "\([0-9a-f]*\)".*/\1/p' "$WORK/release-compare.json")
new forecast release-approve "$SHA" --reviewer "scratch replication" --reason "isolated scratch copy; verifies the gate only" | grep -E '"(version|effective_at|reviewer)"' || true
new forecast make epl/2026-27/manchester-united/brentford "$ROOT" --horizon early | grep -E '"(model_version|release_provenance_sha256|news_recipe_sha256)"'
release

if [ -n "$OTHER" ]; then
  echo "== the approved recipe under $("$OTHER" -V): readiness and one seal (same recipe digest expected)"
  other season readiness --year 2026 --round 12 | "$PY" -c "$pick"
  seal other epl/2026-27/arsenal/manchester-city
fi

if [ -n "$OLD_SEAL" ]; then
  echo "== the seal written by the old version reopens byte-identically under the new one"
  old forecast get "$OLD_SEAL" > "$WORK/seal-old.json"
  new forecast get "$OLD_SEAL" > "$WORK/seal-new.json"
  cmp "$WORK/seal-old.json" "$WORK/seal-new.json" && echo "identical ($OLD_SEAL)"
fi

echo "== after approval of the new recipe: the old version on the same directory"
seal old epl/2026-27/fulham/bournemouth

git -C "$REPO" worktree remove --force "$OLD"
