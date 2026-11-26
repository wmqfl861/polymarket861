# 五联赛模块边界与回归入口

Claude 对 0.4.2 的扫描在 83 个模块中找到 54 个含赛事标识、来源地址、时区或赛季数量的模块。此表覆盖这 54 个模块，包含初稿漏列的 23 个。一个源文件内可以同时存在已登记的英超协议和共用组件。新增赛事从显式联赛入口进入；原有试验的分母、资金、历史记录和批准条件仍可按其登记版本重放。[逐模块原始命中行](../review-evidence/code-review-0.4.2-2026-11-26/outputs/scan-league-assumptions.json)

| 模块 | 赛事边界及验证 |
|---|---|
| `__init__.py`、`football.py`、`football_model.py`、`datasets.py`、`football_candidates.py` | 发行版与训练来源分别记录。五国输入按联赛和赛季成员检查，复用比分数学，分别拟合。旧英超省略联赛参数走原协议。数据刷新和旧候选晋级的范围各自登记。用英超 1,900 场基线与新五国固定档案对照。 |
| `historical_inputs.py`、`input_manifest.py`、`archive_proof.py`、`paired_proof.py`、`kickoff_proof.py` | 封存、原件散列、资格证明和时序定义由调用协议指定。英超的数据库输入证明、首哨来源和新五国历史假设独立。变更受检内容、跨赛事 ID 和重复使用的证据须被识别。 |
| `experiment_statistics.py`、`model_study.py`、`forward_window.py`、`qualification.py`、`research_workspace.py`、`trial_evidence.py` | 保留旧 38 轮及批准条款。复用聚类统计内核，五国初始研究在新的研究入口计算 34/38 轮、本地对照与同比赛周合并区间。正式前向另登记，改变观察次数、考试年份和赛季完整性都要重做资格检查。 |
| `market_provider.py`、`market_archive.py`、`ledger_terms.py`、`ledger.py`、`shadow.py`、`strategy.py`、`opportunity.py` | 旧英超目录、账本、策略和真实可执行盘口证据按账户和条款归属隔离。新增国际站只读研究目录按联赛、比赛、合同和两枚代币定位。结算按入场条款及交易所最终凭据检查。用错方向、重复收件、结果未确定、分数兑付与跨账户重试覆盖。 |
| `news.py`、`news_sources.py`、`news_scope.py`、`news_wire_identity.py`、`news_retention.py`、`news_roster.py`、`news_minutes.py`、`news_importance.py` | 名单与信源的球队归属、生效赛季、首发数容差、赛事纪律及原始日期各自核对。触发英超空回复的覆盖规则与四国许可清册分开。留存、名单替换及权重回退按来源、球员和时刻复查。 |
| `news_ai_benchmark.py`、`news_local_ai.py`、`news_events.py`、`news_evaluation.py`、`news_accumulation.py`、`news_model.py`、`news_market.py`、`news_study_plan.py` | 英文盲标集、冻结模型、新闻对照臂与赛前数据仓有各自配方。五语独立盲评待授权样本。收盘分档、功效重算和赛后纠正不追溯改写原预测，已登记的检验家族须单独留证。 |
| `result_feed.py`、`independent_results.py`、`result_operations.py`、`result_pairings.py`、`result_admission.py`、`result_training_review.py`、`schedule_consensus.py` | 既有线上赛果通道的赛事映射、两源独立性与冲突裁决属于英超生产流程。新增联赛先核对固定来源和特殊场，再对新增线上来源做按字段资格验收。要以相同队名、改期、行政和漏比分测试错配与信息泄漏。 |
| `store.py`、`pipeline.py`、`season.py`、`season_readiness.py`、`season_health.py`、`runner.py`、`maintenance.py`、`jobs_research.py`、`round_history.py` | 英超实时季服务、已冻结轮次及本地 Codex 工单按旧协议继续运行。五国历史轮报、市场值守先独立使用。下一阶段给实时赛程、重算、更正、看板和工单补联赛键、逐国分母与故障恢复。 |

本批新增的 `news_endpoint_originals.py` 属于已登记的英超新闻试验。它用专属来源名将有留存许可的响应绑定到英超比赛、赛季和收件时刻，按原配置离线解析，并从名单修订和身份原件重建当时可识别的球员。其比赛 ID 边界和归档来源名都进入本次源码检查。原有 `news_sources.poll` 默认使用英超注册来源，`news_study_plan` 给会改变封存事实的采集适配、实际投递和时点单独签名。只读留存是否成功影响该前瞻研究的资格。其他联赛各自登记语言、来源和试验窗口。[新闻试验的覆盖和缺件](NEWS-ENDPOINT-STUDY.md)

