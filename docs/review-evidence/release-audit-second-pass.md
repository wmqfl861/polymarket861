# polymarket861 英超 0.1.0 第二轮独立发布审查

**签署结论为 BLOCKED。** 唯一受审 Git 树 `b70683a688bb20763b59e06f8169f7ad993bf2e6`，共 78 个受跟踪文件。2026-11-25 由未参加开发的独立 Codex 发布审查代理完成，审查级别为整体工程与研究发布。首次受审树为 `483d6a5697cda6cf912a9fb8ddccf7190bf00117`，其 BLOCKED 记录保留。

五季概率研究、五场行情分档、真实目录的条款范围及本地恢复均已独立重算。七条首轮反例在原触发条件上有实质进展。进一步通过正常 CLI 发现五条仍会影响历史报告、模拟收益或资格证明的回归。当前第二树不能标记为通过。

## 环境和证据

在给定目录重建临时 Git 索引，`git write-tree` 为上述树。再从该对象独立 `git archive` 至隔离目录执行，避免宿主此前生成的字节码。使用 Python 3.12.14、NumPy 2.3.5、SciPy 1.17.0、单线程 BLAS 和本机 Chromium。真实固定输入、运营状态均在私有副本处理；测试交易、时间推进和来源授权夹具均为模拟。没有改动受审项目或上传。

| 项目 | 独立结果 |
|---|---|
| 打包、CLI 与回归 | 离线从源码构建 wheel，在新虚拟环境安装，清除 PYTHONPATH 后 `pm861 --version` 返回 0.1.0，帮助可用。`unittest` 跑 125 项，含真实 Chromium/Playwright 看板，23.955 秒，退出 0，零跳过。 |
| 五季概率 | 核对 17 个固定赛季输入。24 小时赛果延迟假设和严格无同期凭据两份报告均与冻结 JSON 逐字段相等。T−24h 1,900/1,900，模型 75.44982；T−60m 1,900/1,900，模型 75.44781，B0 为 73.55917，配对 +1.88864，按轮 95% 区间 [1.53926, 2.26230]。严格模式各 0/1,900。 |
| 正常轮报 | 2025/26 第 1 轮 T−60m 为 10/10，78.3732757142 分。同冻结报告相比，评分、四项损失、命中、校准、覆盖均一致；新库与旧库首次发布时刻、版次、赛果实际接收时间及相应封存指纹会不同。回归另覆盖延期、补记和独立轮报版本。 |
| 五场私有行情原件 | 运行市场试验的历史归档入口，预期 123 行、挂牌 116、A=0、B=12、C=18、其余 93。成交 0，历史收益率为空。与交接报告一致。 |
| 赛季目录与规则 | 2025/26 枚举 380 场，胜平负 380、全场大小 297、单队大小各 0。2026/27 当时 129 场均有四种挂牌。直接从原始 Gamma 字节重算 `contract-rule-coverage.json` 逐字段相等，2025/26 映射 2,195 个合约，1,668 个可解释规则、1,131 个明确无费。2026/27 映射 2,736 个，其中 2,451 个可解释规则且全部启用费用，另外 285 个规则待核。 |
| 恢复与运行状态 | 安装后的命令创建、校验、恢复实测运营库备份，299 个文件，重开导出 37 份报告的 HTML。源、恢复主库各 197 条原始观测、12 条封存预测，其 180 个不同归档 SHA 各自重算全部相等。该运营库 `trial readiness` 仅连续两轮建设期实测尚未达到，ready=false，已验证严格历史 T−60=0。 |

## 首轮反例与双向复查

