# 五联赛模拟资金重放

每国从 2,000 模拟美元开始，合计 10,000。每笔按指定数量全成或跳过，含现金费用最多 50。同场所有未兑付头寸的买入成本之和上限为 150。互相对冲可能降低实际最大损失。回放按成交时点、预先指定的优先级和意图 ID 顺序竞争资金与盘口深度。同一时刻先处理已收到的兑付。

## 重放合成压力场景

```bash
pm861 paper-risk stress --write-input scenario.json --output capital-stress.json
pm861 paper-risk replay scenario.json --details
```

导出的 `synthetic` 文件含假设的两次盘口、费用和双向兑付。两次盘口收件须相互独立，决策快照不晚于决策，执行快照严格晚于决策并早于开球。重复投递相同意图 ID 不再扣款，冒用别名也不能恢复已经消耗的盘口深度。两枚代币均有相符的终局记录后才释放占款。[48 场报告](../../reports/five-leagues-0.5/capital-stress.json)

## 核验本机收件

```bash
pm861 --data-dir /srv/pm861 paper-risk replay captured-input.json --as-of 2026-11-26T12:00:00Z --details
```

使用 `evidence_mode=local_receipts`。程序只读打开运行目录中的 `five-league-market/runtime.sqlite3` 和内容寻址原件。也可直接传入该采集目录。报告中的成交表示在归档盘口上按预算模拟出的买入。场外已经执行的真实订单数量单列为零。

| 输入 | 核验依据 |
|---|---|
| 联赛和比赛 | 将 `fixture_id` 写成 `联赛/赛季/Gamma主队数字ID/Gamma客队数字ID`。以原始 Gamma 的国内赛事、系列、标签、主客球队及赛季日期和开球时间核对。 |
| 意图 | `condition_id`、`token_id`、`selection`、`rule_sha256` 和完整的 `fee` 均绑定决策时点的已审合约。球数需同时写明玩法、线和方向，单队玩法另写主队或客队。费用关闭也需要原件支持。 |
| 盘口 | `decision_book` 和 `execution_book` 分别引用 `books` 中的一项。两项均提供代币、合约、比赛、`received_at`、逐档 `asks`、本机原件 `source_sha256`。按正式时窗重新选择并解析原生 CLOB 报价，验证有效状态、时间、价量、最小订单、规则和费用。执行目标是决策后至少 5 秒或场上合约要求的更长延迟。 |
| 兑付 | `redemptions` 引用持有代币、合约、比赛、事件时间和持有边原件的 SHA-256。根据本机成功采集的请求日志，重新读取双方官方原件，核对两边的代币 ID、顺序、方向、比赛、关闭状态、开球后收件时间和互补兑付。支持 0、0.5、1。若双方各为 0.5，还要从入场时的原件哈希重建具体结果代币的条款，确认条款明确允许平分。普通文件上传及自填金额无法证明兑付。 |
| 缺件及冲突 | 未核实的盘口列为 `skipped_unverified_entry`。首次终局之前，真实空列表允许稍后重查。即使两枚代币在同一时刻收到完全相同的空响应，也按各自的请求核对。待定记录可以省略方向等字段，其明确给出的合约、方向、索引、对边代币、赛事和盘口标识须与冻结原件一致。缺少对边终态时资金继续占用。已入账后如出现冲突，截止到新原件的报告须先解决争议，早于该原件的历史报告仍可重放。 |

### 交易所没有给出完整赛前标记时

默认入场资格为 `explicit_pregame`，两份盘口均需绑定明确的赛前状态。要测量缺少部分标记会漏掉多少可研究的模拟入场，可以用同一组本机原件再跑一次。

```sh
pm861 --data-dir /srv/pm861 paper-risk replay captured-input.json \
  --entry-basis scheduled_with_incomplete_flags --details --output scheduled-paper.json
```

排期研究档要求独立交易所原件证明收件时盘口开放、两份簿均在同一计划开球前、主附赛事的赛程及身份稳定。任一段明确开赛、结束、撤销，或状态格式无法解释，该笔都会跳过。两次收件之间即使先改期再改回，也会留下冲突。每一腿仍须能核实同一代币、冻结规则、费用、盘口深度与执行延迟。后来收到的排期更正若把开球移到模拟买入时刻或更早，或者同场获核首哨证明已开踢，该弱证据买入在新的研究报告中会被剔除。`--as-of` 可以复查该项反证到达前可得的结果。

