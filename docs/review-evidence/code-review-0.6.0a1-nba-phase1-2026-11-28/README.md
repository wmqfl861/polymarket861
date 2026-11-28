# NBA 0.6.0a1 第一阶段代码审核 · 复现材料

对应审核稿：[NBA 0.6.0a1 第一阶段代码审核与第二阶段准入意见](../../polymarket861-nba-phase1-code-review-0.6.0a1.md)。审核对象是分支 `development/nba-reviewed` 的 `64b991d`（PR #2，安装包 0.6.0a1）。

所有脚本都不读写仓库，不触碰任何真实数据目录，不联网，只写自己建的或命令行给出的临时目录。脚本用 `python -I` 运行，源码和数据的路径用参数传入。实测环境是 Python 3.12.3、numpy 2.3.5、scipy 1.17.0、httpx 0.28.1；测试和持续集成各步另在 3.11.18 和 3.13.18 上各跑一遍。

有两份外部文件不随仓库分发，要自己下载，各放进一个单独的空目录：FiveThirtyEight 的 `nbaallelo.csv`，和 OpenFootball 固定提交的五联赛文件（76 个）。下载方法在“命令”一节。

## 脚本

| 脚本 | 回答什么问题 | 输出 | 耗时 |
|---|---|---|---|
| `probe_nba_pipeline_end_to_end.py` | 把免费赛程同步、只读盘口采集、封存与评分接在一起跑一场比赛，不替换任何内部函数。七个情景：盘口先到、封存先跑、两个来源开赛时刻差 10 分钟、接口不认 `season_type` 筛选、来源给出 `neutral_site=false`、来源不给 `datetime`、交易所兑付与比分不一致（Y1-1、Y1-2、Y1-3、Y1-5、Y2-2） | `probe-nba-pipeline-end-to-end.json` | 约 10 秒 |
| `probe_nba_pairing_by_timer_phase.py` | 用仓库自己的 `markets.collect` 和 `operations.forecast_cycle`，在模拟时钟上按 11 种相位差、12 种触发偏移、两组网络延迟逐分钟触发，数封存时配上盘口的次数（Y1-1） | `probe-nba-pairing-by-timer-phase.json` | 约 50 秒 |
| `probe_nba_model_inputs.py` | 自己写的回放循环是否与随仓报告逐位一致；仓库的回放有没有用到未来比分；把赛地置为未知、把历史截短到 14 天、28 天、本季至今、外加 1 个或 3 个往季，把得分水平抬高 15 分，各自对 N0、N1、N2 的影响；已发布配置的校准斜率和区间覆盖率（Y1-2、Y1-4、Y3-3） | `probe-nba-model-inputs.json` | 约 4 分钟 |
| `probe_isolation_and_recipe_pin.py` | 在七种位置各插一条不改变行为的语句，持续集成的配方检查过不过；本版两个方向的目录守卫（命令入口、直接调用、子目录）；0.5.0a4 的源码能不能打开 NBA 目录，root 和普通用户各一遍（X4-1、X4-3） | `probe-isolation-and-recipe-pin.json` | 约 30 秒 |
| `probe_paper_calibration_after_fix.py` | 足球推荐脚本改用连续校正之后，用产品自己的滚动回测重算 2025/26 留出季 1,745 场，直接调用部署脚本的校正函数：各方向校正前、校正后与实际频率，上轮点名的几个概率区间，再拟合斜率（W1-2） | `probe-paper-calibration-after-fix.json` | 约 2 分钟 |

`outputs/environment-and-tests.txt` 记录了三个解释器上的测试结果、持续集成各步在本地的结果、配方摘要、两份回放报告重跑后的摘要和 GitHub Actions 的运行编号。

## 命令

```sh
REPO=<64b991d 的检出目录>; E=$REPO/docs/review-evidence/code-review-0.6.0a1-nba-phase1-2026-11-28
cd $REPO

# 测试和持续集成的各步（三个解释器各跑一遍；PM861_TEST_CHROMIUM 指向一个可用的 Chromium）
python -m unittest discover -s tests -v
python scripts/check_league_boundaries.py && python scripts/check_recipe_references.py && python scripts/check_paper_bindings.py
ruff check src tests scripts deploy
mypy --strict --follow-imports=silent src/polymarket861/{blob_storage,score_bounds,experiment_statistics}.py
python scripts/check_recipe_identity.py --approved scripts/football-recipe-baseline.json > recipe-3.12.json   # 3.11、3.13 同样各一份，再 --compare

# 外部文件，各放进一个单独的空目录（D1、D2）；旧版源码解到 O
curl -fsSL -o "$D1/nbaallelo.csv" https://raw.githubusercontent.com/fivethirtyeight/data/master/nba-elo/nbaallelo.csv
python -I docs/review-evidence/code-review-0.4.2-2026-11-26/fetch_openfootball.py "$D2"
git archive 60c1a2d src | tar -x -C "$O"

# 随仓的两份回放报告，重跑后应逐字节相同
pm861 nba model backtest --archive "$D1/nbaallelo.csv" --first-season 2005 --last-season 2014 --lag-days 1 --replicates 10000
pm861 nba model backtest --archive "$D1/nbaallelo.csv" --first-season 2005 --last-season 2014 --lag-days 2 --replicates 10000

# 五个探针（T 是空的临时目录）
python -I $E/probe_nba_pipeline_end_to_end.py --src src --tests tests
python -I $E/probe_nba_pairing_by_timer_phase.py --src src --tests tests
python -I $E/probe_nba_model_inputs.py --src src --archive "$D1/nbaallelo.csv" --report reports/nba-phase1/baseline-538-lag24.json
python -I $E/probe_isolation_and_recipe_pin.py --repo . --old-src "$O/src" --unprivileged-uid 65534   # 最后一个参数要以 root 运行
python -I $E/probe_paper_calibration_after_fix.py --src src --raw "$D2" \
    --report reports/five-leagues-0.5.0a2/baseline-study.json \
    --feed deploy/paper-automation/five-automation/recommendation-feed "$T"
```

