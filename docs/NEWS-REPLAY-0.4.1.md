# 0.4.1 新闻概率复算与真实俱乐部英语评测

## 每场两组概率怎样复查

每条事件修订保留比赛、开球时刻、新闻截止、收件摘要和配置线。每组的 `rates` 给出主队进球强度 `lambda_home`、客队进球强度 `lambda_away` 及 Dixon–Coles 低比分修正参数 `rho`。`probabilities` 给出胜平负、全场大 2.5 和两队各自大 1.5。`totals` 对每条配置线分别给出全场、主队、客队的大球和小球概率。四条半球线时每组可比 30 项，其中三个主要大小球概率也出现在逐线表中。

复算脚本用 Poisson 递推生成主客进球分布，并修正 0–0、0–1、1–0、1–1 四格。对两队中进球率较高的一方，取使右尾概率不超过 `1e-12 / 4` 的最小上限（至少 3 球），保留从零到该上限的整个方阵并重新归一化。每组公开方阵大小、两侧尾概率、两侧之和的联合尾部上界和实际截去的联合质量。脚本分别累计大、小两侧，逐项容差为 `1e-12`。经验分布 B0 回退的概率保留，摘要单列缺少参数的组数。

### 0.4.1 发布前实采

2026 年 11 月 25 日 15:15:29–15:16:07 UTC 的第 12 轮快照包含 10 场不同比赛，共 20 组原始进球率和概率向量。10 场均有概率变化，合计采用 81 条球员结论。独立求和核对了 600 个保存值，最大绝对差 `3.19e-16`；比分格从 17 × 17 到 20 × 20，联合尾部上界最大为 `2.77e-13`。森林对切尔西登记的开球为 11 月 27 日 20:00 UTC，主胜概率从 37.85% 调整到 38.63%。[逐场原率、ρ、完整概率与收件摘要](../reports/news-event-probabilities-0.4.1.json)

这些记录在开发期间先生成 2 场、再生成 8 场，分别使用配方指纹 `d9d8a4bc…` 和 `5ac04268…`。十场同用新闻策略 `f72a095b…` 和历史清单 `fc66fe37…`。发布件按原样保留两个配方标识，供复查每次事件当时的输出。赛果、固定决策窗配对表现和正式部署的批准状态另行核算。

在保存事件的运行库上生成公开报告，命令以只读方式校验原记录的内容摘要，导出原记录中的率、ρ 和全部概率。随后的 `verify` 和旧版重建只需 Python 标准库。

```sh
PYTHONPATH=src python scripts/replay_news_probabilities.py export "$RUNTIME" \
  --season 2026-27 --round 12 --pre-release --as-of 2026-11-25T15:16:07.003785Z \
  --output /tmp/news-event-probabilities.json
python scripts/replay_news_probabilities.py verify reports/news-event-probabilities-0.4.1.json
cmp /tmp/news-event-probabilities.json reports/news-event-probabilities-0.4.1.json
```

### 最终源码的单配方提前封存

同一批观察在最终源码上通过正常主 B1 入口生成 10 场 `technical/early` 封存。新闻输入截止在 2026 年 11 月 25 日 15:53:14.289550–15:53:28.308834 UTC，实际封存时刻在 15:53:14.913578–15:53:28.645446 UTC。10 场共用主配方 `97db0393…`、新闻策略 `b973656b…` 和历史清单 `fc66fe37…`。10 场均有概率变化，20 组的 600 个原始保存值与独立复算的最大绝对差为 `3.19e-16`。这些技术预览与前述事件各有自己的分母。固定的 T−24h 和 T−60m 窗口及赛后成绩另行统计。[10 场最终配方的逐组进球率和全部方向](../reports/news-early-seals-0.4.1.json)

导出时逐条验证原封存摘要和数据库索引，核对主预测与附带无新闻组的率、胜平负和全部逐线值，并核对两组关系中的历史训练摘要、冻结的球员事实与输入截止。使用原配方摘要和最后一条封存时刻，可以在之后仍继续收件的库中重建同一批提前预览。

```sh
PYTHONPATH=src python scripts/replay_news_probabilities.py export "$FINAL_RUNTIME" \
  --early-seals --season 2026-27 --round 12 --pre-release \
  --recipe 97db0393e9b6df7ade73b4ac3cd529da38ba5e1fb9a77897e3c31aa2cdbce176 \
  --as-of 2026-11-25T15:53:28.645446Z --output /tmp/news-early-seals.json
python scripts/replay_news_probabilities.py verify reports/news-early-seals-0.4.1.json
cmp /tmp/news-early-seals.json reports/news-early-seals-0.4.1.json
```

