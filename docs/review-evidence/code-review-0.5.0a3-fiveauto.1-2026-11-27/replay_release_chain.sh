#!/usr/bin/env sh
# Replays handing one EPL data directory along a chain of separately installed versions (0.5.0a3 review variant).
# Every version is its own non-editable install (python3.12 -m venv + pip install -c constraints/research.txt <checkout>),
# which is the layout docs/model-release-governance.md asks for. Nothing here touches a real data directory.
#
# Usage: replay_release_chain.sh <empty scratch dir> <pm861 of the starting version> <pm861 of the next version> \
#            [<pm861 of a further version> ...]
#
# The scratch directory is initialised by the first executable (needs GitHub access for the fixture list and the pinned
# OpenFootball files), then every following executable takes the directory over in turn with the documented steps:
#   release-baseline by the version in charge, release-compare and release-approve by the one taking over.
# To replay a rollback after approval, name the earlier executable again at the end, e.g.
#   replay_release_chain.sh scratch inst-0.4.2/bin/pm861 inst-0.5.0a1/bin/pm861 inst-0.5.0a2/bin/pm861 inst-0.5.0a1/bin/pm861
# With START_DATA=<existing scratch data dir> and START_HISTORY=<the history dir used with it> both are copied instead
# (the first executable must then be the version in charge of that directory).
set -eu
WORK=$1
shift
mkdir -p "$WORK"
DATA="$WORK/data"
HISTORY="$WORK/history"
PY=$(dirname "$1")/python

pick='import json,sys; d=json.load(sys.stdin); r=d["release"]; print(json.dumps({"checks.release_executable": d.get("checks",{}).get("release_executable"), **{k: r.get(k) for k in ("state","ready","active_version","approval","reference_python","running_python")}}, ensure_ascii=False))'
ready() { "$1" --data-dir "$DATA" season readiness --year 2026 --round 12 | "$PY" -I -c "$pick"; }
# next_fixture: an EPL fixture that kicks off more than 30 hours from now and has no seal yet.
next_fixture() {
  "$PY" -I -c 'import sqlite3,sys,datetime
db=sqlite3.connect("file:"+sys.argv[1]+"/runtime.sqlite3?mode=ro",uri=True)
edge=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
print(db.execute("select fixture_id from fixture_versions where kickoff > ? and fixture_id not in (select fixture_id from forecast_seals) group by fixture_id order by min(kickoff), fixture_id limit 1",(edge,)).fetchone()[0])' "$DATA"
}
# seal <pm861>: seals the next free fixture; prints the version lines, or the exit code and message.
seal() {
  FIXTURE=$(next_fixture)
  if "$1" --data-dir "$DATA" forecast make "$FIXTURE" "$ROOT" --horizon early > "$WORK/seal.out" 2> "$WORK/seal.err"; then
    LAST_SEAL=$(sed -n 's/^ *"digest": "\([0-9a-f]*\)".*/\1/p' "$WORK/seal.out" | tail -1)
    echo "sealed $FIXTURE: $(grep -E '"model_version"' "$WORK/seal.out" | tr -s ' ')"
  else
    echo "refused $FIXTURE: exit=$? $(head -c 300 "$WORK/seal.err" | paste -sd' ' -)"
  fi
}

CURRENT=$1
shift
if [ -n "${START_DATA:-}" ]; then
  cp -a "$START_DATA" "$DATA"
  cp -a "$START_HISTORY" "$HISTORY"
  echo "== copied an existing scratch directory; in charge: $("$CURRENT" --version)"
else
  echo "== $("$CURRENT" --version): initialise the scratch directory and fetch the pinned history"
  "$CURRENT" --data-dir "$DATA" data refresh --first 2022-23 --last 2026-27 >/dev/null
  "$CURRENT" --data-dir "$DATA" football fetch "$HISTORY" --from-season 2010-11 --to-season 2026-27 >/dev/null
fi
ROOT=$(ls -d "$DATA"/datasets/football-inputs/*/sources | head -1)
ready "$CURRENT"
seal "$CURRENT"

STEP=0
for NEXT in "$@"; do
  STEP=$((STEP + 1))
  FROM_V=$("$CURRENT" --version)
  TO_V=$("$NEXT" --version)
  echo
  echo "== switch $STEP: $FROM_V -> $TO_V"
  echo "-- $TO_V opens the directory before anything is approved"
  ready "$NEXT"
  seal "$NEXT"
  echo "-- $FROM_V is still in charge and seals again"
  seal "$CURRENT"
  KEEP=$LAST_SEAL
  echo "-- baseline by $FROM_V, compare by $TO_V"
  "$CURRENT" --data-dir "$DATA" forecast release-baseline "$HISTORY" | grep -E '"(version|matches)"' | tr -s ' ' | paste -sd' ' -
  # Unlike the 0.5.0a2 script this one carries on when the next version installs the recipe that is already active:
  # release-compare then refuses, no approval is needed, and both executables may seal.
  if "$NEXT" --data-dir "$DATA" forecast release-compare "$HISTORY" > "$WORK/compare-$STEP.json" 2> "$WORK/compare-$STEP.err"; then
    "$PY" -I -c 'import json,sys
d=json.load(open(sys.argv[1]))
def walk(x, path=""):
    if isinstance(x, dict):
        for k, v in x.items(): yield from walk(v, path + "/" + k)
    else: yield path, x
want = ("changed_predictions","changed_training_views","paired_count","rounds","from_version","to_version","report_sha256")
print(json.dumps({p: v for p, v in walk(d) if p.rsplit("/",1)[-1] in want}, ensure_ascii=False))' "$WORK/compare-$STEP.json"
    SHA=$(sed -n 's/.*"report_sha256": "\([0-9a-f]*\)".*/\1/p' "$WORK/compare-$STEP.json" | tail -1)
    ready "$NEXT"
    echo "-- approve in this scratch directory only"
    "$NEXT" --data-dir "$DATA" forecast release-approve "$SHA" --reviewer "scratch replication" --reason "isolated scratch copy; verifies the switch only" | grep -E '"(version|effective_at|reviewer)"' | tr -s ' ' | paste -sd' ' -
    ready "$NEXT"
    seal "$NEXT"
  else
    echo "release-compare refused: exit=$? $(head -c 300 "$WORK/compare-$STEP.err" | paste -sd' ' -)"
    echo "-- same active recipe: no approval step; $TO_V seals again"
    seal "$NEXT"
  fi
  echo "-- $FROM_V on the same directory afterwards"
  ready "$CURRENT"
  seal "$CURRENT"
  echo "-- a seal written by $FROM_V before the switch reopens identically under $TO_V"
  "$CURRENT" --data-dir "$DATA" forecast get "$KEEP" > "$WORK/get-from.json"
  "$NEXT" --data-dir "$DATA" forecast get "$KEEP" > "$WORK/get-to.json"
  cmp "$WORK/get-from.json" "$WORK/get-to.json" && echo "identical ($KEEP)"
  CURRENT=$NEXT
done