| 编号 | 本树验证 |
|---|---|
| B1 收盘时钟 | 正常 CLI 无开球凭据时 CLV 为空。有独立测试开球证明后，伪造更早开球被退出 2 拒绝；最新簿超价差时选择前十秒有效簿，得到中点 0.495、CLV +0.035。策略从经核验同场时间和盘口取得参照。阳性使用进程内批准的虚构开放样本。 |
| B2 费用与特殊条款 | 仅给原 SHA 和自述收费方式不能解除限制。具受信测试政策时，10 份、价格 0.42 核收现金费 0.12180；保留同一原件而逐一改变资产、精度、舍入或 no_result 的四次尝试全部被拒绝，过期政策也被拒绝。实际发布的含费政策登记为空。真实官方终局与错误场景不会错误入账。 |
| B3 外部文本冒充行情 | 旧纯文本、手填审核和派生哈希反例降为 missing=1，A、成交、CLV 全零，ROI 空。另见本轮 R2 的不同入口。 |
| B4 虚构轮次启用 | 两个虚构赛季、空 Gamma 赛事、每轮十次重复同一对球队的原反例重跑。40 份技术研究预测仍可生成，两个回测门及行情、技术轮门均为 false。只剩账本自检通过，`trial activate` 退出 2。对真实固定原件加未经核实的时间资料仍有 R3。 |
| B5 模型输入配对 | 对两个相同 B0 配方登记共同协议。目标赛季相同，仅改热身文件的两场仍各可独立研究，但晋级比较均以 `different_or_unproved_inputs` 拒绝，配对为 0。已有测试另核验可重建共同档案、前向共享输入和截止时点。 |
| B6 来源切换 | 原候选状态免费名单源晋级后，实际新收到的十一人证据同时进入 trial 和 prediction。全套回归另核对其余字段、旧样本、撤权和回退日期。 |
| B7 正常 A 链路 | 在虚构但实际运行的正式决策、真实格式双簿、经核验的测试开放开球原件和逐币终局共存时，正常归档导出→历史回放可得 A=1、成交=1、结算=1。无凭据保持缺口。额外盘口与证据修订下的 R1、R4、R5 仍失败。 |

## 本轮阻断

| 编号 | 触发及实证 | 再审标准 |
|---|---|---|
| **R1 高 · 历史首簿使用错误时钟** | 15:29:55 正式封存，原始首簿为 15:29:53，名义截止 15:30:00。增加一份 15:29:58 的合格簿，真实正式成交不变，含源 SHA 的正常导出也保留原簿和封存时间，但 `market-replay` 把该行从 A=1、成交=1 降到 A=0、成交=0。历史分档及冻结重算用了名义截止前最后一簿。见 `opportunity._reconstruct`、`ledger._evidence_tier`、`ledger.freeze`。 | 根据独立从原账本验证的 decision_sealed_at 重放第一次取样；在封存后的更优或更差簿均不能改变原决策。仅写入 JSON 的时间不能作证明。 |
| **R2 高 · 真实双簿仍可为未经封存的策略成绩背书** | 对正常导出的 A 行保留同一真实格式原始簿、规则与开球凭据，只改意图 ID、模型名为 never-registered、预测摘要为全零，并自选 normal_fv。普通 `market-replay --source historical` 仍得 A=1、成交=1，并给该模型计入 CLV。供参考账户注册的历史清单与市场原件均不能证明预测或意图真实存在。见 `ledger._evidence_tier`、`ledger.replay`、`strategy.register_trial`。 | 分开证明市场可执行性和具体预测/策略可得收益。正式原单回放核验原始账户、意图摘要、模型封存、赛前公平价及名单。预登记比较的合法分配变动另绑原机会和规则。任意反事实研究不得冒充真实封存或贡献正式晋级。 |
| **R3 高 · 人工输入可成为“已验证”同期历史证据** | 三份真实固定赛季文件。只在 `--evidence` 中把真实 0∶3 的富勒姆对阿森纳填成 43∶0，另填三个时钟和 760 条赛程发布时间，无来源、无原件、无本机同期收件。一次正常 `football backtest` 后，`trial readiness` 如实读取计算报告却给出 `verified_historical_T60=760`、两项历史资格均真。真实文件身份正确，未经核验的是附加时间和赛果。见 `football._parsed_version`、`trial_evidence.verified_study`、`qualification.qualification`。 | 人工资料可用于研究；正式 verified 数目核验字段原件、赛果版本、可用时钟和确切数据范围，不能只查确认过的基础赛季文件。有真材料可通过，只有真实赛果快照而无同期凭据仍报零。 |
| **R4 高 · 同场混合 A 与 B/C 低估覆盖** | 同场 711 的对侧代币 722 另有已核原始价格曲线。只看曲线时 C=1；加入 711 的有凭据正式交易后保留相同原件和五行分母，却变为 A=1、C=0、missing=4，正确为 A=1、C=1、missing=3。原因是组装统一了联赛比赛 ID，而研究档核验又拿这个 ID 和交易所 slug 直接比较。见 `opportunity.archived_inputs`、`archive_proof.research_tier`。 | 保留并认证联赛—交易所比赛身份及条件/代币绑定。A 同场的 B/C 正常计数，其他比赛或代币移植必须被拒绝。 |
| **R5 高 · A 已失效仍报收益率** | 先把一笔真实格式有凭据 A 的持仓按官方逐币终局结算，历史 ROI=117.3913%、PnL=21.600000。随后独立开球证据冲突，同一输入清单再次走真实 `market-replay`，本次 A、成交、已结算全为 0，但输出同一非空收益率和盈利。分档为本次重新认证，收益取旧账户全账。见 `ledger.replay` 末尾与 `ledger_report`。 | 保留历史版本账本。当前仍可执行样本的投入、已实现盈亏、CLV 和 ROI 应取同一分母；全失效留空 ROI，部分失效按保留的可执行行重算。 |

