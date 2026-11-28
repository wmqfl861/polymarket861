# 足球模拟账户复审交接 · 2026-11-28

五大联赛维持免费数据原则。当前获准作赛前外部赔率比较的来源数为 0，新推荐覆盖率为 0。赛前预测、赛后评分和既有仓位的官方兑付继续按各自的证据运行。账户持有人已委托部署机的本地 agent 保全首次六场选单原件，并在覆盖现役足球脚本之前核对账本。更新时使用附带故障处理和回执要求的执行单。[本地 agent 执行单](LOCAL-AGENT-SIX-POSITIONS.md) · [Claude 审核第 7 节](../polymarket861-nba-phase1-code-review-0.6.0a1.md)

## 部署机操作

从已有部署的项目根目录、以原服务账户执行以下命令。例中使用足球监控单元的解释器和现役目录；自定义部署把它们替换为自身路径。先取得包含本交接文件的源码，再执行预检，预检通过后才复制新的足球脚本。以下是一条可以连续粘贴执行的命令。

```sh
.local-deploy/releases/0.5.0a4-6844caf/venv/bin/python -I -B \
  deploy/paper-automation/paper-10000/archive_slate_evidence.py --archive-or-verify \
  --destination .local-deploy/paper-10000/source-evidence/pm861-weekly-value-20261127 && \
.local-deploy/releases/0.5.0a4-6844caf/venv/bin/python -I -B \
  deploy/paper-automation/paper-10000/check_account_upgrade.py \
  --account-dir .local-deploy/paper-10000
```

1. 第一段把 `/tmp/pm861-weekly-value-20261127` 的原件复制到持久目录，逐文件验证 SHA-256。原件若已移走，则核验已有副本与清单；两份都缺失时命令以失败退出，请从本人备份恢复原目录。原始文件和备份清单留在部署机。
2. 第二段冻结已发布的状态、账本输入、兑付日志和两版引擎，在私有临时目录复制足球收据库、SQLite 日志和原文。它使用同一个足球解释器在副本上离线重放，以已发布账表的 `as_of` 为截止点。临时目录需要能容纳收据库和原文，空间不足可将 `TMPDIR` 指向账户目录外有足够容量的本地磁盘。预检读原账户，无须停止监控、请求盘口或发送通知。
3. 只有输出 `state: match` 和退出码 0 才通过。核对范围包括首六笔的原始 ID、之后所有已发布仓位的合约和 token、交易时刻、份额、逐档成交、费用、成本，各币款项以模拟美元计。每笔兑付使用截至该事件的日志前缀单独计算入账金额及其盈亏，另核对联赛小计、账户现金、未兑付成本和已结算盈亏。输出两份引擎和账户输入的摘要，账户数据由本人保留。
4. `mismatch` 会列出受影响的仓位 ID 和账表字段，`unverified` 会列出无法验明的证据。复制时数据更新可重跑同一命令；若缺原件、历史买盘不符或账表与日志不同步，保留旧部署和原账，先查对应的两次 CLOB 原文或双边终局回执。用缺失的数据重建历史账目会改变可核对的依据。[归档程序](../../deploy/paper-automation/paper-10000/archive_slate_evidence.py) · [预检程序](../../deploy/paper-automation/paper-10000/check_account_upgrade.py)

本次开发环境无法读取部署机的专项原件、真实账本或运营回执。已用 2026-11-27 版本（Git `60c1a2d`）的真实引擎与候选脚本交叉回放一笔离线夹具：100 份、成本 50、兑付 100、账户现金 10,050，两版均与夹具账表相同。另将一档提交买价从 0.49 改为 0.48，保留交易所原文为 0.49，旧脚本继续承认该仓，候选脚本和预检指出了持仓、现金、未兑付成本差异。同一套回归还覆盖归档重跑、来源失效、已发布方向锁、单笔兑付资料损坏及份额差异。[专项回归](../../tests/test_football_paper_review.py)

## 新候选与预测

足球推荐层 v3 按每个 token 的原始条款暂停全场大 2.5、全场小 2.5，以及“主队赢球”合约的 No 方向。其余已经支持的胜平负方向仍需具备免费赔率许可、及时的同场赔率和两次有效盘口。暂停方向仍可产生赛前预测并接受赛后验证。历史六个仓位依据各自入账时的条款重放；此前发布过的 token 在开球前仍锁定该场的首次方向。扫描器和手工开仓入口均在新候选上执行限制。生产批准的足球配方继续使用原版本。[选择规则](../../deploy/paper-automation/five-automation/recommendation-feed/README.md)

