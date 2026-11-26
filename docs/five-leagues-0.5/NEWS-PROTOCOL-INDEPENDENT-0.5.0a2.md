# 0.5.0a2 独立复审判决 · T1-2 与 T2-2

**通过。** 2026-11-26 在本地冻结候选提交 `a068b65c824476eb330523992d7858ff36f6e451` 复验。源码树 `0a0bd93fa0002418ae0d2a004d1b38c4dd46419c`，测试树 `7eab2d876a7a25b45a25f7dd0e442909bfba863e`。`five05-validation/venv/bin/python`（3.12.14）运行 26 项相关回归全部通过。另以独立输入及保存的旧版本实库复核下列边界。[专项日志](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-release-tests.log) · [独立断言结果](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-verified.json)

| 抽查 | 复验结果 |
|---|---|
| 双模式、版本及赛前决策窗 | 同场先入库的正式封存比后入库的技术封存时间戳晚 10 秒。决策窗到点前合格数为 0，到点选中正式封存，技术封存记重复。另一个原生用例在两种模式和两种提取配置产生两个可合并的概率版本，v1 保留原队列。真实 SQLite 时钟接受两种模式的及时写入，拒绝事后补写。[双模式收件](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-end-release.json) · [SQLite 时钟](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-legacy-release.json) |
| 固定收官 | 终场第二份凭据到达前后，已核实比赛数由 0 变 1。截止前一微秒仍封存结果，恰到 `2028-07-01 00:00:00 UTC` 揭晓。恰在截止收到比分争议，随后比分改判、收到提前首哨和补登记较早发布的改期。四次重报均与首次收官报告 SHA-256 相同。[逐次报告](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-end-release.json) |
| 赛程逆序 | 初审发现按数据库追加顺序把早已失效的发行当作改期，两窗各失去主胜及大 2.5。修复后，同一份迟到旧日历在截止前入库，四项配对均保留。另验真实生效的改期随后恢复，两窗均剔除。[修复前](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-chronology-before.json) · [候选结果](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-chronology-release.json) |
| 0.5.0a1 实库 | 从 `49313f4` 单独生成原生新闻及预测库，旧版实际 SQL 触发器绑定 1 场、留存两队 2 份原件。升级后旧报告逐字一致；登记 v2 时继承的绑定及原件均为 0。[旧库凭据](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-legacy-native.json) · [新旧并存](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-legacy-release.json) |
| 停赛、伤病分母 | 初审发现一个已知类别会使另一未知类别误报 0。九格复验覆盖无归档、部分归档及完整球队收件，分别搭配无事件、仅伤病、仅停赛。无完整球队收件时，唯一已知类别计 1，另一类为 `null`；完整空名单两类均为 0。各格球队队次之和均为 20。[修复前](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-fields-before.json) · [九格复验](../../reports/five-leagues-0.5.0a2/independent-news/independent-protocol-fields-release.json) |

**实际覆盖仍待前瞻积累。** 功效输入的第 12 轮 10 份早封存均含 Touchline 伤停，90 条球员权重全部来自该源。当前登记只自动留存获准的开放来源原始响应。Touchline 的默认伤停及传闻不属于这项留存范围；同场其他来源的原件也无法替代已用的 Touchline 事实。650 场、单一分层、噪声 5 个百分点、真实斜率 0.5 时，登记功效为主胜 8.45%、大 2.5 为 3.15%。这些数值依赖假设的入组场数及新闻调整分布。[来源核对](../../reports/five-leagues-0.5.0a2/news-source-retention-coverage.json) · [冻结功效](../../reports/five-leagues-0.5.0a2/news-endpoint-power-v2.json)

独立复核使用临时库、受控来源原件及历史场景。双模式逆序和 2028 截止样例使用受控时钟，旧库迁移与新写入截止校验使用宿主机真实 SQLite 时钟。复验范围为研究资格与报告口径。
