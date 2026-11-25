# 当季英超赛果

`pm861 result-feed season 2026` 按赛季调度赛果、重建训练输入并重新发布受影响的轮报。加 `--offline` 可只重放本机实际收取的原件。`pm861 result-feed sync 2026 --round 11` 定向查看 OpenLigaDB，`pm861 result-feed secondary 2026` 按缓存和限流策略检查独立来源。

| 来源 | 收件、许可与可用范围 |
|---|---|
| OpenLigaDB | 原生完整 HTTP 响应要明确终场、唯一的 `After90Minutes` 比分、主客、轮次、实际开球和赛事 ID。单源确认须两次实际获取，相隔至少 30 分钟，收件时均已开球至少 90 分钟。变更标记和本地缓存不产生第二份凭证。数据库遵循 ODbL 1.0。 |
| English Wikipedia | 使用 MediaWiki 官方匿名 API，一次请求获取已登记英超 20 家俱乐部当季文章和站点授权，保存原始字节、请求起止 UTC、SHA-256 及每篇文章的永久修订链接。只解析各自 Premier League 段中的联赛比赛框，要求主客、轮次、真实可换算的 GMT/BST 开球、联赛官方比赛链接 ID、比分和该俱乐部的 W/D/L 一致。文章修订本身也须晚于开球 90 分钟。双俱乐部记录属于同一来源。 |
| OpenFootball | 固定带 Git 修订和对象哈希的公共领域赛季文件。已有比分可交叉核验，空缺位置仍等待其他终场证据。 |
| 人工材料 | 单场或多场附获准使用的原件、位置及 SHA-256。更正或解除隔离需要逐场裁决理由。 |

OpenLigaDB 在开球后 90 分钟起的 48 小时内优先检查尚无比分的轮次，每 30 分钟最多查询一次该轮的小型变更标记。需要确认稳定终场时才重复获取完整响应。每轮调度最多检查三个轮次，更早的未决场次通常每六小时复查。Wikimedia 在有近期缺口或等待第二份稳定终场时最快每 30 分钟一次，否则有未决结果时每六小时、无近期缺口时每天一次。它将整轮俱乐部合并为一个请求，使用联系地址、`maxlag=5`、共享缓存和重试退避，并遵从服务器的 `Retry-After`。这些是本项目的请求间隔。

`pm861 result-feed freshness 2026` 返回最近轮次已过终场窗口、已有比分和未收件的场数，最早缺失裁决距今的小时数，以及下一轮每队最近两场的缺口和对应比赛 ID。`--at 2026-11-25T10:00:00Z` 可复查当时实际可见的状态。下一轮的阻断数按该场 T-60m 的时间点投射，使用当前截止已经收到的比分。球员信息单列已取到的伤停、牌、出场和阵容凭证及收件时间，空缺记为 `unavailable`。

