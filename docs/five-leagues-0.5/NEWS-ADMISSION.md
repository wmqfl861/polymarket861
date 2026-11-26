# 新闻来源与确认缺阵的准入审计

0.5 研究版继续把英超 B1 作为正式足球基线。球队伤停来源的覆盖、球员所受处罚适用的赛事、语言模型精确率和赛果预测的改善各有独立证据。本文的命令复算来源字段的及时有效率，以及各语言独立盲标的确认缺阵精确率。生产启用仍由治理流程决定。[本轮实证](../../reports/five-leagues-0.5/news-audit.json) · [逐俱乐部来源权利](../../reports/five-leagues-0.5/club-source-rights.json)

## 当季空表、停赛与伤病起点

英超官方在 2026-06-19 发布的首轮十场列全 20 队，Touchline Wire 的文档将伤停端点限定为英超。内置名册在 2026-07-01 至 2027-07-01 UTC 的半开区间有效。当前收件绑定实际球队、赛事、赛季、URL、上游响应摘要、来源时钟和本机收件时钟。它能说明该源在当时未列出伤停。此前版本的空表缺少名单证明，需要从本机找到当时已收到、具名标明 EPL 的球员目录，复验原件和时钟。缺件、赛季不符和境外俱乐部均保留未知。实际操作可用 `pm861 news sources --team Arsenal --season 2026-27` 查看可订阅的默认来源。[英超当季官方名单](https://www.premierleague.com/en/news/4675508/premierleague-fixture-schedulereleased-for-season-202627) · [Touchline Wire 文档](https://touchlinewire.com/docs)

停赛在原始新闻和事实账保留。结构化消息须具名标明处罚所适用的赛事，文字消息须有明确指向该联赛的停赛表述。欧战、杯赛和未说明赛事的处罚对英超研究预测的调整为零。历史 0.4.2 的十场技术封存保留原始决策，所含六条停赛在该版证据里都缺赛事范围。新的球员出场对账只容许全队比应有首发多一人，同时核对门将、个人首发和分钟上限；少算继续按 90% 下限。回退记录给出所见与应有首发总数，新目录时点赛果尚未到齐时可以使用同赛季、有效期内且证据完整的较早一份。

具名球员的来源原件如果提供本次受伤的 `injury_started_at` 或 `injury_date`，B3 可从相应时间计算 B1 已经吸收的比赛。前者须有时区，后者采用该当地日可能结束的最晚 UTC 时刻，并以来源已发布该消息的时刻为上限。对原始受伤日期的获知仍从本机实际收件开始。未提供时保留首次观察起点。传闻、新闻更新时间和预计复出日期不能作为受伤日期。

## 冻结来源样本并复算

两个入口共用 `--data-dir`。先把该来源的赛程同步到本机，在观察窗口开始前登记计划。来源样本按已登记的官方轮次全收，导入器核对该轮所有球队、当时本机赛程版本及具体 T−24h / T−60m 截点。每个轮次最多一场涉及该队。对于手术等稀少事件，需要另外定义适用分母，常规场次及时率不能替代稀有事件检验。

```bash
pm861 --data-dir ./data news admission-register --plan ./source-plan.json
pm861 --data-dir ./data news admission-audit --registration "$PLAN_SHA" --reviews ./reviews.json --evidence-dir ./evidence
```

登记回传的 `registration` 为上述 `PLAN_SHA`。计划的 `kind` 取 `source_field`。必填键为 `league`、`season`、`source`、`field`、`independent_reviewer`、`opens_at`、`horizon_minutes`（60 或 1440）和 `samples`。每个样本含 `id`、`round` 和 `deadline`。比赛 ID 使用如 `epl/2026-27/arsenal/coventry-city` 的本地身份，所有时刻带 UTC 偏移。未达到截止时间的样本不提前得分。已到期且缺判读、迟到或判无效的样本均保留在分母。

审计输入的 `registration` 必须匹配本机登记，`decisions` 列出 `{"id":"比赛 ID","evidence_sha256":"判读原件摘要"}`。每份来源判读原件记录 `kind=source_field`、比赛 `id`、`registration`、`league`、`season`、`source`、`field`、登记的 `reviewer`、`reviewed_at`、布尔值 `valid` 和 `receipt_sha256`。未收到时，最后一项可留空并计入缺件。具名收件原件须含 `kind=source_receipt`、同一比赛与来源字段、实际 `received_at` 和原始响应 `response_sha256`。程序打开实际原始响应字节，重新算 SHA-256 并核对各层记录和时钟。外部证据目录用十六进制 SHA-256 作文件名，可保留 .json、.xml 等后缀。省略目录时查本机内容归档。

两整轮、至少 20 个已到期比赛且 ≥95% 及时有效可进临时影子观察。例如德甲两轮只有 18 场。达到 100 个比赛后，每次取最近 100 场，至少 91 场及时有效，且需有通过的独立语义抽验。用 `reviews.json` 的 `semantic_check` 指向其原件 SHA-256。原件的 `kind=source_field_semantics`，含同一登记和来源字段、审查人 `reviewer`、审查时间 `reviewed_at`、当窗已判读的比赛 ID 列表 `cases` 和布尔值 `passed`。滚动失守时回到影子状态。

## 固定每种语言的 84 个确认缺阵

相同登记入口支持 `kind=confirmed_absence`。计划声明 `league`、`season`、`language`（en、es、de、it、fr）、`opens_at`、`model_sha256`、全部 `training_clusters`、依次为两名独立标注员和一名裁定者的 `reviewers`，以及 84 个不同事件簇组成的 `samples`。每簇提供 `id` 和 `confirmed_prediction_sha256`。该预测原件记录 `kind=system_confirmed_absence`、相同事件簇与模型语言范围，以及早于登记或同时的 `predicted_at`。登记时用 `--evidence-dir` 提供这些原件，程序核对其字节摘要。登记成功时，本机在同一事务内预留全部事件簇。相同语言、赛季和模型只有一个登记，换评审人、改开窗时间或并发提交都不能另选结果。事件簇及预测原件也不可在本机另一项已登记试验中重复使用。旧试验如已有交叠，保留原始记录，两项均须更换独立留出集才能给出新的合格结论。跨实例合并试验前，审核者应先对齐各实例的登记和已见样本。

每份判读原件含 `kind=confirmed_absence`、同一登记、语言、模型、联赛、赛季、事件簇 `id` 和 `reviewed_at`。`labels` 的两个键是登记时两位标注员的身份，取值为布尔值，表示该系统确认缺阵是否正确。意见不一致时原件还需 `adjudicator` 与布尔值 `adjudicated`。首次提交的簇级判读在本机封存，可重复复算；更换已封存原件会报错。收齐前只报进度。固定的 84 个全部审完，至多错 3 个且 Wilson 双侧 95% 区间下界 ≥0.90，才通过本项精确率检验。

本轮提交的是可复核的操作流程和合成边界回归。五种语言的真实独立盲标集、逐语实体/事件 F1、召回和授权覆盖仍须建设。审计记录原件字节和统计结果；许可、人工判读的独立性、原始响应的发布者身份和前向预测增益仍须独立核验。准入审计始终返回 `production_enabled=false`。[准入边界回归](../../tests/test_news_admission.py) · [来源、历史和停赛回归](../../tests/test_news_competition_scope.py)