两档都复核每份订单簿绑定的父赛事，以及截至执行时收到的主赛事和附加盘口赛事状态。父赛事已经关闭（`closed=true`）、归档（`archived=true`）或停用（`active=false`）时，即使子盘口显示开放、且两枚赛前标记明确为假，也会跳过。赛后才收到的正常关闭状态保留原赛前买入，双方终态齐备后可按冻结合约兑付。

决策和执行各自引用的 Gamma 响应还须通过 HTTP 新鲜度校验。收到响应时的缓存年龄最多 30 秒，服务器时钟与收件时刻最多相差 30 秒，请求耗时最多 30 秒。交易所数分钟前已经新鲜发布、随后保持稳定的赛程可以使用。过期缓存不能提供赛前状态的正面证据，既有明确开赛反证仍然有效。

`decisions[].entry_evidence` 给出决策簿和执行簿各自的证据档、CLOB 原件、绑定的 Gamma 原件和排期收件。两腿中有一腿仅能核实排期，就将整笔归入 `scheduled_with_incomplete_flags`。`entry_policy.outcomes_by_league_and_evidence` 按国和整笔证据档分别列出成交与跳过数。两次回放各自使用独立的初始研究预算。该排期档只用于模拟。五国均复核 Gamma 改期。现有独立首哨解析器只认英超，支持读取市场子目录及其上层部署目录中的足球原件。[原生赛前回放及反证](../../tests/test_budget_scheduled_entry.py) · [五国采集路径与隔离测试](../../tests/test_budget_domestic_archive.py)

### 只核对旧档案中的 Gamma 合约

五国采集器为每份合同保存球队 ID、所属联赛、主客顺序、名称和缩写。离线核查会按当次请求的采集器，从 Gamma 原件重新提取当时写入的字段，再与本地合同逐项比对。旧英超单联赛采集器使用其原有记录格式。

下面这份德甲平局合约来自 2026 年 11 月 26 日 07:25 UTC 的存档。将目录换成本机留存原件所在目录；其他采集批次可使用各自的合约 ID 和截止时间。

```sh
pm861 --data-dir /srv/pm861 paper-risk verify-contract \
  0xa36b70b3b9eea23f57efc7be7ac2c3836e476fa5a741cd8f058da17437c3cd98 \
  --as-of 2026-11-26T07:25:07.785688Z
```

该次复验返回赛事 `bun-wer-moe-2026-11-27`、双方球队 ID 和源文件 SHA-256 `f8174ded2ec9ebc0f1f0cdf64fd0c75aa6981d36b290b4c7805cf8809a681c57`。相同旧目录的西甲、意甲、法甲合约也通过核对。输出 `order_books_checked: 0` 说明该命令仅核查 Gamma 合约。判断一个固定时窗是否收齐由 `market league-report` 的分层收件结果给出，判断某次模拟入场则需要 `paper-risk replay` 的两份及时订单簿。

### 收费盘口的费用、持仓和收益

用 `verify-contract` 输出中的 `tokens` 选择结果代币。指定同一 `--as-of` 和入场档后，`intent_terms` 会给出该决策时点的代币、选择方向、规则哈希与完整费用字段，可直接并入对应意图。回放会从原件再次核对这些字段。

```sh
pm861 --data-dir /srv/pm861 paper-risk verify-contract "$CONDITION_ID" \
  --as-of "$DECISION_UTC" --token-id "$TOKEN_ID" \
  --entry-basis scheduled_with_incomplete_flags
```

| 逐笔费用依据 | 适用条件 | 资金处理 |
|---|---|---|
| `gamma_fees_disabled` | Gamma 明确给出 `feesEnabled=false` 且没有非空费表。 | 买入成本为盘口名义金额。 |
| `authoritative` | 费表、合约协议、玩法和生效时间匹配唯一已审政策，两腿均能复验当时已留存的权威原件。 | 按该政策扣现金或扣份额。 |
| `research_assumption` | 显式使用排期研究档，收费开关为真，当前协议和玩法尚无登记政策。Gamma 提供可解释的 taker 费表，决策和执行间的原件一致。 | 按下述研究约定扣买入份额，兑付与收益仅用于模拟。 |