重新运行了 Claude 的足球探针，输出与随仓报告逐字节一致，SHA-256 为 `7d14ed00cc9bdb36af5995b9f29f321156f0e1ad9aa40b44d916341b37841b48`。五联赛 2025/26 的 1,745 场有 182 个联赛轮次，区间以轮次为单位重抽样 2,000 次。此季已用于拟合校正曲线。[完整分区报告](../review-evidence/code-review-0.6.0a1-nba-phase1-2026-11-28/outputs/probe-paper-calibration-after-fix.json)

| 原始模型概率区间 | 场数 | 校正后均值 | 实际频率 | 实际减校正后，95% 区间 |
| --- | ---: | ---: | ---: | --- |
| 全场小 2.5，60% 至 70% | 219 | 56.9% | 49.8% | −7.2 个百分点，−14.6 至 +0.3 |
| 全场小 2.5，50% 至 55% | 295 | 49.5% | 54.2% | +4.7 个百分点，−0.9 至 +10.2 |
| 主队不胜，20% 至 40% | 240 | 29.9% | 24.6% | −5.4 个百分点，−10.5 至 +0.05 |

全场大小 2.5 同一合约的两侧互补，因此两侧一起暂停。恢复某一方向之前，在未参与拟合的时间段冻结 T−60 的全部符合条件比赛及缺失情况，赛前固定方向、概率区间、评价窗口和恢复标准，收集正式赛果，并报告样本数、校准残差及其按联赛轮次重抽样的区间。保留新闻版及无新闻版原始封存。新验证结果进入独立复审后，再更新相应方向的候选规则。

在仓库根目录用已安装项目依赖的 Python 复现校准。下载脚本固定 OpenFootball 提交，探针逐联赛核对输入 Git blob。已有这些固定文件时可直接将 `--raw` 指向该目录。

```sh
evidence=docs/review-evidence/code-review-0.6.0a1-nba-phase1-2026-11-28
trial_dir=$(mktemp -d)
python -I docs/review-evidence/code-review-0.4.2-2026-11-26/fetch_openfootball.py "$trial_dir/history"
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python -I "$evidence/probe_paper_calibration_after_fix.py" \
  --src src --raw "$trial_dir/history" --report reports/five-leagues-0.5.0a2/baseline-study.json \
  --feed deploy/paper-automation/five-automation/recommendation-feed "$trial_dir/replay" > "$trial_dir/result.json"
cmp "$trial_dir/result.json" "$evidence/outputs/probe-paper-calibration-after-fix.json"
python -m unittest discover -s tests -p test_football_paper_review.py -v
```

## 免费外部赔率来源的登记

| 复核项 | 登记前需留存或验证的内容 |
| --- | --- |
| 许可与费用 | 保存免费条款或书面许可原文、授予方和赔率权利方、有效期、覆盖联赛、具体路径、字段和用途。确认自动请求、持久保存和项目内模拟比较均有明确范围；核对限额、署名及使用条件。 |
| 同场同盘 | 核对比赛、主客次序、开球、全场口径、盘口线、完整互斥赔率、来源时间和接收时间。赔率为同一家报价商的原始可核对记录，可按相同市场去水。 |
| 传输与证据 | 对现有 ESPN/DraftKings 适配器，只为获准的 `(league, route)` 登记书面原文 SHA-256 及有效起止，原件放入脚本同级私有 `source-permissions/<sha256>`。赛程、球队名单的许可需明确覆盖用于比较的赔率字段。 |
| 新提供方 | 新来源使用独立适配器、自己的许可与字段映射，使用留存的样例测试项目所需的盘口、去水、时效和所有失败分支。未获准、到期、改文或缺赔率时，应在对外请求前拒绝或在缺价阶段记录缺口。经复核后才能进入推荐。 |

当前 ESPN 的两条读取路径均无有效登记，扫描、手工准备和持仓复查都在发送相应请求前停下。Polymarket 盘口用作合约与可成交价格依据；独立赔率证据还有单独的来源要求。只使用已有免费数据即可继续研究，当前没有需要购买的数据或需要本人审批的来源。[现有许可登记](../../deploy/paper-automation/paper-10000/football_odds_access.py)
