# 审核证据 · 五大联赛 0.5.0a1 · 2026-11-26

本目录是[0.5.0a1 审核稿](../../polymarket861-five-leagues-code-review-0.5.0a1.md)里“实测”和“推算”结论的复现脚本和当次输出。它不属于系统代码，也不修改任何仓库文件。审核对象是提交 `49313f4`（0.5.0a1）。升级对照的旧版本是 `8125d57`（0.4.2）；回算历次发版时还用到 `12d1717`（0.3.0）和 `ea58105`（0.4.1）。

## 脚本

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `check_five_league_study.py` | 五国回测逐场核对：用上一轮写的回测脚本（不含任何联赛登记代码）和产品的 `leagues baseline-study` 各走一遍，8,877 场里 B1、B0 和四个分项的得分最大差异是 0；我的脚本多评的 7 场正好是开发方登记的重赛和续赛；各联赛和合并的十项区间独立重算（第 1 节、P0-3、P1-1、P3-2、P3-3、T2-3） | 从 `--src` 导入本项目的包；`--probe` 指向上一个证据目录的 `probe_league_baselines.py`；读固定版本的 OpenFootball 档案 | 4 个核心约 25 秒 |
| `upgrade_replication.sh` | 0.4.2 的数据目录原地换成 0.5.0a1：未批准时 `forecast make` 以退出码 2 拒绝；这时换回旧版本仍能封存；没有基线时 `release-compare` 报错；非 3.12 的解释器不能做基线和比较；按手册顺序做完后 1,900 场里 0 场改变；批准后的配方在另一个解释器上也能封存；旧封存原样重开；批准后旧版本被拒绝（S0-1、S1-1、T0-1、T2-6） | 需要能访问 GitHub 以取得固定版本的 OpenFootball 文件；全部在临时目录里进行；新版本要用 Python 3.12 | 约 1 分钟 |
| `check_recipe_cross_references.py` | 新配方纳入的 26 个文件、296 个定义里，跨文件引用有 155 处，其中 45 处指向不在配方里的定义：6 处是清单明示排除的，39 处没有说明，`league_registry.py` 占 8 个函数（T2-1） | 从 `--src` 导入 `recipe_identity`，只读源码 | 约 2 秒 |
| `trace_recipe_coverage.py` | 给一次真实的 `forecast make` 挂执行探针，把执行到的包内函数和配方实际纳入的定义逐个对照（T2-1） | 一份已经批准 0.5.0a1 的临时数据目录的副本，因为它会真的封存一份预测 | 约 5 秒 |
| `probe_recipe_mutations.py` | 六组改动试验：改岭惩罚，发布状态变为 `approval_required`、封存被拒；只加注释、改看板、删一条联赛别名，配方和英超概率都不变，后者让五国回测报告改变；改新闻调整上限，B1 配方不变而新闻配方的摘要变了（T2-1、T1-2） | 同一份临时数据目录（脚本自己逐组复制）；Python 3.12；`--history` 可选 | 约 4.5 分钟，不带 `--history` 约 1 分钟 |
| `probe_big5_missing_flags.py` | 四国采集在赛事记录没有 `live`、`ended` 标记时：订单簿照常存下 28 份，固定时窗和配对的完成数都是 0（T1-1） | 沿用 `tests/test_big5_market.py` 的合成赛事，不联网 | 不到 1 秒 |
| `probe_big5_watch_faults.py` | 值守循环在 13 种上游故障下会不会退出：连同对照和目录刷新的开与关共 56 种组合，全部正常返回（T2-4） | 同上 | 约 4 秒 |
| `probe_big5_report_during_watch.py` | 值守进程采集固定时窗期间，另一个进程不停地出周报和执行审核稿里的两条查询：采集完成的时窗数与没有干扰时相同，两边都没有异常（第 2 节） | 同上；模拟时钟按真实速度走 | 约 50 秒 |
| `probe_endpoint_study_power.py` | 新闻前瞻研究在十项校正下的把握：650、325、270 场，收盘噪声 3、5、8 个百分点，斜率 0、0.5、1，每格 2,000 次（T1-2） | 读 `reports/news-early-seals-0.4.2.json`；从 `--src` 调用产品的 `news_market.price_gap_slope` | 约 35 秒 |
| `check_era_file_churn.py` | 用 0.5.0a1 自己的摘要函数和清单回算最近三次发版：概率版本涉及的 39 个文件里摘要变了的分别是 17、9、14 个，9 个整文件计入的每次至少变 3 个（T1-2） | 从 `--src` 导入 `news_study_plan`、`recipe_identity`；用 `git archive` 读四个提交 | 约 5 秒 |
| `check_report_details.py` | 三处小的重算：南特对图卢兹四份兑付原件的哈希和终态（P4-1）；第 12 轮 90 条伤停里的 6 条停赛和它们占的权重（T2-2）；我得到的比较报告与发布的那份逐字段比较（T2-5） | 只读随仓报告，不导入本项目的包；`--compare` 指向升级演练留下的 `release-compare.json` | 不到 1 秒 |
| `estimate_weekend_capture_volume.py` | 值守采集一个周末的采集次数和磁盘用量（T2-4，推算） | 只读 `reports/five-leagues-0.5/market-observation.json` | 不到 1 秒 |