任一 PR 可运行 `python scripts/check_league_boundaries.py`。检查包含预审登记过的来源、路由、时区与赛制行；移动到另一模块或增加一处同样的命中也会显示。需要新增合法的适配器时，先查具体命中、补归属及回归，再更新 `scripts/league-boundaries.json`。该清单按源行记录，全文匹配只是审查线索，新的语义仍须代码审查。[检查程序](../../scripts/check_league_boundaries.py)

## 概率配方和五联赛回测的版本

英超概率配方同时登记产生概率的定义、写入或读取赛前输入的规则，以及选择当时生效模型的接口。Claude 在 0.5.0a1 的 B1 配方发现 45 个配方外的跨文件目标，其中 39 个缺说明。0.5.0a2 把其中 17 个输入定义纳入配方，包括赛果配对签名、来源提交、档案解码和时间校验、模型生效时钟。调用层转发给模型选择函数的导入也参与签名。其余 28 个原目标分别列出允许的调用者、适用模型和排除理由。新增调用者需要重新核对。[逐目标排除表](../../scripts/recipe-reference-policy.json)

本版另加的 Gamma 父事件缓存校验只在五联赛纸面入场时启用，核对该份原件的 HTTP Age、Date 及请求耗时。英超开球时间共识沿用原件的收件时间与 `schedule_consensus.MAX_AGE`。CI 将该排除限定到 `archive_proof._event_with_operation`，回归使用同一份归档事件分别验证两种调用，同时检查英超自己的收件时限仍然生效。[开球时间回归](../../tests/test_schedule_consensus.py) · [逐份盘口回执回归](../../tests/test_budget_scheduled_entry.py)

`python scripts/check_recipe_references.py` 对 B0、B1、B2、B3 的主概率路径分别运行。从实际入签的语法树出发，检查声明的跨文件导入、转发和目标定义。新增且未经核对的调用或已失效的排除会令 CI 失败。更改会影响输入接纳或概率的代码时，在来源文件登记入口，并补一个使配方摘要改变的测试。静态检查只能分析源码引用，数据库里的封存、外部响应及跨进程写入还需按其数据通路核对。B3 附带的新闻试验另有配方。[检查程序](../../scripts/check_recipe_references.py) · [变异测试](../../tests/test_recipe_references.py)

`pm861 leagues baseline-study 历史目录 --output 五联赛回测.json` 会在 `research_recipe` 中写入研究版本。核对内容含实际安装的回测、球队登记与评分源码，成员档案的文件摘要，各联赛逐季有效的球队键，40 份核验过的历史文件标识，以及 NumPy、SciPy 版本。在独立副本中删去西甲 `RC Celta` 的别名并重跑同一批原件后，西甲开发集 B1 均分从 76.079872 变为 76.072782，五联赛研究版本改变，其他四国逐项一致，既有英超 B1 配方也保持一致。历史报告按其原版本读取。[研究配方实现](../../src/polymarket861/league_study.py) · [别名和成员档案的回归](../../tests/test_league_study.py)

联赛边界扫描规则与伤停吸收实验的固定赛程均随 `scripts/` 分发。可在只含 `src/`、有关脚本和 `league-boundaries.json` 的目录运行 `python scripts/check_league_boundaries.py`。安装项目依赖后运行 `PYTHONPATH=src python scripts/reproduce_news_absorption.py` 可复算随机种子 861、20 队、8 个伤停期限的实验。[实验脚本](../../scripts/reproduce_news_absorption.py)

英超新闻端点的 v2 声明、原件核对与功效分别属于 `news_endpoint_v2.py`、`news_endpoint_reference.py` 和 `news_endpoint_power.py`。它们使用英超赛季的原生预测，不把四国盘口样本作为该试验的预测。来源轮报 `news_field_coverage.py` 用封存时的 Touchline 解码器版本区分当年是否已记录字段。盘口归档只有在提供赛事归属时才投影相应球队，旧英超不带赛事参数的路径有单独的原件回归；对应的入口在 `market_archive.py` 和 `market_provider.py`。[注册与重放](NEWS-ENDPOINT-V2.md) · [球队及合同原件回归](../../tests/test_budget_domestic_archive.py)
