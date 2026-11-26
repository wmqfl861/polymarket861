# 五联赛 0.5.0a1 首批开发交接

本页记录 0.5.0a1 的首次发布。安装当前 0.5.0a2 时，请结合[最新交接](CLAUDE-REVIEW-RESPONSE-0.5.0a2.md)和[四国采集服务说明](WATCH-SERVICE.md)选用相应的实收、模拟入场与新闻协议。

2026-11-26。适用于全新安装的研究实例。Claude 本轮的两份原审、冻结后的执行计划及完整模块清单都在这次提交历史里。[计划审核](../polymarket861-five-leagues-0.5-plan-review.md) · [英超审核](../polymarket861-EPL-code-review-0.4.2.md) · [实施计划](IMPLEMENTATION-PLAN.md) · [模块覆盖](MODULE-BOUNDARIES.md)

## 从安装到第一份逐轮报告

以下命令在仓库根目录运行。使用 CPython 3.12 和约束文件可以复现仓库的研究环境。将每个输出目录换成自己的持久目录。五国代码为英超 `epl`、西甲 `lal`、德甲 `bun`、意甲 `sea`、法甲 `fl1`。

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -c constraints/research.txt -e .
pm861 --version
pm861 leagues list --season 2025-26
for league in epl lal bun sea fl1; do
  pm861 --data-dir .pm861-05 football fetch .pm861-05/openfootball --league "$league" --from-season 2018-19 --to-season 2025-26
