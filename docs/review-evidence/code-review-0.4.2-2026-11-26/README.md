# 审核证据 · 0.4.2 与五大联赛 0.5 蓝图 · 2026-11-26

本目录是两份审核稿里“实测”和“推算”结论的复现脚本和当次输出，对应 [0.4.2 复核](../../polymarket861-EPL-code-review-0.4.2.md)和[五大联赛 0.5 计划审核](../../polymarket861-five-leagues-0.5-plan-review.md)。它不属于系统代码，也不修改任何仓库文件。审核对象是提交 `8125d57`（0.4.2）和 `88c7a82`（0.5 蓝图，源码和测试与 `8125d57` 相同）。升级对照的旧版本是 `ea58105`（0.4.1）和 `12d1717`（0.3.0）。

## 0.4.2 复核用到的脚本

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `check_player_weights.py` | 第 12 轮十场提前封存里 90 条伤停的球员权重和两队进球率的调整量，都能由公布的出场场次、首发和分钟重算；统计回退到中性系数 0.55 的条数和原因（R1-2、R3-2、S2-1） | 只读 `reports/news-early-seals-0.4.2.json`，不导入本项目的包 | 不到 1 秒 |
| `upgrade_replication.sh` | 旧版本的库原地换成 0.4.2 后，`season readiness` 给出待批准和三步操作；未批准时 `forecast make` 以退出码 2 拒绝封存；没有旧版本保存的基线时 `release-compare` 报错；按手册顺序做完基线、比较、批准后，1,900 场里 0 场改变，得分 75.447813，旧封存原样重开（R0-1、S0-1） | 需要能访问 GitHub 以取得固定版本的 OpenFootball 文件；全部在临时目录里进行 | 约 50 秒 |
| `check_recipe_digest.py` | 同一份 0.4.2 源码、同样的 NumPy 和 SciPy，内置配方的摘要在 Python 3.11、3.12、3.13 上各不相同，差别只来自两个按语法树取摘要的部分（S1-1，计划审核的 P0-4） | 从 `--src` 导入本项目的包，只读 | 约 1 秒 |

这次还沿用了[上一个证据目录](../code-review-0.4.1-2026-11-26/)里的四个脚本，在 0.4.2 上原样重跑：`check_published_probabilities.py`（600 个概率）、`probe_usage_directory.py`（R1-1、R1-2）、`probe_fixed_seal_schedule.py`（R1-5）、`probe_storage.py`（R3-1）。上次的 `probe_power.py` 和 `probe_absorption.py` 在 0.4.2 上不能运行，分别报 `KeyError: 'planning_assumptions'` 和 `KeyError: 'duration_steps_days'`，因为它们读取的策略字段在 0.4.2 里按审核建议换掉了。这两处改用开发方自己的复现脚本 `scripts/reproduce_news_market_study.py` 和 `scripts/reproduce_news_absorption.py` 核对。

## 0.5 计划审核用到的脚本

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `fetch_openfootball.py` | 取得 `openfootball/football.json` 固定提交 `e6744429ee395bc86f247348c6184bb08d4eb361` 的文件：五个联赛、2010/11 到 2026/27 共 85 个地址，其中 76 份存在，9 份返回 404。下面三个脚本读这个目录 | 需要能访问 `raw.githubusercontent.com` | 约 16 秒 |
| `check_history_evidence.py` | 蓝图 `evidence/history.json` 的比分盘点逐项重算：五季分母 8,908、8,887、8,884，各联赛的场数、轮数、球队数和 0 比 0 的写法，差异为零（第一组） | 只读固定档案和 `history.json`，不导入本项目的包 | 不到 1 秒 |
| `check_market_evidence.py` | 逐场盘口表 `market-season-2025-26.tsv` 的重算：1,752 场应有、1,730 场配上、22 场没配上及其原因；单队和全场大小球各自从哪一天开始挂出；主线不是 2.5 的场次（第三组、第四组，P0-1、P3-3） | 只读固定档案、逐场表和 `polymarket.json` | 不到 1 秒 |
| `probe_league_baselines.py` | 五个联赛用 0.4.2 原样引擎的逐场滚动回测：B1 对 B0 的分差和 10 重校正区间；3×3 网格相对默认配方的变化（P3-1）；没有别名表时 23 家俱乐部被拆开的后果（P0-3）；训练用到的 40 份文件逐个盘点（P1-1）；前向门槛逐季回放（P3-2）；各端点的区间（P3-3）；升班马（P3-4）。英超五季得 75.447813，与 0.4.2 发布的数字一致，用来确认这份重写没有走样 | 从 `--src` 导入 `fit_team_model`、`probabilities`、`score_probabilities` 和 `clustered_t`，不修改它们；读固定档案 | 4 个核心约 96 秒 |
| `check_gate_statistics.py` | 各项样本量门槛的通过概率（P2-3）、前向门槛的把握（P3-2）、95% 覆盖下限的取整、资金占用（P4-2）、抓取量（P4-3）、每晚一周少收的场次（P0-1） | 只用 SciPy 的纯算术，不读项目数据 | 约 1 秒 |
| `scan_league_assumptions.py` | 全包 83 个模块里哪些带英超假设，蓝图的两张代码落点表点到了哪些：54 个有命中，23 个有命中而没有被点名，其中 8 个带赛季形状常数（P5-1） | 只读源码和蓝图的两份文档 | 不到 1 秒 |
| `probe_empty_wire_reply.py` | 现有采集器收到“零条、空列表”的伤停回复时，对阿森纳和四家非英超俱乐部都记为 `ok`（P2-1） | 从 `--src` 导入 `news_sources`，回复是合成的，不联网 | 约 1 秒 |