### 0.4.0 公开记录的边界

| 材料 | 公开原始值 | 现在可重做的检查 |
|---|---:|---|
| 十场不定时事件 | 每场每组 6 项，共 120 项 | 从每组的两项球队大 1.5 反解主客进球率，从平局反解 ρ，再核对六项。新生成的 480 个逐线方向与 120 个原值分别计数。 |
| 森林对切尔西的单独提前封存 | 两组各 30 项，共 60 项 | 两组的全部原值均可与反解后的独立比分格对照。该记录的截止和收件摘要与同场的不定时事件不同。 |

旧运行库的原始进球率及十场事件的原始逐线向量未取得。十场的注册开球时刻均为占位值。补充件保留这些时刻和旧报告摘要；其新生成数字无法恢复当时审核者实际逐项比较的原始记录。十场六个公开市场的最大复算差为 `2.78e-16`，提前封存全部公开值为 `2.50e-16`。[旧实采报告](../reports/news-live-0.4.0.json) · [标明反解来源的补充件](../reports/news-probability-reconstruction-0.4.0-supplement.json)

```sh
python scripts/replay_news_probabilities.py reconstruct --output /tmp/news-040-inferred.json
python scripts/replay_news_probabilities.py verify /tmp/news-040-inferred.json
cmp /tmp/news-040-inferred.json reports/news-probability-reconstruction-0.4.0-supplement.json
```

## 三组语言指标各自衡量什么

| 材料与分母 | 实测 | 适用范围 |
|---|---|---|
| 开发者编写的英语留出集，36 段、27 条标注 | 提取 23 条，20 条四字段全对。精确率 86.96%，召回率 74.07%。 | 虚构名单和作者写作风格；初版训练和修正规则时留出。单列的开发回归集曾参与规则调整。 |
| 0.4.0 的真实结构化来源，135 行 | 唯一完整姓名对上名单 122 行（90.37%），这 122 行全部与适配器生成模板的字段相符。其余 13 行均弃权。 | 从原报告逐行重数，衡量姓名关联和同源字段一致性。两份原始快照的采集时间见旧报告。 |
| 32 篇真实俱乐部文章各取一段，16 家俱乐部，31 条标注 | 提取 16 条，11 条四字段全对。精确率 68.75%，召回率 35.48%；9 个阴性段落全部弃权，18/32 段完全正确，16/16 个引文位置可复查。 | 给定一份短候选名单，对照逐段标注评估正文。抽样跨文章体裁，缺少对生产 RSS 内容分布的随机抽样。 |

四个字段为球员、事件类型、证据状态、能否出场。每条多余输出计入精确率分母，每条漏提事实计入召回率分母。官方正文跨 2024 年 1 月至 2026 年 4 月，包括俱乐部的伤情公告、赛前采访、名单更新及赛报。31 条阳性标注含 11 条一般出场状态、9 条恢复、8 条伤病及 3 条停赛，转会传闻和否认的阳性标注均为零。

第 2 段漏掉了并列缺阵的第二名球员，第 4 段简式公告中的疾病缺阵被漏掉，第 7 段的两名球员身份和不确定性均对，事件被归为一般出场状态，标注为可能复出。这些误差保留在逐段报告中。

每段是单一公开文章的连续短摘录，保留原文、原网址、发布日期、摘录形式和编辑标注。Codex 在运行提取器前拟定并固定此次参照标注；尚无独立人工复标或标注者一致性测量。测试只输入摘录和名单，范围不含全文语境、跨段指代、完整当季名单、赛果或发布当时的收件时效。俱乐部网站用于引用与评测；该语料不会登记新的生产来源。[32 条来源与标注](../reports/research/club-prose-0.4.1.json) · [逐项输出、引文位置及混淆表](../reports/news-language-0.4.1.json) · [0.4.0 的结构化快照与逐行决定](../reports/news-language-0.4.0.json)

```sh
PYTHONPATH=src python -m polymarket861.news_ai \
  --club-prose reports/research/club-prose-0.4.1.json \
  --published-wire reports/news-language-0.4.0.json > /tmp/news-language-041.json
cmp /tmp/news-language-041.json reports/news-language-0.4.1.json
```
