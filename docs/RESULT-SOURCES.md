# 当季英超赛果

`pm861 result-feed season 2026` 检查 2026/27 英超赛果、刷新训练输入并重算受影响轮次。状态页的 `epl-regulation-2026` 报告列出已核实的 90 分钟比分、比赛身份冲突、逾期比赛和收件故障。每次最多轮询三轮，重复运行可继续补查。使用 `--offline` 可从本机保存的原件复核。指定一轮可用 `pm861 result-feed sync 2026 --round 11`。

| 来源 | 采信规则 |
|---|---|
| OpenLigaDB | 两份独立 HTTP 响应至少相隔 30 分钟，比赛 ID、球队顺序、英超赛季、比赛轮次、开球时间和比分均一致。两次响应均须表明比赛结束，明确提供唯一的 `After90Minutes` 结果，且收件晚于开球 90 分钟。缓存命中及单独的变更标记不会增加收件数。已知赛程或独立比分矛盾即隔离。 |
| OpenFootball | 核实公开数据集的 Git 提交与赛季文件对象。对已登记比赛补齐缺失赛果并交叉核验。GitHub REST 配额用尽时使用同一公开仓库的 Git 接口。已有归档可离线使用。 |
| 人工批量材料 | 需要原始附件、该附件的 SHA-256、逐场定位、清楚的使用权、赛程身份和明确的 90 分钟状态。修订已有比分或解除隔离还需逐场填写裁决理由。 |

新补赛果从本地准入时刻进入训练。更正和撤回保留时序，系统重新生成受影响轮报并在已完成的正式轮次复核到期试验。原始预测及其摘要保留。超过登记开球 24 小时仍没有可用终场的比赛进入 `epl-overdue-results-2026` 告警。未取得比赛时的人员名单、盘口或其他准入证据，仍按各自的规则处理。交易所结算另按合约原件核实。

## 批量人工导入

把原始比赛记录放在清单的同一目录。下方展示清单结构；实际须列出已登记的**整轮每一场**，并填写原件实际的 SHA-256。比分顺序为主队在前。

```json
{
  "season": "2026-27",
  "round": 11,
  "rights": "permission",
  "source": "获准使用的终场赛报",
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

`rights` 接受 `user-owned` 或 `permission`。命令 `pm861 result-feed import-round 2026 11 round.json` 在同一事务中验证并写入整批数据，返回 `batch_id`。修正导入错误时可运行 `pm861 result-feed rollback-batch BATCH_ID --reason "上传了错误版本"`。如果该批次之后已发生赛果或赛程更正，系统会保留后续裁决并拒绝过时的整批回滚。

OpenLigaDB 公共数据使用 ODbL 1.0。公共展示需署名，公开分发修改后的数据库时须遵守其同许可和开放取得要求。Data from OpenLigaDB (www.openligadb.de), licensed under ODbL 1.0. [数据许可](https://www.openligadb.de/en/license) · [API 条款](https://www.openligadb.de/en/terms-of-use) · [接口说明](https://api.openligadb.de/index.html)

OpenFootball 将英超赛程和赛果数据作为公共领域数据发布。[数据说明](https://github.com/openfootball/football.json/blob/master/README.md)
