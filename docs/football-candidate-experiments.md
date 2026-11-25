# 模型与选单试验

模型和选单策略在新赛季第 1 轮首次截止前，登记同一现用基线、同一组比赛和完整 38 轮的窗口。一个窗口最多比较三个候选。每轮关闭时系统检查材料，最后一个补赛结束后评估全窗。已登记的旧 12 轮试验按原窗口、原统计方法保留，既有启用和回滚记录可继续追溯。尚未启用的旧短窗试验只供研究。

| 对象 | 评估 | 生效 |
|---|---|---|
| 比分模型 | `experiment evaluate KEY` 核实近两季完整严格配对回放、全季前向评分和模拟账户。前向至少 361 场，38 轮均有配对。 | 推荐后运行 `experiment approve KEY --reviewer NAME --reason TEXT`，重验同一份证据。 |
| 选单策略 | `strategy score KEY` 核实双方同一机会池、各自选择和资金流、入场与收盘原件。历史、前向两阶段分别合格。 | 运行 `strategy promote KEY --reviewer NAME --reason TEXT`，重验推荐、证据和同窗排名。 |
| 字段来源 | `experiment evaluate KEY` 核实免费许可、可用于预测、独立上游的字段参考、及时性、正确性和覆盖。登记 `activation_policy`。 | `auto_independent_reference` 通过时自动切换该字段。`named_review` 通过后仍须具名审批。 |

署名和理由记入本机审计记录。需要恢复前一基线时，运行 `experiment rollback KEY --reviewer NAME --reason TEXT` 或 `strategy rollback KEY --reviewer NAME --reason TEXT`。准入材料更新时须重新核验。末轮自动评估和候选研究命令生成研究记录，模型与选单的启用记录由具名审批产生。

每场先计算候选与基线之差。设第 $g$ 个官方轮有 $n_g$ 个有效配对且差值合计为 $S_g$，共 $G$ 轮、$N$ 场。总体均差为 $d=\sum_g S_g/N$，聚类标准误为 $\sqrt{G\sum_g(S_g-n_g d)^2/(G-1)}/N$。两侧区间取 $G-1$ 自由度的 Student t 临界值。模型试验同时校正候选数、历史与前向两阶段、总分与四个子项。策略校正候选数及两个阶段。聚类解释依赖不同轮的误差近似独立。普通 B1 对 B0 五季回测沿用已公布的按轮百分位区间。

## 两个联合比分候选的五季实测

以下命令读取免费的固定 OpenFootball 资料。每季 8 月 1 日冻结参数。大小球校准使用此前三个赛季的滚动预测和已到达的标签，分别缩放比分矩阵中大于和小于 2.5 球的总质量。升班马先验从此前五个完整赛季估计攻防，只在原模型以 `unseen_team` 回退时尝试。两个候选均可从联合比分分布推导胜平负、全场及两队的任意半球线。

```bash
pm861 --data-dir RUNTIME experiment football-study INPUT --from-season 2021-22 --to-season 2025-26 --output strict.json
pm861 --data-dir RUNTIME experiment football-study INPUT --from-season 2021-22 --to-season 2025-26 --archive-lag-hours 24 --output lag24.json
```

| T−60 的研究结果 | 覆盖 | 总均分差（候选减 B1） | 对两个候选校正的同时 95% 区间 |
|---|---:|---:|---:|
| 原档要求真实到达凭据 | 0 / 1,900 | 无可估计配对 | 无 |
| 24 小时假设下的大小球校准 | 1,900，改动 1,895 场 | +0.0030 | −0.0576 至 +0.0636 |
| 24 小时假设下的升班马先验 | 1,900，改动 5 场 | +0.0041 | −0.0292 至 +0.0373 |

全场大 2.5 的 Brier 从 0.243467 变为 0.243419。两个主要区间均包含零。严格档案尚缺赛前时间证据，两个候选继续用于研究。报告附每季训练量与最晚标签时间、候选参数、比赛分母、预测和源文件摘要。五季已经过先前审核，后续前向验证需另行登记。[严格历史报告](../reports/research/football-candidates-strict.json) · [24 小时假设报告](../reports/research/football-candidates-24h.json)
