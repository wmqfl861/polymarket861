#!/usr/bin/env sh
# 新闻前瞻研究 v2 的登记在一份临时数据目录上的实际表现。只在副本上登记，不触碰任何真实目录。
#
#   1. 未登记时 report 返回 not_registered；登记一次；原样再登记一次返回同一份证书；
#      换一个开始时间再登记被拒绝（登记不可更改）。
#   2. 登记之后做一次提前封存（--horizon early）：这类封存不在两个固定决策窗内，不会被研究绑定。
#   3. 模拟“以后某一版只改了方案文字里的一个逗号”：在源码副本上改 news_endpoint_v2.py 的一句说明，
#      看这一版在已登记的目录上能不能出研究报告、能不能照常封存。
#
# 用法: probe_v2_registration.sh <仓库（0.5.0a2）> <装好依赖的 python 3.12> <空的临时目录> <已经批准 0.5.0a2 的临时数据目录>
# 第四个参数可以用 replay_install_switches.sh 留下的 <临时目录>/data。
set -eu
REPO=$1
PY=$2
WORK=$3
START=$4
export PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
mkdir -p "$WORK"
cp -a "$START" "$WORK/data"
DATA="$WORK/data"
ROOT=$(ls -d "$DATA"/datasets/football-inputs/*/sources | head -1)
now() { PYTHONPATH="$REPO/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }
later() { PYTHONPATH="$WORK/later/src" "$PY" -m polymarket861 --data-dir "$DATA" "$@"; }
field() { "$PY" -I -c 'import json,sys
d = json.load(open(sys.argv[1]))
print(json.dumps({k: d.get(k) for k in sys.argv[2:]}, ensure_ascii=False))' "$@"; }
status() { if "$@" > "$WORK/out.json" 2> "$WORK/err.txt"; then echo "exit=0"; else echo "exit=$? $(head -c 200 "$WORK/err.txt" | paste -sd' ' -)"; fi; }
# next_fixture: an EPL fixture that kicks off more than 30 hours from now and has no seal yet.
next_fixture() {
  "$PY" -I -c 'import sqlite3,sys,datetime
db=sqlite3.connect("file:"+sys.argv[1]+"/runtime.sqlite3?mode=ro",uri=True)
edge=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
print(db.execute("select fixture_id from fixture_versions where kickoff > ? and fixture_id not in (select fixture_id from forecast_seals) group by fixture_id order by min(kickoff), fixture_id limit 1",(edge,)).fetchone()[0])' "$DATA"
}

echo "== 未登记"
now leagues news-endpoints report --version v2 > "$WORK/report0.json"
field "$WORK/report0.json" version plan_sha256 state bound_seals family_comparisons

echo "== 登记（只在这份副本上）"
now leagues news-endpoints register --version v2 > "$WORK/reg1.json"
field "$WORK/reg1.json" version plan_sha256 mode ends_at news_policy
echo "== 原样再登记一次"
now leagues news-endpoints register --version v2 > "$WORK/reg2.json"
"$PY" -I -c 'import json,sys; print("same certificate:", json.load(open(sys.argv[1])) == json.load(open(sys.argv[2])))' "$WORK/reg1.json" "$WORK/reg2.json"
echo "== 换一个开始时间再登记"
status now leagues news-endpoints register --version v2 --starts-at 2027-01-01T00:00:00Z

echo "== 登记之后做一次提前封存"
status now forecast make "$(next_fixture)" "$ROOT" --horizon early
"$PY" -I -c 'import sqlite3,sys
db=sqlite3.connect("file:"+sys.argv[1]+"/runtime.sqlite3?mode=ro",uri=True)
print("bindings:", db.execute("select count(*) from news_endpoint_v2_bindings").fetchone()[0])' "$DATA"
now leagues news-endpoints report --version v2 > "$WORK/report1.json"
field "$WORK/report1.json" state bound_seals

echo "== 以后某一版：方案文字多一个逗号"
mkdir -p "$WORK/later"
cp -R "$REPO/src" "$WORK/later/src"
"$PY" -I -c 'import pathlib,sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
old = "\"reporting\": \"Before the fixed end publish only coverage"
assert s.count(old) == 1, "anchor not found exactly once"
p.write_text(s.replace(old, "\"reporting\": \"Before the fixed end, publish only coverage"))' "$WORK/later/src/polymarket861/news_endpoint_v2.py"
echo "plan digest of this build:   $(PYTHONPATH="$REPO/src" "$PY" -c 'import polymarket861.news_endpoint_v2 as v; print(v.proposal()["plan_sha256"])')"
echo "plan digest of the later one: $(PYTHONPATH="$WORK/later/src" "$PY" -c 'import polymarket861.news_endpoint_v2 as v; print(v.proposal()["plan_sha256"])')"
echo "-- report --version v2";   status later leagues news-endpoints report --version v2
echo "-- report (default)";      status later leagues news-endpoints report
echo "-- register --version v2"; status later leagues news-endpoints register --version v2
echo "-- season readiness";      later season readiness --year 2026 --round 12 > "$WORK/ready.json"
"$PY" -I -c 'import json,sys; r = json.load(open(sys.argv[1]))["release"]; print(json.dumps({k: r.get(k) for k in ("state", "ready")}))' "$WORK/ready.json"
echo "-- forecast make";         status later forecast make "$(next_fixture)" "$ROOT" --horizon early
echo "== 回到原来这一版，登记仍然读得出"
now leagues news-endpoints report --version v2 > "$WORK/report2.json"
field "$WORK/report2.json" state bound_seals
