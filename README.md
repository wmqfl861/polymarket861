# polymarket861

英超赛前概率预测与 Polymarket 国际站模拟账本。系统在同一份比分分布上给出胜平负、全场进球大小、主队进球大小和客队进球大小。默认在开球前 24 小时、60 分钟分别封存预测。模拟买入使用买方实际能取得的卖盘、深度和费用，持有至合约兑付。

**当前版本为 0.3.0。** 仓库包含可安装程序、中文本地看板、端到端回归测试和真实历史研究报告。首次运行会建立自己的 SQLite 数据库。建设期影子、历史重放、正式前向模拟各有独立标记。代码采用 Python 3.11 及以上版本，数据接口只读，纸面账户不需要钱包。

[系统蓝图 1.3](docs/polymarket861-EPL-v1.md) · [开发计划及验收口径](docs/polymarket861-EPL-development-plan.md) · [比赛日操作手册](docs/MATCHDAY.md) · [Claude 复审逐项处置](docs/CLAUDE-REVIEW-RESPONSE-0.3.0.md) · [本版独立终审](docs/INDEPENDENT-REVIEW-0.3.0.md)

## 用固定数据跑通历史回放

在仓库根目录建立虚拟环境并运行安装。Windows 可用 `py -3 -m venv .venv`，在 PowerShell 中用 `.\.venv\Scripts\Activate.ps1` 激活。

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
pm861 --help
```

以下命令下载一个固定版本的公共领域数据，生成五个赛季的历史报告，回放一整轮真实英超，再导出可直接打开的单文件看板。建议用 `python -m pip install -c constraints/research.txt -e .` 安装发布时的研究依赖。首次需要联网下载原始赛程。示例中的 24 小时是明确指定的赛果可得延迟假设。完整的历史报告已随仓库提供。

```sh
pm861 --data-dir .pm861 football fetch .pm861/openfootball --from-season 2010-11 --to-season 2026-27
pm861 --data-dir .pm861 football backtest .pm861/openfootball --from-season 2021-22 --to-season 2025-26 --archive-lag-hours 24
pm861 --data-dir .pm861 forecast replay-round .pm861/openfootball 2025-26 1 --archive-lag-hours 24
pm861 --data-dir .pm861 dashboard export --output .pm861/dashboard.html
```

独立测量严格历史覆盖时，去掉 `--archive-lag-hours`。如有具时间凭据的同期赛果和赛程版本，可传 `--evidence 文件.jsonl`。用 `--details 路径.jsonl` 导出逐场预测与评分。正式模型及市场验证采用蓝图中的同场比较协议。

## 日常运行

| 目标 | 正常入口和输出 |
|---|---|
| 刷新公共领域数据 | `data refresh --first 2022-23 --last 2026-27` 锁定上游提交，校验逐季文件，归档原始内容、时间和本机可用的输入清单。已有固定版本可用 `data observe 数据根目录 --register-season 2026-27`。从本机实际观察时刻开始使用历史终场记录。 |
| 更新当季赛果 | `result-feed season 2026` 拉取 OpenLigaDB 和英格兰俱乐部的 Wikimedia 赛季修订。核对身份、轮次、实际开球，并以两个提供方的终场或单源独立复收确认。用 `result-feed freshness 2026` 查看逐队缺口和下一轮影响。冲突、未完赛及来源回滚在独立记录中可查。 |
| 记录新闻、伤停和天气 | `source` 登记免费用途、字段和使用范围。`evidence` 导入球员名单、状态、牌、场外消息及气象事实，区分预计与已确认版本。`feeds` 提供 GDELT 发现与有起报时间的天气试采。每种来源按实际使用条件启用。 |
| 封存赛前预测 | `fixture list --season 2026-27` 查比赛标识。`forecast make 比赛标识 数据根目录 --horizon early` 生成带本机收件凭据的提前技术观察。标准时点用 `run due 数据根目录 --workers 2`，可由本机计划任务每分钟调用，也可加 `--repeat 60` 前台持续运行。 |
| 调度全赛季 | `season run --repeat` 协调未来赛程、盘口、预测与结算；`season backfill 2025 --max-pages 10` 按游标补采，`season status` 查看迟到与漏窗。 |
| 归档盘口并回放 | `market discover`、`market capture`、`market watch` 和 `market history` 记录主赛事、附属赛事、两侧盘口、成交与曲线。`trial study-archive --fixture 已完赛主赛事slug` 使用同库真实历史证据建立回放报告。按 `--help` 查看某个采集器的参数。 |
| 检查一笔纸面交易 | `ledger-example` 给出输入格式。`ledger-demo` 验证合成账户的费用、风险限额、成交、结算和重试。`ledger-freeze`、`ledger-evaluate`、`ledger-capture-resolution`、`ledger-settle` 处理真实证据驱动的模拟账户。正式准入后，可用 `opportunity run 赛事slug --account main --repeat 1` 从封存预测、原始条款和双时点盘口装配订单，并将可验证的历史机会送入 A 档回放。 |
| 查看轮报和版本 | `fixture result 赛果证据.json` 为该场已封存的版本更新轮报。`forecast round 赛季 轮次` 查询相应统计。`forecast round-history 赛季 轮次` 列出更正前后的轮报及赛果版本，`--edition N` 可重取一版。`dashboard serve --port 8765` 提供本机浏览入口，六个页面可分别查看比赛、数据源、轮报、错误、实验和任务。 |
| 并行维护和回滚 | `run due` 会从新轮报、错误、来源异常及每周免费源缺口生成去重的本地研究任务。可用 `maintenance tick` 单独检查，或用 `jobs enqueue` 登记带依赖任务。`jobs run --workers 2` 并行运行。允许调用已有的本机 Codex 时加 `--allow-codex`。`experiment register` 预登记候选与窗口，`experiment evaluate` 计算证据，`experiment approve 键 --reviewer 名称 --reason 理由` 具名确认模型切换，`experiment rollback` 可恢复先前版本。 |
| 比较选单策略 | `strategy register` 预登记固定预算、候选选择规则和自预登记起始轮起的连续 38 个未来官方轮次，可跨赛季。用 `strategy history` 重放可执行历史盘，`strategy freeze` 和 `strategy execute` 在纸面账户执行赛前配对。用 `strategy score` 查看按轮统计的扣费后收盘价格优势和风险。 |
| 检查正式模拟是否可以开始 | `trial readiness` 返回实测历史覆盖、连续技术轮次、历史盘口回放与账本自检。条件满足后用 `trial activate 未来UTC时刻` 登记往后生效的纸面协议。 |
| 复核安装包升级 | 已有库可运行 `forecast release-compare 历史目录` 比较五季同场输出，再按报告摘要与具名决定生效。新旧程序与输入的绑定方式见[版本及实验签核](docs/model-release-governance.md)。 |
| 查看运行容量 | `storage status` 汇总 SQLite 与压缩原件的实际字节数、逐日首次收件量和合约正文复用情况。 |
| 恢复运行状态 | `backup create 新备份目录`、`backup verify 备份目录`、`backup restore 备份目录 新运行目录`。保留数据库、归档证据、已登记输入与工作产物。 |

表格里的命令均接在 `pm861 --data-dir 运行目录` 之后。联网接口失败会记录缺口。标准评分或账本能够独立运行，本地 Codex 只用于有权限的维护和研究任务。当前运行目录、原始响应、密钥和登录状态保存在用户本机。

[研究影子、兑付与轮报](docs/MATCHDAY.md) · [两个候选的五季配对试验](docs/football-candidate-experiments.md) · [全季采集与回补实测](reports/season-collector-validation.json)

## 已经测到了什么

| 研究 | 结果 | 解释 |
|---|---|---|
| 2021/22–2025/26 英超 | 五季 1,900 场，T−60 模型平均 75.45 分（含 5 场回退到联赛基线），联赛频率基线 73.56 分，同一批数据已用于选择参数。配对提高 1.89 分，历史描述性按轮重抽样 95% 区间约 1.54 至 2.26。 | 采用 24 小时延迟假设。只用已发布的最终赛果文件、要求严格历史到达凭据时，原始五季快照在这两个决策时点的有效覆盖均为 0/1,900。 |
| Polymarket 完整赛季目录 | 2025/26 枚举 380 个主赛事。380 场有可映射的胜平负、297 场有全场大小，在该目录中没有匹配到全场主客单队大小。2026/27 采样当日枚举 129 场，均发现四种玩法。 | 挂牌、可取得报价、可执行成交分别计数。源更新时重新发现。 |
| 合约规则与费用 | 2026/27 目录中有 2,736 个已映射合约。2,451 个可解释规则的合约全部启用手续费，其余 285 个的规则尚待核对。 | 正式模拟缺少经核验的当期收费资产与舍入依据。0.2.0 的独立影子可在其余条件满足时按合约费率估算。 |
| 已完赛的五场行情 | 123 条机会记录，有逐笔成交的 B 档 12 条，只有价格曲线的 C 档 18 条，其余 93 条缺证据或未挂牌。可执行 A 档为 0。 | 行情研究的真实样本。历史模拟收益率为空。合成账本样例另列。 |

[本版赛果实收与前瞻检查](reports/result-sources-2026-11-25-0.3.0.json) · [实际市场延迟与回补](docs/EXECUTION-COLLECTION.md) · [五季新旧概率对照](reports/research/release-probability-comparison-0.3.0.json)

[五季概率报告](reports/historical-football.json) · [严格历史覆盖](reports/historical-football-strict.json) · [真实一轮成绩单](reports/round-2025-26-01.json) · [真实市场覆盖](reports/historical-market-coverage.json) · [逐季合约条款核对](reports/contract-rule-coverage.json) · [免费源与赛前采样](reports/first-run.json)

## 运行测试

```sh
python -m unittest discover -s tests -v
```

测试覆盖赛前时钟、两队盘口方向、名单撤回、冷热备份、部分成交、合约特殊兑付、并发资金检查、任务中断和轮报补记。浏览器测试在有本机 Chromium 与 Playwright 时运行。真实前向预测质量、人员及天气的增益、成交样本的扣费后表现需要以后真实比赛的记录。

### 实际开球与收盘证据

正式 CLV 使用实际开球前 30 秒内、同一合约与同一 token 的最后一份合格双边盘口。末笔价差超限时，程序向前选择窗口内合格的报价。无独立开球原件、出现相互矛盾的原件或缺少合格收盘时，CLV 和策略配对标为缺测。纸面交易仍按交易所正式兑付结算。合成演练的时间及 CLV 单独标记。

`ledger-import-open-kickoff --fixture 比赛ID --schedule-version 版本 --event 发布者比赛ID --release 已核发行标识 --timeline 比赛原件.xml --licence 许可证原件.txt` 验证公开许可和发布者原件两个指纹，从已完赛的 Opta F24 导出读取 `Game.period_1_start`。原件中的联赛、主客队、比赛实体及赛程须与封存时的版本一致。新增来源需核对发布者、实际开球字段含义、免费许可与不可变原件，在来源版本中登记两个摘要。当前发布版尚无已核的真实英超开放发行。

`ledger-capture-kickoff --fixture 比赛ID --schedule-version 版本 --event ESPN比赛ID` 可在具有经核实的免费书面授权时读取 ESPN 单场已完赛时间线。程序限定 `eng.1/summary?event=比赛ID`，只接受明确的第一半场开球事件及其绝对 `wallclock`。自动获取、比赛原件留存及私人纸面研究须在授权范围内。该连接默认关闭。录入赛程或自行声明来源许可均无法启用。取得凭据后，`ledger-close` 的输入可为 `{}`，系统自行选择收盘。对暂时无法取证的真实比赛，报告保留缺测。 [ESPN 服务条款](https://disneytermsofuse.com/english/) · [F24 事件文件字段说明，附录 A](https://pure.tue.nl/ws/files/47028874/797423-1.pdf)