这次还沿用了[上一个证据目录](../code-review-0.4.2-2026-11-26/)里的三个脚本：`fetch_openfootball.py` 取固定版本的历史档案；`probe_empty_wire_reply.py` 和 `scan_league_assumptions.py` 在 0.5.0a1 上原样重跑（P2-1、P5-1）。

## 输出

`outputs/` 是 2026-11-26 在审核环境里运行得到的输出。与上表脚本同名的有 `check-five-league-study.json`、`check-recipe-cross-references.json`、`trace-recipe-coverage.json`、`probe-recipe-mutations.json`、`probe-big5-missing-flags.json`、`probe-big5-watch-faults.json`、`probe-big5-report-during-watch.json`、`probe-endpoint-study-power.json`、`check-era-file-churn.json`、`check-report-details.json`、`estimate-weekend-capture-volume.json`。另有：

- `environment-and-tests.txt`：审核对象和改动规模；三个 Python 版本和数值库的版本；全部 572 项测试在三个版本上的结果（接了真实的 Chrome for Testing，零跳过）；`ruff`、`mypy --strict`、`scripts/check_league_boundaries.py`、`git diff --check` 的结果；配方身份在三个解释器上的数值；发行包与源码树的比较（99 个文件）；独立复审签署的 `src`、`tests` 两棵树与仓库的对应；GitHub Actions 在 `49313f4` 上的结果。
- `upgrade-0.4.2-to-0.5.0a1-py312.txt`、`upgrade-0.4.2-py311-to-0.5.0a1-py312.txt`、`upgrade-0.4.2-py313-to-0.5.0a1-py312.txt`：升级演练的全部输出。三份的区别是旧版本跑在 3.12、3.11 还是 3.13 上；新版本都在 3.12 上，另一个解释器都是 3.13。
- `recipe-digest-by-python.json`：`scripts/check_recipe_identity.py` 在三个解释器上的输出合在一起。除 `python` 字段外完全相同。
- `developer-report-reruns.json`：开发方五份报告的重跑记录和与发布文件的逐字节比较。五国回测、历史清册、资金压力测试、新闻盘口研究相同；新闻吸收报告只有 `/news_policy` 一项不同，因为 0.5.0a1 改了新闻策略。
- `rerun-0.4.2-probe-empty-wire-reply.json`、`rerun-0.4.2-scan-league-assumptions.json`：上一轮的两个脚本在 0.5.0a1 上原样重跑的结果。

## 运行方法

在仓库根目录，已安装 NumPy、SciPy、httpx。`$PY312` 是装好依赖的 Python 3.12，`$PY311`、`$PY313` 是另外两个解释器，下面没有写解释器的命令都用 `$PY312`。`$HISTORY` 是存放固定档案的空目录，`$OUT` 是放输出文件的目录，`$A`、`$B`、`$C` 是三次升级演练各用一个的空临时目录，`$T1` 到 `$T7` 是其余脚本各用一个的空临时目录。

```sh
export PYTHONDONTWRITEBYTECODE=1
E=docs/review-evidence/code-review-0.5.0a1-2026-11-26
E0=docs/review-evidence/code-review-0.4.2-2026-11-26

# 五国回测
python $E0/fetch_openfootball.py "$HISTORY"
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python -I $E/check_five_league_study.py --src src \
    --probe $E0/probe_league_baselines.py --published reports/five-leagues-0.5/baseline-study.json \
    "$HISTORY" "$OUT/check-five-league-study.json"

# 升级演练：旧版本分别在 3.12、3.11、3.13 上
sh $E/upgrade_replication.sh "$PWD" "$PY312" "$A" 8125d57 "$PY313"
OLDPY="$PY311" sh $E/upgrade_replication.sh "$PWD" "$PY312" "$B" 8125d57 "$PY313"
OLDPY="$PY313" sh $E/upgrade_replication.sh "$PWD" "$PY312" "$C" 8125d57 "$PY313"

# 配方的覆盖范围（后两条用升级演练留下的、已经批准的临时数据目录 $A/data）
python -I $E/check_recipe_cross_references.py --src src
cp -R "$A/data" "$T1/data"
python -I $E/trace_recipe_coverage.py --src src --data "$T1/data" \
    --inputs "$T1"/data/datasets/football-inputs/*/sources --fixture epl/2026-27/nottingham-forest/chelsea
python -I $E/probe_recipe_mutations.py --src src --data "$A/data" --python "$PY312" --scratch "$T2" --history "$HISTORY"

# 四国采集
python -I $E/probe_big5_missing_flags.py --src src --tests tests "$T3"
python -I $E/probe_big5_watch_faults.py --src src --tests tests "$T4"
python -I $E/probe_big5_report_during_watch.py --src src --tests tests "$T5"
python -I $E/estimate_weekend_capture_volume.py reports/five-leagues-0.5/market-observation.json

# 新闻前瞻研究
python -I $E/probe_endpoint_study_power.py --src src reports/news-early-seals-0.4.2.json
python -I $E/check_era_file_churn.py --src src --repo . "$T6"

# 随仓报告的细节，上一轮的两个探针
python -I $E/check_report_details.py . --compare "$A/release-compare.json"
python -I $E0/probe_empty_wire_reply.py --src src
python -I $E0/scan_league_assumptions.py .

# 开发方的报告
python -m polymarket861 leagues baseline-study "$HISTORY" --output "$OUT/baseline-study.json"
python -m polymarket861 leagues inventory "$HISTORY" --output "$OUT/history-inventory.json"
python -m polymarket861 paper-risk stress --write-input "$T7/scenario.json" --output "$OUT/capital-stress.json"
PYTHONPATH=src python scripts/reproduce_news_market_study.py reports/news-early-seals-0.4.2.json
PYTHONPATH=src python scripts/reproduce_news_absorption.py
python scripts/check_recipe_identity.py            # 三个解释器各运行一次
```