done
pm861 --data-dir .pm861-05 leagues inventory .pm861-05/openfootball > .pm861-05/inventory.json
pm861 --data-dir .pm861-05 leagues baseline-study .pm861-05/openfootball --workers 4 --output .pm861-05/five-league-study.json
pm861 --data-dir .pm861-05 football backtest .pm861-05/openfootball --league bun --from-season 2025-26 --to-season 2025-26 --archive-lag-hours 24 --horizons 60 --bootstrap 0 --details .pm861-05/bun.ndjson
pm861 --data-dir .pm861-05 football round .pm861-05/openfootball --league bun --season 2025-26 --round 1 --cutoff-minutes 60 --archive-lag-hours 24 --predictions .pm861-05/bun.ndjson
```

`football predict` 接受选定联赛、已登记的球队、带时区的开球时刻和 `--lines`。胜平负、全场大小、主队大小、客队大小来自同一个比分分布。在 `football backtest` 去掉 `--archive-lag-hours` 可检查当时实际可得的历史时点证据。只有冻结成绩单时附上的盘线才能作为相应的市场回放对象。市场的主客、合约方向和盘口深度须另行绑定。原英超服务的日常部署步骤见[比赛日手册](../MATCHDAY.md)。

## 五国实际成绩

T−60、按当国球队拟合的 B1 对该国 B0。评分单位为 0–100 分。每国开发期为 2021/22–2024/25，2025/26 为最后留出季。保留旧英超原入口，独立复现其五季 1,900 场，B1 **75.447813**、B0 **73.559172**。在显式五国入口，额外排除无法重建首次赛前窗的伯恩茅斯–卢顿全场重赛。

| 联赛 | 四季开发期配对数 | 开发期综合分差 | 2025/26 可评／应有 | 留出季 B1／B0 | 留出季分差及校正区间 |
|---|---:|---:|---:|---:|---|
| 英超 | 1,519 | +2.137 | 380/380 | 74.526／73.647 | +0.879 [−0.238, +1.996] |
| 西甲 | 1,509 | +1.758 | 380/380 | 75.869／74.270 | +1.600 [+0.411, +2.788] |
| 德甲 | 1,222 | +1.760 | 306/306 | 75.622／73.844 | +1.778 [+0.252, +3.305] |
| 意甲 | 1,508 | +2.064 | 380/380 | 75.434／73.945 | +1.490 [+0.645, +2.334] |
| 法甲 | 1,368 | +1.518 | 305/306 | 74.627／73.893 | +0.734 [−0.508, +1.976] |

五国 2025/26 共 1,751/1,752 场，按 37 个 UTC 比赛周聚类后，综合分差 **+1.300**，同时区间 **[+0.780, +1.819]**。全场大 2.5 单项分差约 **+0.002**，区间 **[−0.731, +0.736]**。这项结果仍不足以说明全场大小预测有改善。所有数字按普通场次开球后 24 小时可用赛果的假设计算。严格档需要真实赛程及赛果收件证明。2025/26 英超及各国部分汇总此前已公开。报告给出每个真实轮次的配对数、独立四项、缺失比分、特殊场和回退，研究通过后未改变正式预测或账户指针。[完整固定输入与输出](../../reports/five-leagues-0.5/baseline-study.json)

Claude 的原标杆脚本在原源码和同一份数据上复跑，四国训练结果、期望覆盖、合并数据及嵌套选择与随审报告一致。新协议与原四季开发基准的场次数差异为西甲 −1、德国 0、意甲 −2、法国 −3，均可按例外登记查到续赛或重赛。法甲留出缺南特–图卢兹弃赛。40 份八季档案的 14,386 个常规赛席位已逐一核对。原始记录、可训练的完场、具有可重建赛前窗的目标、真实可成交合约各自使用独立分母。[历史清册](../../reports/five-leagues-0.5/history-inventory.json) · [Claude 标杆](../review-evidence/code-review-0.4.2-2026-11-26/outputs/probe-league-baselines.json)

## 用四国盘口做试采

以下日期对应本批实测。采其他轮次时替换为比赛开球的 UTC 窗口。目录和值守按计划开球日选择比赛。周报的 HTTP 次数及原件按实际收件时刻归档，代币数、新鲜度和深度分布按该批采集完成时刻计入。若于 23:59:59 收到 14 枚代币、次日 00:00:00.5 才完成，前一天计一次 HTTP，后一天计 14 枚代币。整周首尾对账要把跨界批次单列。默认查询四个新增联赛，`--league` 可重复指定，也可明确选择英超。采集使用独立子目录 `five-league-market`。改期会保留已经到期的旧窗口，同一场的两个收件还要逐合约匹配方向、代币、条款和延迟才能算配对。原始 Gamma 目录、附盘、CLOB 双边簿、可取得的费用与规则按收到时间和摘要可追溯。国际站公布了只读技术接口；长期自动获取、留存和再分发应按部署目的核对适用条款与授权。[公开数据说明](https://docs.polymarket.com/market-data/overview) · [使用条款](https://polymarket.com/tos)

```sh
pm861 --data-dir .pm861-05 market league-archive --start 2026-11-27T00:00:00Z --end 2026-12-01T00:00:00Z --mode all
pm861 --data-dir .pm861-05 market league-watch --start 2026-11-27T00:00:00Z --end 2026-12-01T00:00:00Z --cycles 1
pm861 --data-dir .pm861-05 market league-report --start 2026-11-26T00:00:00Z --end 2026-11-27T00:00:00Z
```

值守以 `--cycles 0` 在前台持续运行，定时采集需要部署实例保持在线。本批在 11 月 26 日做了真实采样和同槽重启演练。西甲 10、德甲 9、意甲 10、法甲 9 场，找到了 38 场的主盘和附盘。全量采样含 912 份目标合约、1,824 枚代币，1,368 枚同时有买卖两边，1,596 枚的可见卖盘税费前可容纳 50 模拟美元，初次采样尚未审定条款。随后以当地比赛实际原件冻结了新增四国的 16 种模板，重新读取同一份存档后 912/912 能确认对应模板。合约是否可进入纸面买入仍需双时点、费用及深度等证据。周末比赛、正式双时点执行、首哨收盘与实际占款仍需随后收件。周报可显示没有采到的窗口以及价格、深度与规则的缺件。[真实采样汇总](../../reports/five-leagues-0.5/market-observation.json) · [规则模板原件抽样](../../reports/five-leagues-0.5/rule-template-observation.json)

法甲南特–图卢兹全场 2.5，市场 2162604，Gamma、CLOB 与两枚代币的 Data API 终态共同表明**大球兑付 0、小球兑付 1**。规则中同时存在条件性平分条款。系统需要核实本合约实际终态才可入账。已收到互相冲突的终态会进入人工复核；已记账的旧流水保留原值并标记争议，后到的证据不会引起重复入账。0.5/0.5 分数兑付已用构造的双代币终态验证；这里未提供一笔真实平分的国内市场样本。可在既有英超赛程档上运行 `pm861 --data-dir 运行目录 close-reference --fixture 本地比赛ID`，逐档查看首哨、赛前状态或双源排期的收盘参照及原件；第三档只作研究。[兑付验据](../../reports/five-leagues-0.5/settlement-observation.json)

## 球员、新闻和审核入口

```sh
pm861 leagues clubs --league lal --season 2026-27
pm861 leagues aliases --league sea
pm861 leagues sources --league fl1
pm861 --data-dir .pm861-05 leagues health --league bun
pm861 --data-dir .pm861-05 leagues news-endpoints plan
pm861 --data-dir .pm861-05 leagues news-endpoints report
```

逐队列出本赛季 96 队的名单、伤停、传闻、纪律、首发和场外信息来源，就每项说明允许用途和待补字段。四国的来源许可、实时收件、成功提取和能否改善预测均需分别取得证据。英超的空伤停回复须先验证球队与赛季归属。未确认赛事范围的纪律消息不能直接改写本场联赛的可用名单；伤停时长优先采用有效原始日期，最新球员目录不合格时可追溯到最近一份合格的旧目录。0.4.2 的新闻功效已按自己的十场封存重算，760 场、斜率 0.5、噪声 5 个百分点时检出率为 **17.95%**。[96 队字段清册](../../reports/five-leagues-0.5/club-source-rights.json) · [功效重算](../../reports/news-market-study-0.4.2.json)

旧英超新闻试验保持原登记。新建的主胜及全场大 2.5 新闻试验支持在本地事前登记，绑定随后实际封存的预测。两个时点、两个收盘口径及赛果护栏合计 10 个同时比较，早期只报告覆盖与缺件。原来那十场的功效测算不参与这个前瞻试验。[新闻审核样例](../../reports/five-leagues-0.5/news-audit.json) · [新前瞻声明和缺件口径](NEWS-ENDPOINT-STUDY.md)

按字段检验新信源可运行 `news admission-register` 后再用 `news admission-audit` 查验。20 个测例和两完整轮只提供初步影子资格；最近 100 个里至少 91 个及时有效并须通过独立语义抽查。确认缺阵每种语言固定 84 个不同盲标簇，容许至多 3 错且 Wilson 下界须达到 0.90。当前仓库没有四国真实授权盲标集的通过结论。[登记格式、原件和判读说明](NEWS-ADMISSION.md)

## 资金重放与版本核对

`pm861 paper-risk stress` 运行 48 场、11 天延迟的合成资金压力场景；`pm861 paper-risk replay 输入.json` 可重放指定事件。五个联赛各预留 2,000 模拟美元，每意图含费上限为 50，同场未兑付成本之和不得超过 150。单个周末从空仓开始，每国 9 至 10 场不会用完 2,000。压力例实际先重放上一轮 24 场，目标周末合计占款 9,950，目标轮因现金不足跳过 24 个意图、因盘口深度不足跳过 15 个。延迟兑付后剩余 5 枚缺终态的代币，250 继续占用。本机模式从独立 Gamma/CLOB 收件重建决策盘口、延迟执行盘口、规则、方向和费用，仅按同场双方互补终局原件释放资金。真实南特–图卢兹的 Under 兑付为 1；真实成交收益仍待积累。[压力场景](../../reports/five-leagues-0.5/capital-stress.json) · [重放输入和原件核验](PAPER-RISK.md)

英超现有实例可运行 `pm861 --data-dir 运行目录 forecast release-provenance` 查看实际解释器、数值库和整包来源。旧英超参考基线和 0.4.2 分别进行了 1,900 场正式升级对照；报告未对目标部署机代签批准。[配方三解释器对照](../../reports/five-leagues-0.5/recipe-interpreters.json) · [0.4.2 逐场对照结论](../../reports/five-leagues-0.5/release-comparison-0.4.2.json) · [版本签署指南](../model-release-governance.md)

## 仍需完成的阶段

| 范围 | 当前可复查 | 下一次验收 |
|---|---|---|
| 四国实时赛程、赛果、轮报与看板 | 显式历史入口、固定数据、真实轮次、特殊比赛及单国历史报告 | 每国实时来源准入、持续固定窗封存、补赛纠正与中文看板全流程。英超 `season run` 仍是已交付的线上季服务。 |
| 球员、五语新闻与主动迭代 | 96 队权利目录、英超实时新闻修复、资格规则和历史功效测量 | 有授权的五语原文、名单与纪律实体、独立盲标、每国真实轮次的新闻驱动预测和增量评分；把跨联赛变更接入本地 Codex 维护工单。 |
| 市场与五国模拟资金 | 按联赛目录和盘口独立采集、规则和兑付深度诊断、五桶资金重放及 11 天延迟压力测试 | 在完整比赛周收满 T−24h/T−60min、延迟执行及收盘原件。审核各玩法条款，把可验证的五国合格订单和兑付逐笔接进持久化资金账户，实测每国余额、跳过与实际回报。 |
| 前向模型批准 | 四季开发和留出季本地 B1/B0，旧英超协议和已封存记录可核 | 新登记的可重复前向窗口、逐国负迁移检查、数据源升级/降级与独立签署。目标部署机的本地数据库、同意和服务心跳需由该实例自己生成。 |

审核时可按[实施计划的 17 项处置表](IMPLEMENTATION-PLAN.md)定位每条发现。独立复审记录列出了哪些反例曾让候选版本退回、怎样重做验证，以及可发布的范围。Claude 原始审核、复现脚本和英超既有试验均可从本提交的父历史追踪。[独立整体审核](INDEPENDENT-REVIEW.md) · [全量回归与安装验证](../../reports/five-leagues-0.5/integration-verification.json)