已有登记政策但证据缺失、过期、歧义或费率不匹配时，该笔跳过。缺少费用开关或参数、未知字段、执行前改费再改回也会跳过。赛前标记齐全与否单列在 `entry_evidence`。即使两腿都明确赛前，使用估算手续费的交易仍归入 `research_assumption`。默认 `explicit_pregame` 处理收费盘口时只采用经核实的登记政策。[费用登记表](../../src/polymarket861/fee_registry.py)

研究档沿用英超影子账户已有约定。在两本盘口可共同执行的每个价格 `p` 上，取该档成交份额 `q`，计算现金等值 `q × rate × (p × (1 − p))^exponent`，向上取整到 0.01，再除以 `p`，向上取整到 0.000001 份，从买到的份额中扣除。允许指数 1 或 2，只收 taker，返佣按零计算。举例，100 份以 0.50 买入、费率 0.05、指数 1，需支付 50，扣 2.5 份，持有 97.5 份。互补终态核实后，赢方兑付 97.5，净收益为 47.5。缺少任一边的终态时，50 继续占用。

加 `--details` 后，`decisions[].fee_evidence` 列出费表版本及两份订单簿所绑定的 Gamma 来源，`fill.levels` 给出逐档现金或份额扣费，`fill.net_shares` 为可兑份额。每国的 `settled_paper_pnl_by_fee_basis` 按已有成交的费用依据分列持仓成本、已兑金额和已兑头寸净收益。全部为纸面结果，`venue_realized_pnl` 保持为空。[收费、深度、兑付与费用凭证的集成测试](../../tests/test_budget_research_fees.py)

2026 年 11 月 26 日的两次真实四国采集，每库各 912 份受支持合约均开启手续费，原件给出费率 0.05、指数 1、仅收 taker、返佣参数 0.15。本版在每库分别从 Gamma 恢复了 1,824 个代币的研究条款。默认档当日有据可用的收费条款为 0。第一库 2,280 份订单簿中，同代币重复收件的最短间隔约 139 秒。第二库的 48 份订单簿各属不同代币。两库都没有可重放该买入时窗的两份簿。五国的收费买入至兑付另以原生格式的受控行情验算。[两版对照、真实档案和五国模拟结算](../../reports/five-leagues-0.5.0a3/paper-fee-verification.json) · [复核脚本和输入路径示例](../../reports/five-leagues-0.5.0a3/verify-paper-fees.py)

本机回放的开盘及成交路径有一项覆盖采集器、原件、费用、两次盘口和互补终态的集成测试，使用受控行情输入。另以仓库保存的真实南特–图卢兹双方 Data API 原件重验最终兑付，Under 为 1。该真实样本支持终态判断，赛前模拟成交还需同场的合约、费用和两份及时盘口。[双边原件](../../reports/five-leagues-0.5/settlement-observation.json) · [集成与反例测试](../../tests/test_capital_native_proof.py)

在原生格式的受控回归中，主胜 Yes 的取消条款规定兑付 0。即使两枚代币均报 0.5，该笔 50 模拟美元仍占用。具有已审平分条款的全场和单队大小各入账 50；同一个主胜合约收到 1 与 0 的合法二元终态时入账 100。每例都从模拟买入重放到最终余额。[冻结条款核验报告](../../reports/five-leagues-0.5/capital-rule-verification.json)

2026-11-26 核对的国际站费用页公布体育市场 taker 参数 0.05、以 USDC 计算的对称公式和五位小数精度。仅有上述字段，还无法从当时逐合约收据确定所有实际扣费细节，例如各次成交与同价位多笔成交的取整和汇总方式。两份真实快照中的原始 `feesEnabled` 按其采集时的值保留。[Polymarket 费用说明](https://docs.polymarket.com/trading/fees) · [本版原件采样](../../reports/five-leagues-0.5.0a2/market-observation.json)