## 开发方修复之后，这些探针应当变成什么样

审核稿第 5 节每一条的“怎样算通过”都落在这几个输出上：

| 发现 | 探针 | 现在的结果 | 修复后应当是 |
|---|---|---|---|
| Y1-1 | `probe_nba_pairing_by_timer_phase.py` | 相位 0、2、5 秒是 0/12，最好的一列是 10/12 | 每一列不少于 11/12 |
| Y1-1 | 端到端的 `forecast_first` 情景 | 四类玩法都是 `missing_decision_quote: 1` | `with_decision_quote: 1` |
| Y1-2 | 端到端的 `market_first` 情景 | `venue_basis: unknown_home_advantage_withheld`，N0 主胜概率 0.5 | 赛地依据不是未知，N0 主胜概率不是 0.5 |
| Y1-2 | `probe_nba_model_inputs.py` 的 `venue_unknown_all_rows` | N2 的 Brier 是 0.21399 | 按新的默认规则重跑，不高于 0.2065 |
| Y1-3 | 端到端的 `season_type_ignored`、`no_datetime` 情景 | 同步和封存都报 `ok`，封存 0 场 | 状态不是 `ok`，写明原因 |
| Y1-5 | 端到端的 `kickoff_mismatch` 情景 | T−60m 窗口 `missed` | `sealed`；把脚本里的 10 分钟改成 180 分钟仍然拦住 |
| Y2-2 | 端到端的 `payout_disagrees` 情景 | 日报、状态里看不出分歧 | 日报里出现一条分歧 |

修复会改变函数签名或报表字段的话，探针要跟着改，以输出的含义为准。

## 读这些输出时要知道的几点

- 端到端和定时器相位两个探针里，交易所一侧是 `tests/test_nba_markets.py` 的 `Venue` 夹具：一场热火对尼克斯，五个合约，规则文本是开发方从真实合约抄录的。赛程一侧的字段按 BALLDONTLIE 官方 Python SDK（2025 年 9 月 17 日的版本）的比赛模型加上 `datetime` 构造，历史是 20 天、60 场合成的常规赛。它们说明的是程序在这些输入下怎样走，不代表真实接口的返回。真实接口有没有 `season_type` 筛选、`datetime`、赛地字段，都未核实。
- 定时器相位探针用的是模拟时钟：每次触发算一个新进程，启动 1.0 秒；对每个主机的第一次请求 4.0 秒，后续 0.2 秒，取自开发方随仓回执（3.7 到 5.9 秒）。另一组是 0.3 秒和 0.1 秒的快网络。封存只读“开始时刻之前已经落库”的盘口行，所以探针里先执行哪个调用不影响可见性。它没有在真实的 systemd 上运行，“安装命令会让两个定时器同时触发”是按 systemd 文档的语义推出来的。
- `probe_nba_model_inputs.py` 的 `fidelity.identical_to_shipped_report` 为真，说明下面各变体用的循环与仓库的回放等价。`no_lookahead` 是直接调用仓库自己的 `backtest`，用一个记录器包住 `model.forecast`。`shift15` 是把归档里每队得分统一加 15 分，用来模拟今天每队约 114 分的得分水平；它只改水平，不改分差和波动，是合成数据。`last_14_days_only` 和 `last_28_days_only` 是每个比赛日重新起一个状态，只给它之前若干天的终场；`season_to_date_*` 是每个赛季重新起一个状态。这几种截短的配置各有 401 场不出预测，都在各季开头，`paired_vs_published_config_same_games` 是在出了预测的场次上逐场相减。所有变体都沿用回放的一天滞后。
- `probe_isolation_and_recipe_pin.py` 里 `store.connect` 一行标的是 `informational`：它不在配方里，改它检查照样通过，本版正是这样给它加了目录守卫。0.5.0a4 一侧用的是 `git archive` 解出的源码，没有另装 wheel；持续集成里的同类演练装的是 wheel，结果一致。
- `probe_paper_calibration_after_fix.py` 是样本内复验：部署脚本里的四组系数就是在这 1,745 场上拟合的，所以整体平均必然对得上，值得看的是分区间的残差和“各自校正再归一”带来的差别。区间按“联赛 × 官方轮次”共 182 个单位重抽样 2,000 次，种子固定。分区间的场次只有 70 到 516 场，区间很宽。
- 这台机器访问不了 Polymarket、BALLDONTLIE（包括它公开的接口文档）、Big Balls、DunkAPI、ESPN 和 NBA 官网。审核稿里凡是来自开发方取样或部署机的数字都标了“随仓证据”。