几点说明：

- 除 `fetch_openfootball.py` 和 `upgrade_replication.sh` 外全部离线运行。审核环境访问不了 Polymarket（代理返回 403），也访问不了 Touchline Wire、OpenLigaDB 等来源，所以没有一个脚本访问这些接口，随仓报告里的实采内容无法重新采集。
- 读取下载档案或随仓数据的脚本都带 `-I` 运行。这个开关让解释器不把脚本所在目录和当前目录加进导入路径，也不读 `PYTHONPATH`，所以包和测试的位置用 `--src`、`--tests` 传入。开发方的命令（最后一组）按仓库文档的写法运行，要先设 `PYTHONPATH=src` 或安装这个包。
- 提交前我把上面的命令从头又跑了一遍（Python 3.12.3）。`check_five_league_study.py`、`check_recipe_cross_references.py`、`probe_recipe_mutations.py`、`probe_big5_missing_flags.py`、`probe_big5_watch_faults.py`、`probe_endpoint_study_power.py` 和 `scan_league_assumptions.py` 的输出与 `outputs/` 里的文件逐字节相同。`trace_recipe_coverage.py` 在另一份数据目录副本上重跑，内容相同。例外有三处：`probe_empty_wire_reply.py` 的输出里有一个取自当前时钟的字段；`upgrade_replication.sh` 的输出里有批准时刻和旧封存的摘要，其余各行相同；`probe_big5_report_during_watch.py` 里第二个进程完成的次数取决于机器快慢，时窗完成数不变。
- 四国采集的三个探针用的是仓库测试自带的合成赛事和订单簿。它们证明的是采集程序怎样处理这些情形，不是交易所实际返回什么。交易所在开球前是否给出 `live`、`ended` 标记，要看真实采集的结果。
- `probe_big5_watch_faults.py` 的故障在 `market_provider.get` 这一层注入。真实的网络层（`market_http.read`）把连接失败、超时和非 2xx 状态都转成 `OSError` 再交给它，与注入的情形一致。
- `upgrade_replication.sh` 里的具名批准只发生在脚本自己建立的临时目录。它会在临时目录里建一个旧版本的 git 工作树，结束时自己删掉。`trace_recipe_coverage.py` 和 `probe_recipe_mutations.py` 也只应当对临时数据目录使用。
- `check_five_league_study.py` 要把数值库设为单线程，否则几个进程互相争抢会慢很多。四个新联赛的 2025/26 留出季这次载入了，因为开发方已经打开并公布了它。
- `probe_endpoint_study_power.py` 的收盘价是合成的：收盘相对 B1 的差等于真实斜率乘以新闻调整量再加噪声，噪声方差的 5% 是整轮共有的。它只说明样本量够不够，不说明新闻有没有用。随机种子固定为 861。
- `check_era_file_churn.py` 用的是 0.5.0a1 的清单和摘要函数，回答的是“假如这项研究早就按现在的定义登记，过去每次升级会不会开启新的概率版本”。摘要按语法树取，只改注释的文件不算变化。
- `estimate_weekend_capture_volume.py` 是推算。随仓报告只有比赛日期，没有开球时刻，周六和周日的场次按三种开球时刻各算一遍。数据库的单位成本含一次性的目录和条款，是偏高的上限；原始回复按采样的平均大小算，并给出两个极端。
- `check_recipe_cross_references.py` 列出的是静态引用，不等于这些引用在英超正式路径上会被执行。39 处没有说明的引用我逐个看过，结论在审核稿 T2-1。
