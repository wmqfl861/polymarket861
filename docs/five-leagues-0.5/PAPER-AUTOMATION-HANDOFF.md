# 五大联赛模拟账户与微信交接

2026-11-27，文中时刻均为北京时间。Ubuntu 数据盘运行 `0.5.0a4`，对应发行提交 `6844caf`。部署脚本存放在 `deploy/paper-automation/`，随仓库分发。预测 wheel 与部署脚本分别更新。[发行提交](https://github.com/wmqfl861/polymarket861/commit/6844caf945491e26083c5112c3e677f5e2782e61) · [预测服务升级及回退](DEPLOY-0.5.0a4.md)

## 从扫描到微信

| 任务 | 调度 | 产物及触发条件 |
| --- | --- | --- |
| 七天推荐扫描 | 系统级 `pm861-recommendation-feed.timer`，每 10 分钟 | 匹配生产预测、外部赔率、两次公开盘口、手续费及历史校准。候选写入 `five-automation/recommendation-feed/feed.json`。每项参考金额分别取同一笔可用现金估算。 |
| 模拟账户 | 系统级 `pm861-paper-10000.timer`，每 15 分钟 | 以 10,000 模拟美元作为共享本金。逐笔按成交回执重放，核对余额、净份额和正式兑付，将打印用报表写入 `paper-10000/account.md`，并原子更新 `paper-10000/account-status.json`。完成后调用服务账户的微信任务。 |
| 微信新仓通知 | 用户级 `pm861-weixin-recommendations.service`，另有每 5 分钟检查及重试的 timer | 一个新仓位必须同时出现在账本的 `positions` 和 `decisions`，两处均为 `filled`，成交时刻、正的含费成本及净份额相符。推送球队、方向、模拟投入、成交均价、建仓和开球时间。 |

扫描与建仓是两个入口。自动扫描发现的候选供研究，只有经模拟开仓流程核验并计入账本的成交才会触发微信。报价或持仓估值变化、暂停、恢复、过期都保持静默。首装通知器把当时已有的仓位登记为起点；重启读取同一份 SQLite。新仓以账本仓位 ID 去重。Hermes 明确拒收后至少等 5 分钟；结果不明时先核对本地 Hermes 会话回执，查询不到记录且已过 30 分钟才重试。[账本与开仓工具](../../deploy/paper-automation/paper-10000/) · [候选筛选与金额口径](../../deploy/paper-automation/five-automation/recommendation-feed/README.md) · [新仓通知](../../deploy/paper-automation/five-automation/weixin-recommendations/notify.py)

## 在已有部署上同步

原部署的本地 agent 取得新源码后，先完成[首六仓原件归档与账本复核](LOCAL-AGENT-SIX-POSITIONS.md)。将现场回执记入更新总结，再按实际部署任务同步脚本。

项目根目录为 `/mnt/d/polymarket861`。下列命令从仓库根目录执行，把源码复制到已有运行布局。脚本按自身位置查找账本、历史和生产数据。要在新机器接续同一个账户，先恢复一致备份，包括 `.pm861`、`.local-deploy/paper-10000`、四国预测和盘口数据、推荐历史及通知数据库。通知器从 `notifications.sqlite3` 恢复起点及送达记录。仓库提供可重新生成的历史概率校准表。[预测部署前置条件](DEPLOY-0.5.0a4.md) · [校准表生成程序](../../deploy/paper-automation/five-automation/recommendation-feed/build_calibration.py)

```bash
cd /mnt/d/polymarket861
src=deploy/paper-automation
run=.local-deploy
paper=paper-10000
feed=five-automation/recommendation-feed
wx=five-automation/weixin-recommendations
install -d -m 0700 "$run/$paper" "$run/$feed" "$run/$wx"
install -m 0644 "$src/$paper/"*.py "$run/$paper/"
install -m 0644 "$src/$feed/"*.py "$src/$feed/calibration.json" "$run/$feed/"
install -m 0644 "$src/$wx/notify.py" "$run/$wx/"
```

系统级单元按服务账户 `ubuntu`、UID 1000 与本机 release 目录配置。用户级微信单元调用该用户已配置的 Hermes 微信会话。迁机时按实际路径、用户和 UID 核对单元，尤其是 `ExecStart`、`user@1000.service`、`XDG_RUNTIME_DIR` 和通知器内的 Hermes 可执行文件。系统服务的专用解析目录取自仓库内的英超 DNS 示例，使用它前需先安装示例的 `stub-resolv.conf`，并核对主机 `/etc/resolv.conf` 的目标。盘口和四国预测的现役覆盖配置均保存在系统单元目录。[系统级配置](../../deploy/paper-automation/systemd/system/) · [用户级配置](../../deploy/paper-automation/systemd/user/) · [解析器示例及验证工具](../../deploy/examples/epl-resolver/)

```bash
# 仍在仓库根目录，使用上一段的 src 变量。用户级命令用服务账户执行。
units="$src/systemd"
user_units="$HOME/.config/systemd/user"
install -d "$user_units"
install -m 0644 "$units/user/"pm861-weixin-recommendations.{service,timer} "$user_units/"
sudo install -m 0644 "$units/system/"pm861-{paper-10000,recommendation-feed}.{service,timer} /etc/systemd/system/
systemctl --user daemon-reload
sudo systemctl daemon-reload
systemctl --user enable --now pm861-weixin-recommendations.timer
sudo systemctl enable --now pm861-paper-10000.timer pm861-recommendation-feed.timer
```

用了旧用户级账户、扫描定时器的机器，切换到系统级同名定时器前先停用旧的两项。本机已完成切换。无登录会话时持续发送依赖服务账户的 systemd user manager，本机已启用 linger。接手时可用 `python3 .local-deploy/five-automation/weixin-recommendations/notify.py --preview` 检查预计发出的内容。手工准备初始六场的 `prepare-slate.py` 是 2026-11-27 的专项工具，依赖当天的私有研究输入和原始回执。它和 `open-portfolio.py` 均未加入定时器。恢复既有账户时直接续用备份的输入和凭证。

## 本机验收与已知限制

- **版本与数据。** 16:47 完成英超切换。新旧程序对 5 季、190 轮、1,900 场的逐场预测及训练输入零差异，所核旧封存字节一致。73 项本机专项回归通过，英超备份的 30,719 个文件通过验证，另外 12 个数据库的 SQLite 快检通过。本机原件在 `.local-deploy/upgrade-0.5.0a4/acceptance.json` 及同目录的 `epl/`、`backup/`。[发行版 CI](https://github.com/wmqfl861/polymarket861/actions/runs/45954236335)
- **实时采集。** 本机将四国盘口每页 25 条改为 100 条，并为盘口、预测、扫描和模拟账户配置服务专用 DNS。连续两轮取得四国完整目录，保留了逐页 30 秒及完整目录 600 秒的新鲜度校验。更换解析后，生产调度重新收到四国 Wikimedia 比分来源。历史上的外部失败回执及英超漏窗保留在报表中。新闻、伤停、官方首发覆盖仍由逐场证据决定。核对当下状态可查看三个 watchdog 单元的最近退出结果。[盘口验收口径](WATCH-SERVICE.md) · [人员资料口径](PLAYER-SCENARIOS-0.5.0a4.md)
- **通知验收。** 18:06 建立新仓推送起点，登记当时 6 个已有仓位。旧版一次曾发 14 条混合更新，使用该批及原持仓隔离回放，产生 0 条新仓消息。用测试账本加入一笔有效新仓产生 1 条消息，进程重启及改价均未重发。21:18 查生产送达库，自起点以来新增模拟仓位 0 个、发送尝试 0 次。新规则的首笔线上新仓送达仍待实际建仓验证。接手者可运行仓库的离线通知测试，单独检查基线、成交对账和重试。[离线回归](../../tests/test_paper_opening_notifications.py)
- **发布核对。** 已逐字节核对 9 个运行脚本、校准表和 9 个在役 systemd 单元或覆盖文件。4 项通知回归使用临时账本和发送替身，覆盖旧仓静默、成交不匹配、去重、失败重试及延迟回执。隔离目录复验了账户监控、扫描器的校准依据和通知命令入口。在仓库根目录执行 `python -m unittest discover -s tests -p test_paper_opening_notifications.py -v` 可重跑通知回归。

后续查账优先看 `account-status.json` 的 `as_of`、`positions` 及 `decisions`。查候选缺项看 `feed.json` 的 `checked_at`、`coverage.gaps` 及每条记录的 `valid_until`。若账本确有新仓而微信未到，先看服务账户的 `journalctl --user -u pm861-weixin-recommendations.service` 和通知数据库中的 `attempts.status`。Hermes 返回 `ret=-2 session` 时，用户可向原机器人发一条消息恢复会话。账本、聊天正文、接收者信息、密钥及备份归档保留在部署机。

## 11 月 28 日英超盘口恢复

当天有两种独立故障。旧进程积累了 47 条 `CLOSE-WAIT` 连接，目录和订单簿请求持续超时；在同一服务解析环境中新进程能读到真实订单簿。重启英超后，14:20 四场常规盘口各收 48 份，共 192 份，其中 136 份有双边报价。14:30 的九个盘口窗口又全部漏采，当窗主调度在等待 SQLite 写锁，九场均未发出 CLOB 请求。14:35 四场盘口任务明确返回 `database is locked`。日志、线程栈、WAL 锁采样和逐场回执留存部署机。

周目录及历史目录都曾把一整页比赛的合约处理放在主库写事务中。修复把目录页的解析、归一化及合约准备移入连接私有的临时表，再在一次事务中发布全页赛事、状态及合约回执。这样可以保留原始收件时刻、重复收件的行号及整页回滚。调度器也为在途服务续租，避免一次预测或赛果任务运行超过五分钟后重复占用工人和数据库。已完成的任务仍按自身间隔排下一次。[目录归档](../../src/polymarket861/market_archive.py) · [赛季调度](../../src/polymarket861/season.py) · [目录回归](../../tests/test_market_archive.py) · [租约回归](../../tests/test_season_operations.py)

后台恢复检查每两分钟读取请求账本。英超进程须已运行 30 分钟，过去 15 分钟 Gamma 和 CLOB 都发生总时限超时，首个超时距今至少 10 分钟，最近 3 分钟仍有超时，期间没有成功或上游 HTTP 回应，也没有生效中的限流冷却。再用该服务的解析器从新进程读取 Gamma 目录和近期真实 token 的 CLOB 订单簿，复核 PID 和账本后才请求 systemd 重启。写锁、过窗、单端点超时由原调度如实记入缺口。`Result=exec-condition` 配合条件退出码 1 表示本轮跳过重启。[恢复检查](../../deploy/paper-automation/epl_http_recovery.py) · [回归用例](../../tests/test_epl_http_recovery.py)

在本机从仓库根目录安装或更新这个检查。探针使用单元内列明的独立足球解释器和[英超解析配置](../../deploy/examples/epl-resolver/)，迁机时先核实这两个路径。

```bash
src=deploy/paper-automation
sudo install -d -m 0755 /usr/local/lib/polymarket861
sudo install -m 0644 "$src/epl_http_recovery.py" /usr/local/lib/polymarket861/
sudo install -m 0644 "$src/systemd/system/"pm861-epl-http-recovery.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pm861-epl-http-recovery.timer
systemctl list-timers pm861-epl-http-recovery.timer
journalctl -u pm861-epl-http-recovery.service --since today
```

英超调度和看门狗的 `95-epl-collection.conf` 指向独立修复安装。切换前按本机实际安装核对新旧 B1 配方、现有封存和选单实验。配方摘要变化时依照五季对照和具名批准流程办理；绑定了原物理实现的选单实验仍需原安装复核。回退时保留新增的原件与账本，若新配方已经生效，先完成同库反向比较与签核，再撤除这两份覆盖文件、`daemon-reload` 并重启英超。单独停用自动重启用 `sudo systemctl disable --now pm861-epl-http-recovery.timer`。[英超安装与反向签核](DEPLOY-0.5.0a4.md) · [本机系统单元](../../deploy/paper-automation/systemd/system/)

核对窗口时在仓库根目录只读打开 `.pm861/runtime.sqlite3`，从 `season_slots` 按 UTC `target`、`stage`、`role`、`fixture` 看 `state` 和 `detail`。针对所选场次，将 `ma_requests` 中 `operation='books'`、`status='ok'` 的 `sha` 和 `received_at` 与 `ma_books` 对齐，核实在截止前实收的 token 和其中有双边报价的数量。`done`、抓到单边报价和有可执行双边报价是三种不同的统计。盘口采样本身不会触发微信。

### 本机切换回执

11 月 28 日 15:48，部署机对修复提交 `ec6a499` 完成具名批准。英超采集器与看门狗使用已核验的独立安装。五季 190 轮共 1,900 场逐场配对，正式概率与训练视图的差异均为 0。本机比较报告为 `3b9b32b750f29fd230d41e8360779d5c4fe4e8a697ceab91d516d61ecf9ac43b`，可用新安装的 `pm861 --data-dir .pm861 forecast release-report` 加上该摘要复核。上线后核对到生效配方 `54cd9cd6…` 与安装配方相同，原有 10 份封存的内容与摘要全部一致。14:45 的模拟账务复核覆盖首六仓及一笔兑付，两套引擎的账户和逐仓差异均为空。[修复提交](https://github.com/wmqfl861/polymarket861/commit/ec6a499676e50371b9c61ae8cfb3a763ec47e153)

切换前取 14:51:50 的 WAL 一致快照。34,619 份原件、53 个便携文件和 SQLite 数据库共 34,673 项，全部哈希通过。SQLite 完整性检查通过，外键违规为 0。压缩库完整解压后的 5,346,607,104 字节与快照哈希一致。部署机在 `.local-deploy/backups/epl-before-collector-update-20261128T065149Z-q7k0spdg/` 留存压缩库、清单、两份回执和 `RESTORE.md`。核验采用独立副本的 SQLite 检查和完整解压流哈希。历史漏窗按当时记录保留。

15:50 的常规采样按时完成 4/4 场，192 份盘口中有 136 份双边报价。16:00 的 7 场均漏采，窗口内 CLOB 的 17 次批量请求和 6 次单项请求超时；同期 Gamma 的 7 次请求成功。另在历史回补按代币、日期逐项排队时测到 10.195 秒的写锁。后续补丁把回补任务准备放入连接私有的临时表，成批提交新增任务及对应进度。用含 14 个赛事条目的真实原件回放 6,236 个任务，重复回补的主库写事务从 1,002 毫秒降至 86 毫秒，首次生成从 1,103 毫秒降至 102 毫秒，任务、顺序、重试状态和进度逐项一致。普通定时盘口的批量请求每次最多用 12 秒，原采样截止时间仍约束每次请求。到期后尚未发出的逐代币请求只在该窗口登记缺口。[回补代码](../../src/polymarket861/season_backfill.py) · [回补并发回归](../../tests/test_season_backfill_concurrency.py) · [盘口采样回归](../../tests/test_market_archive.py)

15:48 就绪报告中有 8 场正在等待同一份 Gamma 目录的赛事索引。16:02 只读复核了第 12 轮 9 场未赛对阵的原有双源凭据、最新 Gamma 原件和 OpenLigaDB，9 场的主客、身份和开球时刻均吻合。埃弗顿对利物浦在北京时间 11 月 29 日 20:00 开球，T−24h 窗口为 11 月 28 日 19:55–20:00。后续须用 `run status` 的实际封存摘要查验该窗口。两家比分源对较早的伯恩茅斯对布伦特福德仍有冲突，该条比分保留隔离。

### 16:27 盘口补丁上线后的逐窗实收

北京时间 11 月 28 日 16:27:18，英超服务启动后使用 `0.5.0a4-epl-20261128.2`，采集器与看门狗均指向 `fbb12d5` 的独立安装。部署前逐项核对了 122 份源码及资产、131 项安装哈希、10 份旧预测封存和已签核 B1。首六仓的 215 项原件通过复核，按 16:00:44 账本重放两套引擎，6 仓、1 笔兑付的账户与逐仓差异均为零。切换脚本首次在 systemd 刚声明启动时读取进程路径，过早回退。改为等待实际进程执行后，复核新安装路径并完成切换。[代码](https://github.com/wmqfl861/polymarket861/commit/fbb12d5a7ce5f932a3badc766cce0858e2e3d9d4) · [完整回归](https://github.com/wmqfl861/polymarket861/actions/runs/46124397584)

下表按常规定时盘口的原截止时间统计，同一场在不同时刻分别计数。每份代表一个合约代币在当窗及时收到的订单簿，双边表示同时有买价和卖价。16:53 复核时，四轮合计 20/22 个场次及时完成，收到 960 份，其中 712 份双边。

| 北京时间 | 按时完成 / 应采场次 | 及时订单簿 | 其中双边 |
| --- | ---: | ---: | ---: |
| 16:30 | 7 / 9 | 336 | 246 |
| 16:35 | 4 / 4 | 192 | 136 |
| 16:45 | 5 / 5 | 240 | 194 |
| 16:50 | 4 / 4 | 192 | 136 |

16:30 曼城对利兹联、曼联对布伦特福德的批量任务直到 16:30:51 才进入请求步骤，已过 16:30:45 截止时间，两场均保留缺口。曼联那场在 16:45 的下一次采样收到 48 份，其中 42 份双边。启动时另有 19 场恢复采样，12 场按时收齐、7 场过窗。16:50 的四场常规盘口都完成，同轮布莱顿对纽卡斯尔的赛事状态更新遇到 SQLite 写锁，桑德兰对热刺的状态更新过窗。16:47 的正式预测巡检通过，待核赛程清单为空。历史窗口、赛事状态与当轮盘口分别以各自账本为准。

### 单场合约归档与盘口查询

16:27 的版本在 17:00 按时采到 4/7 场。伊普斯维奇对阿斯顿维拉、曼联对布伦特福德遇到数据库写锁，热刺对富勒姆过窗。17:15 和 17:30 均采到 6/9 场。旧版在持有主库写锁时处理单场赛事的整组合约。读取指定场次的最新合约时，旧索引只能按赛事代号扫描历史收件，再逐条筛选收件时刻与原件摘要。

提交 `83259fe` 让单场赛事复用目录归档的临时表，将准备好的回执一次提交，并给 `ma_contract_receipts(event_slug,received_at,sha)` 建联合索引。布莱顿对纽卡斯尔的真实 55 合约回包在隔离库首次写入时，主库写阶段从 276 毫秒降到 5 毫秒，重复收件从 204 毫秒降到 1 毫秒。五张表的数据和行号逐项相同。首次 GitHub 回归发现概率配方的审核清单仍列着旧的英超入库调用。提交 `6e303b5` 移除这条过时记录，三个 Python 版本、跨版本配方一致性及 systemd 部署检查均通过。这两个提交的安装包输入完全相同。[代码](https://github.com/wmqfl861/polymarket861/commit/83259fe3940d642fa9f1f3780060185c9f47a69c) · [审核清单修正](https://github.com/wmqfl861/polymarket861/commit/6e303b5746657849e135ff4b8db8147f649e2128) · [通过的完整回归](https://github.com/wmqfl861/polymarket861/actions/runs/46130531620)

北京时间 17:41:19，负责人在同一生产库批准比较报告 `cf4403a19af98ec8e898d0b59a90d18bf95697a1b5bf90aed21b60d18daa9ec7`。五季 190 轮的 1,900 场均已配对，每场预测和训练视图零差异。17:41:22 起采集器运行独立安装 `0.5.0a4-epl-20261128.3`，17:42 看门狗通过。已核实安装配方与生效 B1 都是 `18a8cf6c…`，原 10 份封存、4 次既有批准及其报告均保留。17:15:34 的模拟账表覆盖 6 仓和 1 笔兑付，两套引擎的账户与逐仓差异均为空，215 项归档输入通过校验。切换安排在 17:35 的 4 场盘口收齐后。旧进程等待研究线程退出，用满 systemd 的 5 分钟停止时限；该时段没有到期的常规定时盘口。此前 34,673 项一致快照已通过 SQLite 校验及完整解压流哈希，现场未另做整机恢复演练。

在生产库用相同的收件截止、相同的 SQL 对这 9 场读取两次。逐场合约计数、返回顺序和内容哈希全部一致，共 522 条。九场累计查询耗时从 5.566 秒降至 0.299 秒。新查询计划命中 `ma_contract_event_receipt (event_slug=? AND received_at=? AND sha=?)`。实际窗口另按截止前收到的订单簿计数。

截至 18:06，四个常规定时窗口按期收到 20/20 场次的盘口，共 960 份，其中 768 份有双边报价。17:45 利兹联对考文垂、曼联对布伦特福德的首次批量请求超时，两场分别在 17:45:31 和 17:45:30 收到重试回包，均早于 17:45:45 截止。同期赛事状态完成 16/38 项，其余 22 项过窗。18:04 正式预测巡检通过。18:05:36 的数据库写锁告警来自历史回补。17:41 启动恢复时另外安排 19 场，7 场按时收到，其余 12 场过窗。

| 北京时间 | 按时收到盘口 / 应采 | 及时订单簿 | 其中双边 | 状态任务完成 / 应查 | 主赛事与附加盘口状态均按时收到 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 17:45 | 5 / 5 | 240 | 214 | 5 / 15 | 3 |
| 17:50 | 4 / 4 | 192 | 138 | 4 / 4 | 3 |
| 18:00 | 7 / 7 | 336 | 278 | 3 / 15 | 3 |
| 18:05 | 4 / 4 | 192 | 138 | 4 / 4 | 3 |
| 18:15 | 5 / 9 | 240 | 170 | 3 / 15 | 3 |
| 18:20 | 4 / 4 | 192 | 138 | 4 / 4 | 3 |
| 18:30 | 9 / 9 | 432 | 340 | 15 / 15 | 15 |
| 18:35 | 4 / 4 | 192 | 138 | 4 / 4 | 3 |
| 18:45 | 5 / 5 | 240 | 214 | 3 / 15 | 3 |
| **合计** | **47 / 51** | **2,256** | **1,768** | **45 / 91** | **39** |

18:10 核对英超新闻库，本赛季已有 188 条球队报道，覆盖 20 家俱乐部。信息提取表保留 146 条球员相关记录，涉及 110 个球员 ID。最近一条新报道入库于当天 09:50。18:04 新收到球队名册和新闻源轮询回执，纽卡斯尔的传闻和维基新闻检查成功，当轮球员可用性来源记录为部分覆盖。新闻组件上一次完整任务结果仍显示 14:31 的写锁错误，逐源收件已推进到 18:04。逐场核对伤停或首发时，按对应比赛的来源、发布时间、收件时间和预测截止时间判断。

18:15 的九场中有四场错过 45 秒截止。阿森纳对曼城在 18:15:25.970 发出首次批量请求，两次请求均超时。利兹联对考文垂、曼联对布伦特福德到截止后才进入请求步骤，纽卡斯尔对曼联在窗内未获调度。该轮第一笔订单簿请求在开窗后 23.765 秒才发出。18:30 同一安装、同样六个盘口工人处理九场，开窗 0.947 秒即发出第一笔请求，九场均首次请求成功，最后一份回包在 6.8 秒内收到。

表中按每场每窗计数，17:41 的启动恢复另计。对赛事状态，调度器标记完成 45 项。逐笔核对 Gamma 回执，39 项在各自 45 秒的窗口内同时收到了主赛事及附加盘口的状态。18:15:38 的模拟账本复核覆盖六仓、一笔兑付，两套引擎与账本逐仓一致。

18:22 正式预测预检核对九场赛程，布莱顿对纽卡斯尔的最新 Gamma 原件一度无法确认唯一赛前赛事。18:39 完成的下一次预检再次核对九场，阻塞清单为空，已签核的 B1 配方就绪。埃弗顿对利物浦的 T−24h 封存窗口仍为当天 19:55–20:00，实际封存须查该窗口的 `run status` 及摘要。[固定窗口验收](DEPLOY-0.5.0a4.md)

## 同机 NBA 后台与微信

NBA 使用独立服务账户 `pm861-nba`。11 月 28 日默认 DNS 导致公开 Gamma 目录连续失败，专用账户的用户级服务也无法创建解析器挂载命名空间。现场仅把每分钟的 `markets` 定时器迁到系统管理器，由原 NBA 账户运行，保留原解释器、数据目录和命令 `nba run --component markets`。该公开接口无需供应商密钥。赛程、预测、伤停和综合看门狗四项仍由 NBA 账户的用户管理器调度。14:36 首次重新收齐两页目录、15 场，随后综合看门狗恢复正常。[本机 NBA 行情单元](../../deploy/examples/nba-markets-system/) · [NBA 部署及伤停验收](https://github.com/wmqfl861/polymarket861/blob/cc48470e5edd65c5d6df7c04fad120ad77d3245f/docs/nba-plan/OPERATIONS-0.6.0a2.md)

迁移或回退 NBA 行情时先停原层级的 `polymarket861-nba-markets.timer`，等该层级的同名 service 完成，再启用目标层级的 timer，避免两个实例并跑。系统级单元在 `/etc/systemd/system`，用户级原件在 NBA 账户的 `~/.config/systemd/user`。两处均以各自的 `systemctl list-timers` 和 NBA 库的 `nba_market_requests`、`nba_market_runs` 验证。伤停验收以真实赛前窗口的认证回执为准。

持有人指定的首场 NBA 伤停汇总由本机 Hermes 单次任务发送，在最后一个窗口结束后统计 Big Balls 三窗和 DunkAPI 一窗的实收。遇到空窗，报告已有失败原因或标明原因尚未证实。接受的发送会停用该任务。日常微信继续只发模拟账户实际新建仓位；单次任务的送达状态、会话与接收方配置保存在本机。

首场赛程为北京时间 11 月 29 日 06:00 魔术客场对奇才。Big Balls 三个采集窗口为 04:25–04:30、04:55–05:00、05:40–05:45，DunkAPI 使用 04:55–05:00。查看 `ubuntu` 用户的 `systemctl --user list-timers pm861-nba-first-injury-weixin.timer` 可核对 05:47 的首次发送安排。脚本按实际赛程判断最后一窗是否结束，在本机 `.local-deploy/nba-first-injury-weixin/delivery.json` 记录 Hermes 是否接受消息。11 月 28 日 15:27 实查 BALLDONTLIE 已认证接收 63 页赛程，15:22 的公开盘口目录收到 15 场，两路伤停的首场窗口当时尚未开放。

16:48 复核，BALLDONTLIE 累计实收 83 页认证赛程，16:38 的公开目录覆盖 15 场。NBA 看门狗确认赛程、盘口和预测均正常。Big Balls 与 DunkAPI 首场伤停各为 0 份，首次赛前窗口在次日 04:25 开始。Hermes 网关运行中，单次微信定时器已启用，正在等待次日 05:47。

18:06 再查，盘口恢复后已有 210 个成功采集周期，17:55 的完整两页目录涵盖 15 场。BALLDONTLIE 累计收到 103 页认证赛程，18:06 的综合看门狗通过。两路伤停仍各为 0 份。一次汇总的 Hermes 定时器已启用，首次下发安排在北京时间 11 月 29 日 05:47。

18:20 核实盘口恢复后连续 223 个周期成功，BALLDONTLIE 累计实收 107 页认证赛程。18:38 复核时两项分别增至 241 个周期、115 页。期间 16 次完整目录刷新每次均覆盖 15 场，最新一次于 18:26 完成。18:38 的看门狗确认赛程、盘口和预测均正常。距首个伤停窗口尚有数小时，Big Balls 和 DunkAPI 当天各有 0 份认证伤停回执。Hermes 已在运行，一次性定时器正在等待 05:47 首次发送。