## 输出

`outputs/` 是 2026-11-26 在审核环境里运行得到的输出。与上表脚本同名的有 `check-player-weights.json`、`check-history-evidence.json`、`check-market-evidence.json`、`probe-league-baselines.json`、`check-gate-statistics.json`、`scan-league-assumptions.json`、`probe-empty-wire-reply.json`。另有：

- `environment-and-tests.txt`：三个 Python 版本和数值库的版本；全部 476 项测试在三个版本上的结果（接了真实的 Chromium，零跳过）；独立复审签署的 `src`、`tests` 两棵树与仓库的对应；GitHub Actions 在 `8125d57` 和 `88c7a82` 上的结果；模块数和改动规模。
- `upgrade-replication-0.4.1-to-0.4.2-py312.txt`、`upgrade-replication-0.3.0-to-0.4.2-py312.txt`、`upgrade-replication-0.4.1-to-0.4.2.txt`：升级演练的全部输出，前两份用 Python 3.12，第三份用 3.13。处置说明里的两个比较摘要只在 3.12 下能复现。
- `recipe-digest-by-python.json`：`check_recipe_digest.py` 在三个版本上的输出合在一起，并列出哪些部分随版本变化。
- `developer-script-reruns.json`：开发方三个复现脚本的重跑记录和与发布报告的逐字节比较。吸收报告在 3.12、3.13 上相同；历史重建报告在 3.12、3.13 上相同，在 3.11 上 1,760 个数里有 8 个最后一位不同；盘口研究报告只有换回 0.4.1 的封存作输入才相同（S1-2、S2-6）。
- `market-study-with-0.4.2-moves.json`：`scripts/reproduce_news_market_study.py` 以 0.4.2 的封存为输入的全部输出，760 场、斜率 0.5 一格的检出率是 17.95%（S1-2）。
- `rerun-0.4.1-check-published-probabilities.txt`、`rerun-0.4.1-probe-usage-directory.json`、`rerun-0.4.1-probe-fixed-seal-schedule.json`、`rerun-0.4.1-probe-storage.json`：上一次审核的四个脚本在 0.4.2 上原样重跑的结果。

`ruff check src tests scripts`、对三个模块的 `mypy --strict`、`git diff --check` 和发行包与源码树的逐字节比较（86 个文件）在审核时都运行并通过，终端输出没有另存。前两项与仓库持续集成的步骤相同，可以由上面两次 GitHub Actions 的记录核对。

## 运行方法

在仓库根目录，Python 3.11 以上，已安装 NumPy、SciPy、httpx。`$HISTORY` 是存放固定档案的空目录，`$SCRATCH` 和 `$SCRATCH2` 是两次升级演练各用一个的空临时目录，`$OUT` 是放输出文件的目录。0.4.2 复核里用到 `$HISTORY` 的那一条，要先运行下面的 `fetch_openfootball.py`。

