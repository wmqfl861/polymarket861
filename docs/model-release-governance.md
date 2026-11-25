# 英超赛季中的模型研究、选单和版本生效

每次登记先选起始赛季和轮次，然后固定随后 38 个官方轮次，每轮 10 场。以 2026/27 第 12 轮起步，窗口将在 2027/28 第 11 轮结束，分母始终为 380。延期比赛按所属的官方轮次计入，最后一场补赛给出可信结果后才揭晓。已登记的旧 12 轮或从首轮起的 38 轮沿用自己的原始协议、预测和审批记录。

| 用途 | 历史材料 | 前向材料与生效时间 |
|---|---|---|
| 概率研究 `probability-research-24h` | 至少两个在登记赛季之前结束的完整赛季。校验原始文件指纹，额外载入各目标赛季之前三个训练赛季。固定假设为完赛数据从开球后 24 小时可用，开球时刻取最终档案。历史样本缺少当季新闻和首发收据。 | 历史段算完即可具名评议。38 轮结束后，候选与基线按同场、同模式、同截止点配对。每场优先采用两边均有的正式预测，否则选两边均有的技术预测。整窗至少 361 场、38 轮全覆盖。达到预登记统计门槛并具名签收，状态为 `research_reviewed`。 |
| 正式概率模型 `formal-strict` | 两个及以上完整赛季的严格收据和同输入回放，历史场场配对。 | 需要正式试验在第一场截止前启用、完整前向评估和独立候选模拟账户的可核验意图与成交。具名批准后改变正式概率指针。 |
| 选单策略 | 已登记真实市场历史回放、正式父单、可核验盘口、结算和固定机会池，保留完整的入场与收盘证明。 | 未来 38 轮两臂用相同机会池、真实盘口及固定预算，连空机会也封存。满足原有 A 级资格及两阶段统计门槛后具名批准。缺少真实盘口时只能记录缺口。 |
| 发布包更新内置 B1 | 在五个固定赛季上对旧、新实现逐场比较四项概率得分和所有已配置的半球线。 | 本机的具名负责人核对报告，再签署生效。生效时间由本机事务记录，上一版本的预测摘要可按原版本取回。内置版本更新独立于正式模拟账户的启用条件。 |

季中登记必须已收到起始轮的十场比赛，且登记时窗口内的任何已知决策都还未开始。下一季赛程未发布时，登记保留 38 个轮次及已知比赛。新增比赛须有赛前 T−60 前的本机收件证据。缺比赛、重复、换轮、已登记的比赛消失和赛后才录入的赛程会阻止整窗揭晓。相同目标的重叠窗口只能共享相同的起始轮、基线和证据口径。统计聚类用“所属赛季、官方轮次”，同时校正候选数、两阶段和五个评分端点。

## 当前赛季的研究样例

以下命令在本机已收齐 2026/27 第 12 轮赛程、且首场 T−60 尚未到来时使用。`HISTORY` 指向按 `YYYY-YY/en.1.json` 组织的固定来源目录，`STATE` 是保存本机证据的目录。

```bash
pm861 --data-dir "$STATE" forecast recipe league-frequency B0
pm861 --data-dir "$STATE" experiment register league-frequency-2026 --kind model \
  --version league-frequency --baseline B1 --window 2026-27/12 \
  --evidence-track probability-research-24h --hypothesis "Compare league frequencies with team strength"
pm861 --data-dir "$STATE" experiment history-study league-frequency-2026 "$HISTORY"
pm861 --data-dir "$STATE" experiment review-study league-frequency-2026 \
  --reviewer "$REVIEWER" --reason "$REVIEW_REASON"
pm861 --data-dir "$STATE" experiment evaluate league-frequency-2026
```

使用本地 Codex 产生并通过可行性验证的模型提案，会登记下一轮尚未开始的完整已知官方轮，并使用概率研究口径。赛前调度器按窗口封存基线和候选。历史已有具名评议时也必须等整窗关闭才能查看前向比较。