## 复现命令及资料

实测用以下运行时选项，独立新增探针和退出日志在 `/tmp/pm861-independent-final-run.QC0XDloz`。各反例仅在自己的临时运行目录进行。未把测试用的开放数据或费率信任根写入发布配置。

```sh
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$PWD/src" OPENBLAS_NUM_THREADS=1
export PM861_TEST_CHROMIUM=/tmp/chromium
python -m unittest discover -s tests -v
w=$(mktemp -d)
input=/tmp/polymarket861-scorecheck.pxNsRYWc/upstream
python -m polymarket861 --data-dir "$w/s1" football backtest "$input" --from-season 2021-22 --to-season 2025-26 --horizons 1440 60 --bootstrap 2000 --seed 861 --archive-lag-hours 24 --summary-output "$w/lag.json"
python -m polymarket861 --data-dir "$w/s2" football backtest "$input" --from-season 2021-22 --to-season 2025-26 --horizons 1440 60 --bootstrap 2000 --seed 861 --summary-output "$w/strict.json"
python -m polymarket861 --data-dir "$w/round" forecast replay-round "$input" 2025-26 1 --horizon T-60m --archive-lag-hours 24
python -m polymarket861 --data-dir "$private_copy" trial study-archive --fixture epl-liv-bou-2025-08-15 --fixture epl-ars-che-2026-03-01 --fixture epl-ast-liv-2026-05-17 --fixture epl-ars-cov-2026-08-21 --fixture epl-liv-ars-2026-11-01
python scripts/market_rule_coverage.py "$private_copy/season-catalog"
```

`private_copy` 指独立复制的私有行情运行目录。上述命令及两季 `market catalog-summary`、打包、安装、备份恢复均退出 0。两份五季报告还导出了明细，24 小时假设共 3,800 行，严格口径为 0 行。复跑独立探针以 `PYTHONPATH="$PWD/src:$PWD/tests"` 调用。R1/R2 为 `clock_and_binding.py`，R3 为 `strict_sidecar.py 临时目录`，R4 为 `mixed_coverage.py`，R5 为 `evidence_invalidation.py`。它们在本树均返回 0，表示反例断言成立。原七项复查另用 `recheck_fee.py`、`retest_archive.py`、`retest_qualification.py`、`retest_pair.py`、`retest_source.py` 与正常机会阳性，各返回 0。项目内对应的正常和边界用例位于 `tests/test_{kickoff_proof,fee_registry,ledger_terms,opportunity,paired_proof,source_rollout,round_history}.py`。

## 尚待现场验证的事项

当前还缺两个连续真实完整英超轮次的双时点技术封存。发布版没有经独立核验的英超实际开球开放发行或免费 ESPN 授权，也没有已登记的当期含费政策，2026/27 已采目录因此无可执行买入。本轮含费和实际开球的工程阳性均为明确的测试原件。运动员、场外新闻、纪律和天气的录入及处理有代码和测试；真实赛前到达率、模型增益、连续本机 Codex 模型作业、前向优势与盈利仍待现场材料。本地与公开可用范围已在 README/Claude 交接注明。

**本树决定维持 BLOCKED。** 请留存本次报告。后续版本需要另报树号、重跑上述正常及失败分支，并独立签署。