```sh
export PYTHONDONTWRITEBYTECODE=1
E=docs/review-evidence/code-review-0.4.2-2026-11-26
E1=docs/review-evidence/code-review-0.4.1-2026-11-26

# 0.4.2 复核
python -I $E/check_player_weights.py reports/news-early-seals-0.4.2.json
python $E1/check_published_probabilities.py reports/news-early-seals-0.4.2.json
PYTHONPATH=src:tests python $E1/probe_usage_directory.py
PYTHONPATH=src:tests python $E1/probe_fixed_seal_schedule.py
PYTHONPATH=src:docs/review-evidence/code-review-0.4.0-2026-11-25 python $E1/probe_storage.py
sh $E/upgrade_replication.sh "$PWD" "$(command -v python)" "$SCRATCH"            # 从 0.4.1 升级
sh $E/upgrade_replication.sh "$PWD" "$(command -v python)" "$SCRATCH2" 12d1717   # 从 0.3.0 升级
python -I $E/check_recipe_digest.py --src src                                    # 分别用 3.11、3.12、3.13 运行
PYTHONPATH=src python scripts/reproduce_news_market_study.py reports/news-early-seals-0.4.2.json    # 输出见 market-study-with-0.4.2-moves.json
PYTHONPATH=src python scripts/reproduce_news_market_study.py reports/news-early-seals-0.4.1.json    # 与 reports/news-market-study-0.4.2.json 相同
PYTHONPATH=src python scripts/reproduce_news_absorption.py                                         # 与 reports/news-absorption-0.4.2.json 相同
PYTHONPATH=src python scripts/reproduce_news_history.py "$HISTORY" --output "$OUT/news-history.json"   # 与 reports/news-history-0.4.2.json 相同（3.12、3.13）

# 0.5 计划审核
python $E/fetch_openfootball.py "$HISTORY"
python -I $E/check_history_evidence.py "$HISTORY" docs/five-leagues-0.5/evidence/history.json
python -I $E/check_market_evidence.py "$HISTORY" docs/five-leagues-0.5/evidence/market-season-2025-26.tsv docs/five-leagues-0.5/evidence/polymarket.json
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python -I $E/probe_league_baselines.py --src src "$HISTORY" "$OUT/probe-league-baselines.json"
python -I $E/check_gate_statistics.py
python -I $E/scan_league_assumptions.py .
python -I $E/probe_empty_wire_reply.py --src src
```

几点说明：

- 除 `fetch_openfootball.py` 和 `upgrade_replication.sh` 外全部离线运行。审核环境的网络策略不允许访问 Polymarket、Touchline Wire、OpenLigaDB、Wikimedia、各联赛和俱乐部网站、Zenodo 和 Figshare，所以没有一个脚本访问这些接口，也无法重新采集随仓证据里的实采内容。
- 读取下载档案的脚本都带 `-I` 运行。这个开关让解释器不把脚本所在目录和当前目录加进导入路径，也不读 `PYTHONPATH`，所以包的位置用 `--src` 传入。
- 提交前我把上面的命令从头又跑了一遍（Python 3.12.3，`check_recipe_digest.py` 三个版本各一遍），输出与 `outputs/` 里的文件逐字节相同，注明“与某份报告相同”的四条也都相同。例外有两处：`probe_empty_wire_reply.py` 的输出里有一个取自当前时钟的字段；`upgrade_replication.sh` 的输出里有批准时刻、旧封存的摘要和耗时，其余各行相同。`check_recipe_digest.py` 每次只输出当前解释器的一段，与合并文件里对应的一段相同。换一个解释器或数值库版本时，`probe_league_baselines.py` 的 `environment` 一段会不同，分数在末位也可能有差别。
- `probe_league_baselines.py` 要把数值库设为单线程，否则几个进程互相争抢会慢十倍以上。可选参数 `--workers` 和 `--leagues` 用来只跑一部分。四个新联赛只评 2021/22 到 2024/25，蓝图留作留出季的 2025/26 没有载入。脚本里的 `ALIASES` 是我为这次回测整理的 27 条写法对照，不是正式的身份表。
- `probe_empty_wire_reply.py` 的回复是合成的，表头字段照来源真实回复的样子写，数据为空。它证明的是采集器怎样处理这种回复，不是来源对这些俱乐部实际返回什么。
- `check_gate_statistics.py` 里的真实正确率、模型差距这类输入是假设值，每一段都注明对应蓝图的哪一节。
- `upgrade_replication.sh` 里的具名批准只发生在脚本自己建立的临时目录。新旧两个版本要用同一个解释器，因为内置配方包含 NumPy 和 SciPy 的版本。
- `scan_league_assumptions.py` 按文字特征找命中，注释和文档字符串里的也算，因为其中不少是给用户看的报错文字。它给出的是要逐个看的清单，不是缺陷清单。
