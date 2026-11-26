# 英超新闻来源与 v2 登记决策

**0.5.0a3 可以部署。英超新闻采集可以启动，v2 正式研究建议暂缓登记。** 2026 年 11 月 26 日在隔离数据目录实采英超第 12 轮全部 20 队。新闻服务读到了具体球员的消息，也重开了维基百科的原始响应。十场赛前快照中识别出的 100 条消息全部来自 Touchline Wire，该来源目前只有规范化事实可以进入系统，研究所需的完整原始响应缺少授权依据。这是该次实采的结果，部署实例的覆盖以本机为准。[本次逐队收件、逐场分母与来源链接](../../reports/five-leagues-0.5.0a3/news-source-readiness.json)

## 实际收到了什么

| 默认订阅 | 首次成功取得原生收件的球队 | v2 重开并重跑解析 | 十场普通 `news facts` 快照中的消息 |
|---|---:|---:|---|
| `touchline-availability` | 20/20 | 0/20 | 83 条具名消息，包括 78 条伤病、5 条停赛，均标记为 `reported` |
| `touchline-rumours` | 20/20 | 0/20 | 17 条转会传闻，均标记为 `rumour` |
| `wikipedia-season-news` | 20/20 | 20/20 | 0 条。首次收到俱乐部赛季页时只建立修订基线 |

100 条消息涉及 97 个球员标识。伤停源有 14 队完整、6 队为 `partial`，其中 6 队有未匹配的球员，2 队还包含适配器未接受的条目。十场快照于 15:43 UTC 读取，表中统计当时可见的事实，并不表示这些事实都触发了概率调整。另一次早期技术封存的统计才是实际加权的口径，十场共 90 条，全来自 Touchline。[本次快照](../../reports/five-leagues-0.5.0a3/news-source-readiness.json) · [早期封存复核](../../reports/five-leagues-0.5.0a2/news-source-retention-coverage.json)

20 份维基原件均有永久修订号和 CC BY-SA 4.0 许可。用新旧两个版本分别解析同一批原件，只有阿森纳的 1 段、曼城的 3 段正文触发了伤停等关键词；这 4 段的日期均未满足相对各自修订时间的七天规则。页面覆盖因此可以达到 20/20，而本次新增合格消息为零。[逐队修订及响应指纹](../../reports/five-leagues-0.5.0a3/news-source-readiness.json)

