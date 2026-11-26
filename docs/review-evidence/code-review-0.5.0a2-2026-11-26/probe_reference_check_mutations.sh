#!/usr/bin/env sh
# 0.5.0a2 新增的两项配方保护在改动面前的表现。全部在源码副本上做，不改仓库。
#
#   1. scripts/check_recipe_references.py（持续集成新增的一步）：配方内的代码新增一处没有登记的跨文件调用、
#      用拼出来的名字间接调用、删掉一处已登记的调用，三种改动各做一次，看检查是否以非零状态退出。
#   2. 五国回测新增的研究配方（research_recipe）：只删掉西甲的一条写法对照（RC Celta），
#      重新生成五国回测，看研究配方的摘要和哪些联赛的俱乐部键变了。
#
# 用法: probe_reference_check_mutations.sh <仓库> <装好 numpy、scipy 的 python 3.12> <空的临时目录> [固定版本的 OpenFootball 档案目录]
# 第四个参数省略时只做第 1 项。
set -eu
REPO=$1
PY=$2
WORK=$3
HISTORY=${4:-}
export PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
mkdir -p "$WORK/copy"
cp -R "$REPO/src" "$REPO/scripts" "$WORK/copy/"
cd "$WORK/copy"
TARGET=src/polymarket861/football.py
cp "$TARGET" "$WORK/football.orig"

check() {
  if "$PY" -I scripts/check_recipe_references.py > "$WORK/check.json"; then code=0; else code=$?; fi
  "$PY" -I -c 'import json,sys
d = json.load(open(sys.argv[1]))
def one(rows):
    return sorted({" -> ".join(r[1:]) for r in rows})
print(json.dumps({"exit": int(sys.argv[2]), "unreviewed": one(d["unreviewed"]), "obsolete": one(d["obsolete"])}, ensure_ascii=False))' "$WORK/check.json" "$code"
}
edit() {
  "$PY" -I -c 'import pathlib,sys
p = pathlib.Path(sys.argv[1]); s = p.read_text()
assert s.count(sys.argv[2]) == 1, "anchor not found exactly once"
p.write_text(s.replace(sys.argv[2], sys.argv[3]))' "$@"
}
ANCHOR='    return team_key(name) if league is None else league_registry.club_key(league, season, name)'

echo "== 副本未改动"
check
echo "== 配方内的 football._club_key 新增一处对 league_registry.leagues 的调用（排除表里没有）"
edit "$TARGET" "$ANCHOR" "    league_registry.leagues() if league else None
$ANCHOR"
check
cp "$WORK/football.orig" "$TARGET"
echo "== 同一处改成用拼出来的名字间接调用"
edit "$TARGET" "$ANCHOR" "    getattr(league_registry, 'lea' + 'gues')
$ANCHOR"
check
cp "$WORK/football.orig" "$TARGET"
echo "== 删掉一处已登记的调用（league_registry.club_key）"
edit "$TARGET" "$ANCHOR" "    return team_key(name)"
check
cp "$WORK/football.orig" "$TARGET"
echo "== 复原"
check

if [ -n "$HISTORY" ]; then
  echo "== 只删掉西甲的一条写法对照 \"RC Celta\"，重新生成五国回测"
  edit src/polymarket861/league_registry.py '"RC Celta": "RC Celta de Vigo", ' ''
  PYTHONPATH="$WORK/copy/src" "$PY" -m polymarket861 leagues baseline-study "$HISTORY" --workers 4 --output "$WORK/study.json" >/dev/null
  "$PY" -I -c 'import json,sys
a = json.load(open(sys.argv[1])); b = json.load(open(sys.argv[2]))
ma, mb = a["research_recipe"]["manifest"], b["research_recipe"]["manifest"]
def leaves(x, p=""):
    if isinstance(x, dict):
        for k, v in x.items(): yield from leaves(v, p + "/" + str(k))
    elif isinstance(x, list):
        for i, v in enumerate(x): yield from leaves(v, p + "/" + str(i))
    else: yield p, x
A, B = dict(leaves(a)), dict(leaves(b))
changed = sorted(k for k in set(A) | set(B) if A.get(k) != B.get(k))
print(json.dumps({
    "research_recipe_published": a["research_recipe"]["sha256"],
    "research_recipe_after_deletion": b["research_recipe"]["sha256"],
    "leagues_whose_club_keys_changed": [k for k in ma["effective_club_keys_sha256"] if ma["effective_club_keys_sha256"][k] != mb["effective_club_keys_sha256"][k]],
    "package_files_changed": [k for k in ma["package_files_sha256"] if ma["package_files_sha256"][k] != mb["package_files_sha256"][k]],
    "report_values_changed": len(changed),
    "leagues_with_changed_results": sorted({k.split("/")[2] for k in changed if k.startswith("/leagues/")}),
}, ensure_ascii=False, indent=1))' "$REPO/reports/five-leagues-0.5.0a2/baseline-study.json" "$WORK/study.json"
fi
