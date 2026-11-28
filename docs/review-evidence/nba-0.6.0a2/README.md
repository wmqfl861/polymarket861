# 0.6.0a2 复审证据及复现入口

目录内两位复核员的脚本和输出对应冻结的源码 Git 树 `d87741329d10fe9b31c4defae464dcd55d6c76b8`。原始命令日志保留当时使用的工作目录。数据组签署的 20 项结果摘要为 [summary.json](data/summary.json)。系统组的关键结果见[错钟和改期](system/extremes-frozen.json)、[盘口原件](system/originals-frozen.json)、[六仓账本副本](system/football-frozen.json)和[安装包复验](system/install-frozen.json)。原 Claude 七情景在本版上的输出为 [pipeline.json](pipeline.json)。

下列命令从已安装仓库研究依赖的环境运行。先把当前目录记为 `repo`，在临时目录导出已签版本。发布提交的 Git 提交者字段可能与签核时不同；两者的整仓树相等。本目录保留原提交对象，可重建同一个本地签核号。

```bash
repo=$PWD
evidence="$repo/docs/review-evidence/nba-0.6.0a2"
signed=$(git hash-object -t commit -w "$evidence/frozen-source.commit")
test "$signed" = dd2d67257812a46803baf8638203827c6f34e589
test "$(git rev-parse "$signed^{tree}")" = d87741329d10fe9b31c4defae464dcd55d6c76b8
audit=$(mktemp -d)
git archive "$signed" | tar -C "$audit" -xf -
export PYTHONPATH="$audit/src:$audit/tests" AUDIT_SOURCE="$audit"
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
python "$evidence/data/data_audit.py" --run players
python "$evidence/data/data_audit.py" --run schedule
python "$evidence/data/forward_14.py" positive
python "$evidence/data/forward_14.py" gap
python "$evidence/data/forward_14.py" off
python "$evidence/system/audit_http.py" --out "$audit/comparison.json" compare_2_resample
python "$evidence/system/audit_fixed.py" "$audit"
(cd "$audit" && python -m unittest discover -s tests -p 'test_nba*.py')
```

`data/freeze_checks.py` 是独立审核当时针对其环境的批量驱动，记录原解释器、旧库、缓存及工作目录布局；上面的单项运行只需源码与研究依赖。足球六仓的原始线上成交证据始终在部署机，公开场景用合成私账检验新旧引擎的重放与归档行为。安装演练的 GitHub Actions 工作流另行验证真实 Linux 用户级 systemd。