仓内这次实际以最近两季比赛检验联赛频率 B0 与球队实力 B1。2024/25 和 2025/26 两季共计 760 场、76 个官方轮，B0 得分 73.474232，B1 得分 74.791925。B0 减 B1 为 −1.317692，预登记的 10 重校正区间为 [−2.104267, −0.531117]。本次隔离研究登记的未来 380 场尚无前向配对成绩。独立复跑使用 `python scripts/reproduce_probability_trial.py "$HISTORY" --state "$ISOLATED_STATE" --season 2026-27 --first-round 12`。保留同一个隔离目录可重读原登记，报告含实验标识、登记和赛程实收时间、具名评议事件以及实际赛前封存数量。使用新目录会产生当时的新登记收据，起始截止已过时重新登记会被拒绝。历史打分是 24 小时假设研究。[两季逐项结果和归档指纹](../reports/research/probability-research-history-0.3.0.json)

## 内置版本换代

生产预测绑定家族、大小球线、算法、训练和预测装配、证据筛选、赛果准入、赛程与时点输入等代码摘要，以及 NumPy 和 SciPy 版本。新增、删除或修改结果收集、赛程、上下文和足球计算模块都会改变实施指纹。预测还封存当时实际使用的输入清单、证据帧和训练摘要。各自的模型版本绑定不可覆盖。选单登记单独绑定规则、选单、账本和证据实现代码。不同依赖版本可能产生不同的实施指纹。

1. 更换版本前运行 `pm861 --data-dir "$STATE" forecast release-baseline "$HISTORY"`，冻结正在使用的内置实现的五季逐场输出。0.2.0 旧库可使用随包提供的标准版本基准。旧配方只记录两个算法文件，无法单凭旧库识别本地实际的训练装配代码。若采用随包基准，负责人须核实上一个安装包的提交，并在第 3 步加上 `--legacy-reference 8993f15cfcc61042aedc5e885bb82217488ecb33`。自定义旧实现须先提供其真实冻结基准。
2. 安装新包后运行 `pm861 --data-dir "$STATE" forecast release-compare "$HISTORY"`。五季为 2021/22 至 2025/26，含 1,900 场、190 个官方轮。报告给出两个实现摘要、同一批原始文件与截止规则、综合与分项分差、训练视图及全盘口输出变化数。资料不足时，旧预测可用 `forecast get PREDICTION_SHA256` 独立取回，新默认预测等待有效版本。
3. 核对后运行 `pm861 --data-dir "$STATE" forecast release-approve "$REPORT_SHA256" --reviewer "$REVIEWER" --reason "$REVIEW_REASON"`。签署事务再次检查两边实施指纹、报告、现用版本和已冻结的比较资料。新版本只在实际签署后生效。本轮报告的 `main_model_rollout` 列出每个实际封存的主版本、采用该版本的比赛以及切换对应的截止点。
4. 独立复核者可在空目录运行 `pm861 --data-dir "$VERIFY_STATE" forecast release-compare "$HISTORY" --reference-0.2.0`，对照随包的旧版基准。旧版基准使用 `8993f15` 的真实实现，从固定八季原始档案生成。用该提交的 `src` 作为 `PYTHONPATH`，运行当前仓库的 `scripts/reproduce_probability_baseline.py "$HISTORY" regenerated.json.gz`，即可独立还原逐场输出。

0.2.0 与此次发布的 B1 在五季样本上得分均为 75.447813，1,900 场中改变了六项核心概率或任一已配置大小球线的场次为 0。历史的球队新闻和首发缺乏可验证的逐场档案，当季输入处理更改的实际表现需要后续前向样本。这次发布比较用于复核，用户本机的版本决定仍需负责人签署。[五季版本对照](../reports/research/release-probability-comparison-0.3.0.json)
