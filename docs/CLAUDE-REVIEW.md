# Claude 技术审核交接

请从当前提交运行英超版本，核实赛前预测、逐轮评分、Polymarket 国际站的模拟入场及持有至结算。产品要覆盖胜平负、全场大小和主客队各自的大小球。数据源、球员与场外证据、错误修复和本地 Codex 迭代都在审核范围内。

本页记录 2026-11-25 的开发实测、复现方法和待核问题。审核依据为[首版蓝图与冻结参数](./polymarket861-EPL-v1.md#protocol)和[开发计划](./polymarket861-EPL-development-plan.md)。此前的 [Claude 审核报告](./polymarket861-EPL-review-handoff.md)及其[按日期截断的评分实验](./review-evidence/README.md)可作对照。本次代码冻结、独立审核结论及其复现依据见[独立审核记录](./INDEPENDENT-REVIEW.md)。请记录本次运行的发现、命令、退出码、所用提交和适用范围。

## 一、从干净仓库运行

以下命令使用 POSIX shell。项目要求 Python 3.11 及以上。固定报告记录的计算环境是 Python 3.12.14、NumPy 2.3.5 和 SciPy 1.17.0。请在报告中记录实际版本。用新建的临时目录区分个人已有的数据库、试验账户和本次审查输出。

```bash
git clone https://github.com/wmqfl861/polymarket861.git
cd polymarket861
git rev-parse HEAD
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -c "import sys,numpy,scipy; print(sys.version, numpy.__version__, scipy.__version__)"
python -m unittest discover -s tests -p 'test_*.py' -v
pm861 --help
review_work=$(mktemp -d)
state="$review_work/state"
input="$review_work/openfootball"
```

可选的交互测试需要 Python 的 `playwright` 包和可执行的本机 Chromium。设 `PM861_TEST_CHROMIUM` 为其路径后，运行 `python -m unittest discover -s tests -p test_dashboard.py -v`。普通测试会将这项环境依赖标为跳过。部署界面与导出的单文件 HTML 均使用本地资源。

## 二、复现五季概率回测和一轮成绩单

`football fetch` 固定 OpenFootball 的提交 `e6744429ee395bc86f247348c6184bb08d4eb361`，校验下载的赛季文件。2010/11–2026/27 的 17 份文件提供热身、来源验证和目标赛季数据。评分窗口仅为 2021/22–2025/26。第一次回测假定此前比赛的赛果在开球后 24 小时可用。第二次回测只接受有相应时间证据的赛果。

```bash
pm861 --data-dir "$state" football fetch "$input" --from-season 2010-11 --to-season 2026-27
pm861 --data-dir "$state" football backtest "$input" \
  --from-season 2021-22 --to-season 2025-26 --horizons 1440 60 --bootstrap 2000 --seed 861 \
  --archive-lag-hours 24 --summary-output "$review_work/lag.json" \
  --details "$review_work/lag.jsonl" > /dev/null
pm861 --data-dir "$state" football backtest "$input" \
  --from-season 2021-22 --to-season 2025-26 --horizons 1440 60 --bootstrap 2000 --seed 861 \
  --summary-output "$review_work/strict.json" --details "$review_work/strict.jsonl" > /dev/null
pm861 --data-dir "$state" forecast replay-round "$input" 2025-26 1 \
  --horizon T-60m --archive-lag-hours 24 > "$review_work/round.json"
```

B0 是联赛历史比分频率，B1 是球队级 Dixon–Coles 模型。下列配对差和区间均来自产品代码，按官方轮次整组重抽样。两个时点使用相同的 1,900 场。每个时点的模型动用了 1,895 次 B1 和 5 次 B0 回退，模型均分包含这 5 场。比较时可逐字段核对 `horizons`、`protocol`、`source.blobs` 和 `reproduce`。[产品回测报告](../reports/historical-football.json) · [同期时间凭据覆盖报告](../reports/historical-football-strict.json)

| 数据可用口径 | 时点 | 已评分／应评 | B0 均分 | 模型均分 | 模型减 B0，95% 区间 |
|---|---|---:|---:|---:|---|
| 开球后 24 小时可用的研究假设 | 赛前 24 小时 | 1,900／1,900 | 73.55917 | 75.44982 | +1.89065，1.54087 至 2.26539 |
| 同一假设 | 赛前 60 分钟 | 1,900／1,900 | 73.55917 | 75.44781 | +1.88864，1.53926 至 2.26230 |
| 严格截止，只有这份赛果快照、无同期证据文件 | 两个时点分别计算 | 各 0／1,900 | 不适用 | 不适用 | 截止前缺少可验证的历史赛果输入 |

2025/26 第 1 轮单独的 T−60 回放覆盖 10／10 场，四项等权均分 78.37328。它核验单场封存、汇总评分及覆盖分母。上述命令的输出用 `round_report` 包裹成绩单；跨运行目录复查时可比对比分、四项损失、命中和校准。单场封存摘要与首次收件时间随各自记录保存。完整字段见[逐轮成绩样本](../reports/round-2025-26-01.json)。真实 T−60 名单到达率、前向预测优势及扣费后收益仍须另行测量。2025/26 已由此前审核查看，按历史回归集使用。

## 三、核对行情覆盖和模拟收益分母

目录、逐笔成交、价格曲线、可执行的双时点订单簿是四种不同证据。复核前先约定统计单位。目录按比赛计数，历史证据分档按方向代币计数，回放机会表还包含未核实挂牌的玩法占位行。[行情覆盖、五场样本名及采集参数](../reports/historical-market-coverage.json)

| 已测范围 | 实际读数 | 核对方法 |
|---|---|---|
| 2025/26 国际站目录 | 380 场。胜平负映射 380 场，全场大小 297 场，主队及客队大小各 0 场 | 与固定快照的 380 个有序主客队组合逐一比对。0 是本次适配器在此目录中映射到的场数。 |
| 2026/27 当时已列目录 | 129 场，其中 110 场已过名义开球时刻。四种玩法各映射到 129 场 | 分母包括未来比赛，不能用 129 充当已完赛数、可成交数或整个赛季场数。 |
| 两季有意选取的 5 场历史样本 | 116 个挂牌方向代币中 A=0、B=12、C=18、缺证=86。计入 7 个玩法缺口后，回放共有 123 行 | 本次 21 个历史分页任务走完，24 个未走完。成交可与曲线同时存在，报表分档去重。个别比赛的赛程版本存在冲突。 |
| 诺丁汉森林对切尔西的赛前抽样 | 8 个不同代币各取得一份双边深度，8 份有独立的新鲜度凭据，明确声明赛前状态的有 0 份 | 这次观测早于决策窗口，每个代币只有一个采样时刻，合格 A=0。 |

这 5 场的历史可执行收益率为 `null`。模拟账本已实现盈亏为零可以表示没有已结算盈亏，0 笔合格成交不能产生 0% 的收益率。报告还保留了旧版球队简称导致 105 条派生映射发生变化的重建记录。复查时比较旧派生值、原始字节指纹和新版本。

要重新发现目录，可在独立数据目录执行以下命令。网络读取会获得审核当日能访问的官方记录。原采集时的盘口深度只能用当时保存的原始快照回放。公开仓库保留汇总及清单指纹，原始材料在私有运行目录。

```bash
pm861 --data-dir "$state" market catalog 2025 --page-size 30 --pages 80 > /dev/null
pm861 --data-dir "$state" market catalog-summary 2025
pm861 --data-dir "$state" market discover epl-liv-ars-2026-11-01 > /dev/null
pm861 --data-dir "$state" market markets epl-liv-ars-2026-11-01
pm861 --data-dir "$state" market coverage --fixture epl-liv-ars-2026-11-01
```

其余四场及完整采样窗口见行情报告的 `scope_and_reproduction`。取得对应事件 ID 后，用 `market history trades EVENT_ID` 续传。每个代币用 `market history prices TOKEN --start EPOCH --end EPOCH` 核对观测时刻和分辨率。原始记录到齐后，`trial study-archive` 通过可重复的 `--fixture` 参数选取历史样本，把归档来源与账本回放相连。`ledger-demo` 的已知输入仅用于验算逻辑，请把该账户放在单独的临时目录。

## 四、优先复核的 14 类风险

每一项请说明触发条件、实际输出、受影响的预测或账本以及可重复的最小案例。优先从正常 CLI 入口复现，再定位表中的函数或测试。已有测试可以扩展边界条件，不能单凭测试名称作出结论。

| 编号 | 复核对象及可观察的错误 | 优先代码与现有测试 |
|---|---|---|
| 01 | **赛果时钟和泄漏。** 修改 2021-08-13 晚场的比分、赛后发布或撤回时间，8 月 14 日曼联对利兹的 T−24 预测应保持一致。核对 UTC、夏令时、赛果修订、2020 年复赛错年及无资格历史输入。 | [历史加载与时间过滤](../src/polymarket861/football.py) · [比分测试](../tests/test_football.py) |
| 02 | **四项概率与模型降级。** 主客互换、0∶0、极端进球、升班马、新球队、阵容互斥情景。核对联合比分总质量、大小线单调性、B0/B1/B2 标记及同一球员事件是否重复计入。 | [比分模型及人员情景](../src/polymarket861/football_model.py) · [球队测试](../tests/test_football.py) |
| 03 | **免费字段、名单与天气新闻。** 十一人预计名单不能变成确认首发。撤销训练许可、转载、更正先到、天气起报早于实际分发均应改变可用证据。操作后对比同一场历史与真实前向的首次收到时间。 | [字段许可](../src/polymarket861/evidence_sources.py) · [事实查询](../src/polymarket861/evidence.py) · [新闻和天气测试](../tests/test_context_feeds.py) |
| 04 | **输入污染与根因修复。** 登记故障、隔离、找到特征和训练版本的间接依赖、实际修正确认标记。回访原始预测及修复后的重放值。追查训练模型是否真的重训、回退或仍待处理。 | [血缘与修复](../src/polymarket861/evidence.py) · [纠错用例](../tests/test_evidence.py) |
| 05 | **市场身份与契约。** 核对站点、合约代际、主/附事件、球队历史简称、全场／半场、主客单队、进球线、Yes/No 或 Over/Under 的代币对应。用 2∶0 和 0∶2 互验。 | [市场解析](../src/polymarket861/market_provider.py) · [映射与重建测试](../tests/test_market_archive.py) |
| 06 | **两次观测与先后次序。** 第一份应是决策截止前最新合格簿，第二份在意图封存后及明确延迟窗口内。尝试陈旧缓存、单侧簿、缺赛前标志、同一响应重复及实际开球前提不满足的路径。 | [采样与时间记录](../src/polymarket861/market_archive.py) · [订单簿检查](../src/polymarket861/ledger_rules.py) · [账本测试](../tests/test_ledger.py) |
| 07 | **历史分档及覆盖偏差。** 提供逐笔交易、曲线、缺签名或断续深度。核实 A/B/C/缺失分母、游标续传、终局价格排除、改期冲突、规则费表版本和原始归档指纹。A=0 时检查收益率为空。 | [历史归档](../src/polymarket861/market_archive.py) · [历史回放与归档核验](../src/polymarket861/ledger.py) · [行情测试](../tests/test_market_archive.py) |
| 08 | **成交量、费用与资金。** 两份卖盘价量不一致、费用收现金／份额、多意图竞争同一深度、最小份数、达到预算和同场最坏损失上限。并行重复执行，逐项对账净份数和余额。 | [逐档价格与费用](../src/polymarket861/ledger_rules.py) · [资金预留与事务](../src/polymarket861/ledger.py) · [账本测试](../tests/test_ledger.py) |
| 09 | **结算与收盘价格。** 从逐合约原文构造延期继续持有、取消、半额兑付、结果已确定后腰斩。森林对切尔西的指定无结果分支中平局 Yes 付 1。检验 CLV 同代币最后合格赛前中点、过期报价及重复结算。 | [情景兑付](../src/polymarket861/ledger_rules.py) · [结算和收盘记录](../src/polymarket861/ledger.py) · [两份规则实例](./polymarket861-EPL-development-plan.md) |
| 10 | **轮报和统计比较。** 复算蓝图 85.69 分，以及九场均分 80、一场漏发产生的 72–82 范围。分别注入延期、漏发、缺盘口、赛果更正、多盘口高度相关等情况。检查分母、校准分箱和按轮配对区间。 | [回测和评分](../src/polymarket861/football.py) · [轮报与更正](../src/polymarket861/pipeline.py) · [运行测试](../tests/test_runtime.py) |
| 11 | **前向调度与封存。** 在 T−24h／T−60 窗口前、窗口内、窗口后执行 `run due`。注入改期、两个调度器同时认领和封存后进程退出。重启不重复发布，历史重放和早期技术快照继续带原模式。 | [调度器](../src/polymarket861/runner.py) · [封存器](../src/polymarket861/pipeline.py) · [调度测试](../tests/test_runner.py) |
| 12 | **正式启用与候选晋级。** 让候选提交自报分数、减少困难场次或重复利用同一评测窗。查看真正封存的候选/基线、未来赛事分母、两项历史报告、同窗多重比较、风险账本和回滚。模型和字段来源使用对应准入指标。 | [正式启用条件](../src/polymarket861/qualification.py) · [实验验收器](../src/polymarket861/jobs_research.py) · [候选测试](../tests/test_jobs.py) |
| 13 | **多子代理执行边界。** 有依赖的并行任务经历超时、重派、旧进程恢复、Codex 不可用。检查每次尝试的隔离目录、输入与产物、租约持有者、状态、实际并发与用量。内建采集/预测能独立运行。 | [作业与本地 Codex](../src/polymarket861/jobs.py) · [作业测试](../tests/test_jobs.py) |
| 14 | **持久化、恢复和六个入口。** 在活跃 SQLite 及内容寻址档案上创建备份、验证哈希、恢复到空目录，比较账本和封存摘要。打开离线看板与回环 HTTP，分别检查时点/模式、关键源滞后、真实联查、HTML 转义、零样本及空账本。 | [存储](../src/polymarket861/store.py) · [备份和恢复](../src/polymarket861/operations.py) · [看板测试](../tests/test_dashboard.py) |

## 五、现场操作和仍需比赛验证的事项

**工作区实测。** 开发现场曾为 2026-11-27 诺丁汉森林对切尔西封存开球前的 `technical/early` 快照。当时球队历史源缺主客各两场到期赛果，`source_health.critical_stale=true`。这条记录用于验证滞后告警、原始时间依据和降级展示。它与同机 10 场历史回放各有独立模式。看板、数据库、原始行情和本机运行轨迹留在各自的私有数据目录。

**持续观察。** 在新运行目录先登记赛程及真实收到的历史证据。`data observe` 返回本机已见、带指纹的输入目录 `fixture_root` 和相应的证据文件 `evidence_path`。跨多个赛季的首次读取只证明本机此次读取后见到这些事实。复制一份旧赛果文件无法证明数年前就收到了其中的数据。

```bash
pm861 --data-dir "$state" data observe "$input" --register-season 2026-27 > "$review_work/observation.json"
observed_input=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["fixture_root"])' "$review_work/observation.json")
evidence=$(python -c 'import json,sys; print(json.load(open(sys.argv[1]))["evidence_path"])' "$review_work/observation.json")
pm861 --data-dir "$state" run due "$observed_input" --evidence "$evidence" --workers 2
pm861 --data-dir "$state" run status
pm861 --data-dir "$state" source list
pm861 --data-dir "$state" feeds providers
pm861 --data-dir "$state" experiment status
pm861 --data-dir "$state" trial readiness
pm861 --data-dir "$state" dashboard export --output "$review_work/dashboard.html"
pm861 --data-dir "$state" backup create "$review_work/backup"
pm861 --data-dir "$state" backup verify "$review_work/backup"
pm861 --data-dir "$state" backup restore "$review_work/backup" "$review_work/restored"
```

要验证真实的赛前窗口，用可持续运行的本机进程执行 `run due INPUT --evidence EVIDENCE --workers 2 --repeat 60`。将 INPUT、EVIDENCE 替换为 `data observe` 返回的配对路径。当前未到窗口时可返回零个到期任务。在新一轮更新后使用对应的新输入。补充免费赛果可通过 `result-feed sync 2026 --round 11` 获取 OpenLigaDB 的当季指定轮，再用 `result-feed compare 2026` 检查与现有赛果的匹配和冲突。该源的研究记录保留本机首次实际接收时间。行情由 `market watch FIXTURE --cycles N` 维护，实际入场和结算分别通过 `ledger-freeze`、`ledger-evaluate`、`ledger-settle` 进入账本。使用各命令的 `--help` 和 `ledger-example` 查看有时间戳的输入形状。

候选通过 `forecast recipe` 绑定模型配方，`experiment register` 登记类型、基线、窗口及假设，`experiment evaluate KEY` 从独立证据决定维持影子或晋级。`experiment review-due SEASON ROUND` 检查到期窗口。`experiment rollback KEY --reason TEXT` 留下回滚原因。该入口接受 `model` 和 `source`。选单策略另由 `strategy register` 绑定相同模型、正式机会源、历史清单、每场预算和未来十二轮。用 `strategy history` 复算历史 A 档，`strategy freeze KEY FIXTURE` 在赛前封存双臂，`strategy execute KEY` 执行并核对结算，`strategy score KEY` 从真实簿、成交及收盘观察计算配对指标。随后可用 `strategy promote KEY` 验收、`strategy apply FIXTURE` 应用活跃的纸面策略、`strategy rollback KEY --reason TEXT` 回退。当前真实历史 A 档为零，没有策略取得晋级证据。[策略代码](../src/polymarket861/strategy.py)

`jobs enqueue`、`jobs run --workers 2` 和 `jobs status` 用于并行工作。已在本机配置的 Codex 可以经 `--allow-codex` 执行已登记的 Codex 任务。

现场仍需核验同赛季两个连续真实完整轮次。每轮应有十场完赛，十场各自都要在 T−24h 和 T−60 两个窗口按时封存技术预测。同时持续记录免费源的双队名单及时率、新闻与天气的边际增益、同窗双订单簿及同代币收盘价覆盖、未来比赛的独立模型晋级，以及恢复后的连续运营。按 `trial readiness` 输出逐项记录证据和缺口。严格历史可用时间的实测覆盖可以为 0，历史 A=0 时可执行收益率继续留空。审核时分别保留这两个分母。全部正式准入条件满足后，通过 `trial activate FUTURE_UTC` 从新未来窗口启用前向模拟。建议审核结论按“已由真实运行验证、仅有测试样例、需要现场样本、发现可复现缺陷”注明证据类型，并给出对应提交、命令、输入来源和改动优先级。

**其它真实检查。** OpenLigaDB 当季提供 100 场有终场比分的比赛。与主源重叠 50 场，49 场一致，1 场冲突进入隔离队列。GDELT DOC 当天返回 503，系统从 GKG 的 1,525 条元数据中提取出两条阿斯顿维拉官网链接。Open-Meteo 起报预报仅在独立非商业研究环境实测。当前没有已验证的人员或天气增益。首次技术预测保留当时实际封存的模型和输入标识，后续版本可核对原记录；五季统计使用交付代码重新计算。本机 Codex 进程可以启动，托管宿主拒绝了沙箱创建所需的系统调用，未取得一次真实模型作业的完成回执。完整运行目录的备份、校验、跨目录恢复均已实测，299 个文件通过校验，恢复快照时的 197 条归档观测、11 条预测封存逐项一致。[采样依据及状态](../reports/first-run.json)

**修订后的证据入口。** 可用 `forecast round-history 赛季 轮次 --mode replay --edition 1` 复看补赛或更正前的条目，含比分版本、当时的漏发范围和封存摘要。`run due` 完成赛前调度后会运行 `maintenance tick`，将已结束的轮报、隔离错误、免费源异常及本周缺口登记为去重的本地 Codex 研究任务。同机多次执行同一输入只产生一次任务；更正后另有新任务。任务由 `jobs run --allow-codex` 在隔离目录执行。正式启用检查还核实同赛季每轮 20 支真实球队、每个比赛身份与预测所用的有时间戳赛季原件，历史概率报告须对应相同版本的完整英超赛季与评分代码。只有找到了真实挂牌且有合约行情观测的已结束赛事，才计作已做市场研究。

来源晋级按字段及生效时间处理。可在 `source list` 的 `field_status` 中核对，例如仅首发名单字段已生效时，新闻字段仍可处于试采。晋级前取得的事实保留原采集用途，回滚后新决策重新使用原字段来源。

### 单笔持有至兑付的复核

从原始 `ledger-freeze` 输入中保留 `intent.contract`，其中包含真实场次、代币、核过的规则和异常分支。对已有持仓的纸面账户，把下列变量替换为该账户、代币和实际比赛的独立结果。正常完赛需核对 90 分钟及补时终场比分。例中的 2∶0 只演示字段格式。

```sh
pm861 --data-dir "$state" ledger-capture-resolution --token "$token" > "$review_work/resolution.json"
python - "$review_work/resolution.json" "$review_work/settlement.json" <<'PY'
import json, sys
from pathlib import Path
receipt = json.loads(Path(sys.argv[1]).read_bytes())
if receipt["status"] != "resolved":
    raise SystemExit("交易所尚未确认终局")
case = {"scenario": "normal", "score": [2, 0]}
Path(sys.argv[2]).write_text(json.dumps(dict(receipt, **case)), encoding="utf-8")
PY
pm861 --data-dir "$state" ledger-settle --account "$account" --input "$review_work/settlement.json"
pm861 --data-dir "$state" ledger-report --account "$account"
```

异常结算时，按该笔持仓锁定的条款及赛事方公告选择 `intent.contract.special` 中确有的分支。例如公告确认无比赛结果，且条款归入 `no_result` 时，将示例中的 `case` 换为 `{"scenario": "no_result"}`。若条款涉及腰斩时玩法已提前决定，还要提供经核对的中止比分。复核原件的 `condition_id`、`token_id`、时间、原始 SHA 和最终每份兑付；金额由程序读取原始交易所结果。到账本报告核对已付现金、净份数、盈亏及重复调用的余额。

### 从预测走到市场回放

正式模拟准入后，先以 `ledger-account --account main --mode formal` 建纸面主账户，启动该场的 `market watch` 归档盘口与 `run due` 封存预测。用 `opportunity run 赛事slug --fixture-id 联赛比赛ID --account main --repeat 1` 在真实时钟下处理机会。配对核对双方球队、主客方向与该版开球，自动查找唯一可用的正式模型输出，按已归档原件解释每个代币及其买方成本。每次跳过都有原因。用 `market replay-inputs` 或 `trial study-archive` 在完赛后重建；外部填写的归档审核人、价格、深度和哈希不能代替交易所原件。

正式 CLV 需要有独立证据的实际开球事件。首发名单及时率、购买竞争、费率版本、特殊兑付和实际开球证明均依赖免费来源的真实覆盖。当前实测六个赛事归档中，79 个解析到规则的合约里有 7 个无费合约具备可构造的条款；72 个含费合约缺少对应时期收费资产、精度或舍入证据。另外 3 份旧文案待核。在缺少字段或实际开球证据时，分别查看模拟入场与收盘比较的缺口。

模型晋级用的数据与时间可以独立核对。每个历史预测封存赛季原件、证据文件、赛果可用协议、训练切片及上下文摘要。更换历史输入要用新版本；同一实验的两臂必须符合预登记的共同输入协议。前向同场同窗口复用第一份可信输入和实际截止，支持并行及中断恢复。旧版缺少配对凭据的成绩仍可查阅。原始五季概率回测与正式晋级分别验收，公开的数值可逐字段重算。

费用规则登记可用 `ledger-fee-policies` 核对。当前只对可验证的零费原件直接给出费用；版本登记表中尚无经独立确认的含费政策。今后增加收费政策需随代码绑定国际站原件、发布时间和有效期、交易费公式、收费资产与舍入口径，并通过已有的正反回归。已核的政策可用 `ledger-capture-fee-policy --policy 政策ID` 读取原件；成交前的两个盘口时刻都必须满足该生效版本。

逐季目录的复算使用 `python scripts/market_rule_coverage.py 目录采集器的运行目录`。2025/26 的 2,195 个已映射合约中，1,668 个可解释规则，包括 1,131 个明确免费和 537 个含费。2026/27 的 2,736 个已映射合约中，2,451 个可解释且全部含费，285 个规则待核。当前尚无经核验的当期收费与舍入版本，因而此次 2026/27 目录样本全部跳过模拟买入。可核零费规则仍需逐笔满足赛前时间、深度与模型门槛。[逐季合约条款核对](../reports/contract-rule-coverage.json)

## 独立审查的回归场景

[第一轮审查原件](review-evidence/release-audit-first-pass.md)和[第二轮审查原件](review-evidence/release-audit-second-pass.md)保留各自冻结版本的结论。检查当前实现时，可用同一场次构造以下对照，并记录两个账户的实际结果。

| 场景 | 复核方法与预期 |
|---|---|
| 赛前封存后行情继续更新 | 在 T−60 截止前 5 秒封存，使用截止前 7 秒的合格盘口；在截止前 2 秒再放入一份盘口。原账户与其历史重放应使用封存时已见的同一份第一盘口。 |
| 已持有的合约与模型身份 | 只修改导出意图的模型版本、公平价、封存摘要或意图编号，保留真实合约及盘口。逐笔检查该机会在历史报告的分档、是否成交、是否贡献收益；另验证预登记的两臂可在同一来源意图上执行各自的预算。 |
| 历史赛果的可用时间 | 固定同一组经校验的赛季文件，额外给出无出处的赛程发布时间或与原件矛盾的比分。检查研究报告的来源标记及 `trial readiness` 的严格覆盖，随后用实际归档原件作正例。 |
| 同场不同市场证据 | 给一个代币提供正式意图与前后两簿，给对侧代币提供独立的交易或曲线证据。加入正式账户前后核对逐币身份和整场分母，已核的交易或曲线应继续计数。 |
| 历史入场凭据被纠正 | 先查看已结算历史报告，再导入与其中一场矛盾且来源有效的实际开球凭据。核对更正后的有效成交、已结算成本、净盈亏、收盘覆盖和收益率。若同批还有另一笔有效成交，只用保留的记录计算本次统计；原账户的资金及历史结算仍可复查。更正后直接运行 `trial readiness`，再重新生成同一范围的行情研究，分别核对两次准入及其收益。 |

比较选单策略时，先选定比赛，再列出该场指定主账户同一截止时刻所有满足 A 档的原始意图。若两枚代币合格但只有一枚有收盘记录，完整池的收盘覆盖应为 1/2。将另一枚从参考清单中删去后，重新尝试 `strategy register`、`strategy history` 和 `strategy score`。再取出并恢复其中一份首簿原件，检查配对场数和收益是否随证据变化。
