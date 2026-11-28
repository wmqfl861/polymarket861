# 五大联赛模拟账户与微信交接

2026-11-27，文中时刻均为北京时间。Ubuntu 数据盘运行 `0.5.0a4`，对应发行提交 `6844caf`。部署脚本存放在 `deploy/paper-automation/`，随仓库分发。预测 wheel 与部署脚本分别更新。[发行提交](https://github.com/wmqfl861/polymarket861/commit/6844caf945491e26083c5112c3e677f5e2782e61) · [预测服务升级及回退](DEPLOY-0.5.0a4.md)

本次复审更新了仓库分发的脚本。新推荐依赖经审核的免费外部赔率授权；登记为空时，扫描只发布缺口，账户仍依据原始回执重放。[复审回应与证据归档](REVIEW-0.5.0a4-FOLLOWUP.md)

## 从扫描到微信

| 任务 | 调度 | 产物及触发条件 |
| --- | --- | --- |
| 七天推荐扫描 | 系统级 `pm861-recommendation-feed.timer`，每 10 分钟 | 匹配生产预测、外部赔率、两次公开盘口、手续费及历史校准。候选写入 `five-automation/recommendation-feed/feed.json`。每项参考金额分别取同一笔可用现金估算。 |
| 模拟账户 | 系统级 `pm861-paper-10000.timer`，每 15 分钟 | 以 10,000 模拟美元作为共享本金。逐笔按成交回执重放，核对余额、净份额和正式兑付，将打印用报表写入 `paper-10000/account.md`，并原子更新 `paper-10000/account-status.json`。完成后调用服务账户的微信任务。 |
| 微信新仓通知 | 用户级 `pm861-weixin-recommendations.service`，另有每 5 分钟检查及重试的 timer | 一个新仓位必须同时出现在账本的 `positions` 和 `decisions`，两处均为 `filled`，成交时刻、正的含费成本及净份额相符。推送球队、方向、模拟投入、成交均价、建仓和开球时间。 |

扫描与建仓是两个入口。自动扫描发现的候选供研究，只有经模拟开仓流程核验并计入账本的成交才会触发微信。报价或持仓估值变化、暂停、恢复、过期都保持静默。首装通知器把当时已有的仓位登记为起点；重启读取同一份 SQLite。新仓以账本仓位 ID 去重。Hermes 明确拒收后至少等 5 分钟；结果不明时先核对本地 Hermes 会话回执，查询不到记录且已过 30 分钟才重试。[账本与开仓工具](../../deploy/paper-automation/paper-10000/) · [候选筛选与金额口径](../../deploy/paper-automation/five-automation/recommendation-feed/README.md) · [新仓通知](../../deploy/paper-automation/five-automation/weixin-recommendations/notify.py)

## 在已有部署上同步

覆盖现役脚本前，先在部署机保全六个最初仓位的研究原件，并核对旧引擎、候选引擎与已发布账表。以下同步命令用于预检返回 `match` 的部署。[原件归档和只读预检命令](FOOTBALL-PAPER-REVIEW-20261128.md)

项目根目录为 `/mnt/d/polymarket861`。下列命令从仓库根目录执行，把源码复制到已有运行布局。脚本按自身位置查找账本、历史和生产数据。要在新机器接续同一个账户，先恢复一致备份，包括 `.pm861`、`.local-deploy/paper-10000`、四国预测和盘口数据、推荐历史及通知数据库。通知器从 `notifications.sqlite3` 恢复起点及送达记录。记录首次六场选单依据时，还需归档部署机的专项研究目录。仓库提供历史分箱研究和当前推荐层连续校正。[预测部署前置条件](DEPLOY-0.5.0a4.md) · [校准表生成程序](../../deploy/paper-automation/five-automation/recommendation-feed/build_calibration.py)

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