**双队实测。** 在发布方允许下次请求后，系统再次采集诺丁汉森林和切尔西。两队各得到一份可重开的维基响应，均为零条新消息。原生研究核验器在当时的比赛和排期下复核了两侧来源覆盖及两队名单。切尔西的里斯·詹姆斯伤病消息也匹配到了名单中的 `enwiki:57778547`，保留来源报告状态。本场十条事实的十份依据全部返回 `missing private retained response`，完整新闻快照未通过。检查时隔离库的已绑定固定窗为 0，当场赔率或赛果尚未进入这项研究。[球队名单修订](https://en.wikipedia.org/w/index.php?oldid=1373200900) · [两侧收件和核验结果](../../reports/five-leagues-0.5.0a3/news-source-readiness.json)

## 许可、伤停字段与备选来源

Touchline 的开发者文档允许个人、爱好者、学术及开源项目免费查询，要求在界面、网站或 README 明显署名。其条款允许研究者和 AI 查询，要求遵守缓存、限流及 robots，并说明部分传闻标题与链接来自第三方。OpenAPI 的 `info.license` 写作 `MIT with Attribution`，指向同一开发文档；OpenAPI 规范将该字段定义为暴露接口的许可信息。公开文件及响应中的许可声明，尚不能确认两条通道的全部返回内容均可逐次长期私下归档。保留两通道的来源标识、署名、指纹和规范化事实。只有覆盖具体接口、原始响应及存储用途的许可落实后，才有依据更新原件策略。[Touchline 开发文档](https://touchlinewire.com/docs#attribution) · [条款第 2、3 节](https://touchlinewire.com/terms.html) · [OpenAPI 原件](https://touchlinewire.com/api/v1/openapi.json) · [OpenAPI 许可字段定义](https://spec.openapis.org/oas/v3.0.3.html#license-object)

15:38 UTC 另取全联盟伤停接口，发布方声明 92 行，实际也是 92 行。其中 84 例伤病均缺 `injury_started_at` 和 `injury_date`，6 例停赛均缺 `suspension_competition`，另有 1 例疾病和 1 例无出场资格。`expected_return` 表示预期回归，`timestamp_iso` 是快照时间；两者都无法确定本次受伤起点。累计红黄牌也无法单独确定停赛适用于哪项赛事。OpenAPI 未声明返回行模式，未查到提供这些字段的时间承诺。报告只发布字段、计数及指纹。[公开伤停接口](https://touchlinewire.com/api/v1/injuries) · [当次核对](../../reports/five-leagues-0.5.0a3/news-source-readiness.json)

| 候选 | 本次决定及依据 |
|---|---|
| 英文维基俱乐部赛季页 | 继续轮询。验证每份响应的页面标识及许可，保存完整页面响应，保留贡献者署名和永久修订链接，分享改编文字时说明修改并遵循相同或兼容许可。图片、链接的媒体正文和外部引文各有权利范围。维基是二手来源。[Wikimedia 内容复用说明](https://www.mediawiki.org/wiki/Wikimedia_APIs/Content_reuse) · [基金会条款第 7 节](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use/en#7._Licensing_of_Content) |
| 英超官网 20 队伤病总表 | 可供人工核查。网站条款对纳入数据库另有事先书面批准要求。[英超官网](https://www.premierleague.com/) · [使用条款](https://www.premierleague.com/en/terms-and-conditions) |
| Guardian Open Platform、Injury Wire | 前者免费开发访问的条款限制机器学习和数据挖掘。后者列明五大联赛伤病时间线和数据授权服务，但其公开说明未提供符合本研究的免费原件归档许可。[Guardian 条款](https://www.theguardian.com/open-platform/terms-and-conditions) · [Injury Wire 数据说明](https://injurywire.com/data/) |
| Wikinews | Wikimedia 的新闻项目于 2026 年 5 月转为只读，保留历史资料，已停止发新稿。[Wikimedia 项目状态](https://meta.wikimedia.org/wiki/Wikinews) |

## Claude 提出的三项登记决定

1. **来源。** 继续默认免费采集，暂缓在部署目录执行 v2 登记。新增球队 RSS/Atom 时，授权须明确覆盖自动获取、保存整份订阅响应、提取及建模，并核实其中第三方内容。用未来两队的真实收件检查队名、球员、发布时间、原件和提取结果。`news add-feed` 的授权参数记录操作者已核实的许可。每条研究所用事实仍需自己的原件；补订阅不能给之前的事实补发收据。
2. **新闻策略。** 登记固定当时的 `epl-player-news-paired-v3` 及其 SHA-256，而订阅和功能开发可继续迭代。希望同一试验持续累积到 2028 年 7 月 1 日 00:00 UTC 的部署方，须决定能否继续产生该策略下的封存。切换新闻策略后，新策略的封存按 `incompatible_registered_news_policy` 单列；另立方案可以评估新的策略。符合原配对条件的概率实现更新按版本分层。
3. **方案兼容。** 0.5.0a2 已发表的 v2 方案指纹固定为 `3db775698c93664b5ba749be8a03f94f75940f212438849ad63e4cf672002a61`。已登记实例使用原证书，兼容性防回归覆盖方案文字和功效表。请先运行只读检查；只有正式决定参与时才在实际收件目录登记。[v2 操作与报告](NEWS-ENDPOINT-V2.md)

## 在部署机查看

`STATE` 指向英超服务实际使用的数据目录。以下命令适用于第 12 轮；查看后续轮次时替换年份、轮次和比赛标识。`preflight` 只读，空库、尚未到窗或 0 个可查时窗，均无法证明已有合格比赛。

```sh
pm861 --data-dir "$STATE" season readiness --year 2026 --round 12
pm861 --data-dir "$STATE" news sources --team 'Chelsea FC' --season 2026-27
pm861 --data-dir "$STATE" news status
pm861 --data-dir "$STATE" news facts epl/2026-27/nottingham-forest/chelsea
pm861 --data-dir "$STATE" leagues news-endpoints preflight --version v2
pm861 --data-dir "$STATE" leagues news-endpoints report --version v2
```

已载入赛程的库可按下列顺序单次采集。实测用独立副本，含 380 个已注册的英超赛程，在副本内单独启用原件订阅以验证原生重开路径。首次 `season run` 处理了 4 个订阅，随后 `news sync` 处理其余 56 个。常驻部署继续由 `season run --repeat` 调度，后续收件遵守上游缓存时间。其余四国使用各自的来源验证与新闻准入流程。[五联赛来源说明](LEAGUES-AND-SOURCES.md) · [新闻准入](NEWS-ADMISSION.md)

```sh
pm861 --data-dir "$STATE" season run --workers 2
pm861 --data-dir "$STATE" news sync --season 2026-27 --limit 60 --workers 3
```

登记时还须核对两队**每个计入有效覆盖的来源**，包括报告零条消息的响应，以及封存内所有事实、佐证和相应名单的原件。实测中，即使两队名单和实际加权事实均有原件，另一个报零条的来源缺少原件仍会使该窗排除。部署后用 `preflight` 逐队查看 `coverage` 和 `fact_dependencies`。