独立来源的署名为 **English Wikipedia contributors**。输出逐场永久修订链接及来源声明；本项目提取、规范化的 Wikipedia 比分、时刻及对应衍生表以 CC BY-SA 4.0 提供。原样页面文字、球员名单和影像不随仓分发。原件冲突、同一官方 ID 被用于两个已终场对阵、撤回，以及无法核实时区均保留可追溯状态；后补收件按本地收件或裁决时间进入训练。[使用条款](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) · [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) · [MediaWiki API 礼节](https://www.mediawiki.org/wiki/API:Etiquette) · [免费接口](https://en.wikipedia.org/w/api.php)

Football-Data.co.uk 的使用条件限制自动下载与 AI 数据训练，open-matchday 对其汇总及下游源有独立限制。这两类来源未接入本管线。[Football-Data.co.uk 使用说明](https://www.football-data.co.uk/contact.php) · [open-matchday 来源与许可](https://github.com/oladeeayo/open-matchday#license-and-attribution) · [FixtureDownload 条款](https://fixturedownload.com/terms)

## 2026 年 11 月 25 日的真实补数

在重新登记 17 季固定输入的本机库，第 10 轮的 10 场由 OpenLigaDB 与 Wikipedia 比分互证，其中 8 场开球偏离占位赛程。第 11 轮的 10 场在 OpenLigaDB 均未标终场，另有 5 场开球偏离登记时间。Wikipedia 于 10:09:43 UTC 和 10:39:50 UTC 分别返回同一批次的实际原件，第二次请求在首份收件后 30 分 2 秒发起。两轮 20 场的终场均已从保存的原件逐场重新验证。

| 实测项目 | 结果 |
|---|---|
| 前 11 轮可用终场 | 109 / 110；第 4 轮 Bournemouth–Brentford 在 OpenLigaDB 为 2–1，两家俱乐部的 Wikipedia 文章和固定 OpenFootball 文件均为 2–2，主库保留隔离。 |
| 第 12 轮 | 10 场、20 队。补回前重新登记库 10 场满足连续两轮缺失条件。补回后 0 场因该条件受阻，10 份赛前早期技术预测与状态页逐场一致，均使用 1,249 场训练数据。 |
| 名单和真实可交易性 | 这次重建只收取赛果数据。10 份技术预测均缺两队已确认首发，实测只核查赛果新鲜度与训练输入。 |

完整分母、逐场时刻和比分、真实 UTC 收件、永久修订链接、入库方法、哈希及 10 份早期预测摘要见[本次原件复核报告](../reports/result-sources-2026-11-25-0.3.0.json)。

## 未来七天的开球时间

赛季调度会核对已登记赛程、OpenLigaDB 的原生分轮响应及 Gamma 主赛事原件。准入要求主客顺序、赛季、轮次和 UTC 开球时间吻合，Gamma 至少有一份已核实常规时间规则。报告 `epl-upcoming-schedules-2026` 逐场列出原时间、确认时间和延后审核原因。运行 `pm861 result-feed season 2026` 会采集并核验，`--offline` 可用归档的原件复核。[第 12 轮实测及原件标识](../reports/upcoming-schedule-2026-11-25.json)

自动修订最多移动 36 小时，须早于两个候选开球时刻中较早者的 T−60。调度保存本地准入时间和两个来源的原件摘要。往期预测保留原赛程，新的赛程用于后续预测和评分。身份歧义、已临近决策、来源矛盾和大幅改期进入人工审核。实际第一声哨与交易所兑付继续使用各自的独立证据。

当两个原件同意的修订超过单次或累计 36 小时，`epl-schedule-review-2026` 列出登记时刻、OpenLigaDB 时刻、Gamma 候选时刻和延后原因。处理人先读原件，再运行以下命令，将变量替换为报告中的标识和 UTC 时刻。

```bash
pm861 result-feed approve-schedule 2026 "$FIXTURE_ID" --kickoff "$PROPOSED_UTC" --reviewer "$REVIEWER" --reason "$REASON"
pm861 result-feed withdraw-schedule 2026 "$FIXTURE_ID" --kickoff "$APPROVED_UTC" --reviewer "$REVIEWER" --reason "$REASON"
```

确认命令重新核验两份最近 24 小时收到的原件、联赛身份、主客顺序、常规时间市场和指定时刻。改期的新开球须在当前时刻一小时以后。原登记时刻已过的延期仍可确认，变更从本机这一刻起生效。撤回命令挂起该版赛程，保留确认人、理由、证据摘要及生效前的预测。

## 赛后时间对账

`pm861 result-feed season 2026 --offline` 重开已收件的归档。它按英超赛季、轮次和唯一有序主客组合逐场核对。报告的 `postmatch_pairings` 给出 `registry_kickoff`、`actual_kickoff`、常规时间比分、确认方法和 `proof_sha256`。两个开球不同时，须由两个不同提供方给出相同的精确时刻。双方比分相符时可直接确认。一方尚未出终场时，系统要求报出比分的一方有两次独立的、相隔至少 30 分钟的请求，且身份、分数和开球自上一份终场起连续一致。缓存、页面修订日期及同次请求中的多篇文章共用一次收件时间。

已确认的真实比赛时间随该版赛果进入训练。从本地认证时点起，下一场预测才可用该赛果。早于登记占位时刻实际完赛的比赛也遵循同一规则。历次数据清单和封存保持可按原时点复现。轮报给固定时距预测评分时核对真实开球，对已经开球后才封存的预测保留缺口。对已证实改期的比赛，历史回测和候选模型使用真实开球计算 T−60。若在这个决策截点之前收不到对应赛程，比赛保留在分母，评分列明缺少当时可知的赛程证据。自动结算还会重验两边的原件，检查持仓所用开球与该场真实开球一致。

原件撤回、比分矛盾或身份冲突会暂停相应场次，并重算训练清单和轮报。处理人也可以按已确认的证明摘要挂起一场，调查完毕后解除。解除操作会重新读取原件并执行准入规则。

```bash
pm861 result-feed review-pairing 2026 "$FIXTURE_ID" --proof "$PROOF_SHA256" --action withdraw --reviewer "$REVIEWER" --reason "$REASON"
pm861 result-feed review-pairing 2026 "$FIXTURE_ID" --proof "$PROOF_SHA256" --action release --reviewer "$REVIEWER" --reason "$REASON"
```

## 批量人工导入

把原始比赛记录放在清单的同一目录。整轮使用 `import-round`，同轮一场或多场使用 `import-matches`。两条命令都要求填写原件实际的 SHA-256，比分顺序为主队在前。清单中的 `kickoff` 填原件载明的真实 UTC 开球。它与登记时刻不同时，还须有一份已归档的独立时刻匹配、顶层 `reviewer` 和逐场 `decision_reason`。

```json
{
  "season": "2026-27",
  "round": 11,
  "rights": "permission",
  "source": "获准使用的终场赛报",
  "reviewer": "具名复核人",
  "evidence_file": "official-match-sheets.pdf",
  "evidence_sha256": "填写附件的64位SHA-256",
  "matches": [
    {
      "home": "主队名",
      "away": "客队名",
      "kickoff": "2026-11-21T15:00:00Z",
      "status": "final",
      "period": "regulation",
      "home_goals": 2,
      "away_goals": 1,
      "locator": "原件第1页第3行",
      "decision_reason": "签署的终场更正注明主队第二粒进球有效"
    }
  ]
}
```

`rights` 接受 `user-owned` 或 `permission`。`pm861 result-feed import-round 2026 11 round.json` 要求清单覆盖该轮全部登记场次。`pm861 result-feed import-matches 2026 11 matches.json` 只处理清单中列出的场次。两者都会在同一事务中验证并写入数据，返回 `batch_id`。修正导入错误时可运行 `pm861 result-feed rollback-batch BATCH_ID --reason "上传了错误版本"`。如果该批次之后已发生赛果或赛程更正，系统会保留后续裁决并拒绝过时的整批回滚。

### 两个终场来源明确分歧时的训练裁定

`pm861 result-feed dispute-evidence 2026 epl/2026-27/bournemouth/brentford` 显示两份已过正常完赛时间的原生终场记录。如果赛事身份和时刻相同且比分不同，把返回的 `training_dispute` 原样填入人工清单中对应的 `matches[]` 项，列出实际选择的原文比分、`decision_reason`，在清单顶层注明实际复核者 `reviewer`，同时保留获准使用的原始附件。`import-matches` 原子验证两个来源的赛事 ID、主客、轮次、开球、终场状态、比分、原件 SHA-256 和 UTC 收件，也绑定当时的 OpenFootball 比分。

裁定只影响训练输入。正常离线重放重读当初的原件，后来携带相同比赛事实的 HTTP 响应可继续使用。任一来源改分、撤回、改期、变更身份、丢失已验证的原件，或固定参考源出现不同事实，均重新隔离；改动后再恢复原分也会保留隔离。纸面正式结算持续要求独立的标准终场证明。正常的批次回滚会恢复原裁决，并允许按历史时点查看曾经有效的训练结果。

OpenLigaDB 公共数据使用 ODbL 1.0。公共展示需署名，公开分发修改后的数据库时须遵守其同许可和开放取得要求。Data from OpenLigaDB (www.openligadb.de), licensed under ODbL 1.0. [数据许可](https://www.openligadb.de/en/license) · [API 条款](https://www.openligadb.de/en/terms-of-use) · [接口说明](https://api.openligadb.de/index.html)

OpenFootball 将英超赛程和赛果数据作为公共领域数据发布。[数据说明](https://github.com/openfootball/football.json/blob/master/README.md)
