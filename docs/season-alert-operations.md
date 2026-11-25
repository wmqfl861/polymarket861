# 比赛日状态与独立心跳检查

`pm861 --data-dir /absolute/path season status` 返回实际使用的绝对路径。采集、查询、看门狗和人工导入应指向同一目录。先核对 `data_dir`、`heartbeat_stale` 和 `result_freshness`，再按比赛查看 `recent_paper_slots`。数据源响应、当时的赔率与纸面账户都在本机归档。

## 单场怎么查

纸面时段明细为 `formal` 与 `shadow` 分别保存本次处理时刻 `as_of`、冻结、持有、跳过、结算和重复读到的动作笔数。正式成交 `filled` 和影子持有 `held` 均计在该账户的 `held` 内。跨时段累计持仓数以账户账本为准。`gap_reasons` 统计缺失的证据，`skip_reasons` 说明规则为何拒绝各方向，`operational_reasons` 列出需要追查的项目。

| 现象 | 对应的运行记录 |
| --- | --- |
| 已评估的各方向均缺价值或触及资金、深度限制 | 账户为 `no_value`，时段可为 `done` |
| 在本时刻没有应处理的单 | 账户为 `idle` |
| 缺预测、盘口或收盘凭证 | 账户为 `gap` 或 `partial`，告警指出账户、比赛和原因 |
| 单一账户抛错、另一账户正常 | 时段为 `partial`，失败账户的原因进告警 |
| 两个账户均抛错 | 时段为 `error`，两条独立账户告警 |
| 执行时段没有待执行冻结方向 | 时段为 `done`，`no_frozen_intents=true` |
| 已有冻结方向，但未在原五秒窗收到执行簿 | 依据及时收件计入 `gap` 或 `partial` |

T−7 天至 T−48 小时的常规盘口按相对开球整点每小时保存一次，目录检查仍按原计划。升级会将旧计划中尚未启动的额外远端盘口标为 `superseded`，明细原因为 `far_book_hourly`。T−48 小时内的常规与关键时段继续按比赛日计划执行。执行簿默认只采两个账户的已冻结方向。有额外带宽时可给 `season run` 增加 `--execution-research` 并发保存其他方向的研究行情。

## 接收告警

所有告警都会写入 SQLite 的 `season_alerts` 和标准错误。赛果源降级、逾期赛果、隔离比分、回补目录和账户故障会按原因限流。同一比赛、账户和原因要经过一小时才会再通知。关键盘口漏采保留逐时段记录。通过 `recent_alerts` 可查看最近发生的事件。

`pm861 season run --repeat --alert-command '/absolute/path/to/handler --profile epl'` 启用外部处理程序。用户自行配置目的地和凭证。每次启动程序时，标准输入提供一行 UTF-8 JSON，含 `key`、UTC 时间 `occurred`、`role`、`fixture`、`reason` 和 `data_dir`。程序参数固定，事件内容只通过标准输入传递。处理程序的退出码 0 表示接收成功。请用 `key` 去重，以处理接收成功之后、本机登记之前发生的进程重启。

发送在独立线程进行，单次最多三秒。失败信息、重试次数和送达时间保存在 `season_alert_delivery`，由 `season status` 的 `alert_delivery` 展示。处理程序失败后逐步延后重试，采集器持续运行时每秒最多派发一条。发送窗口为告警产生后一小时。更早的事件保留在本机告警表。

## 系统服务

确认 `deploy/polymarket861-season.service` 中的可执行文件和数据目录与本机一致。将同目录的采集服务、`polymarket861-watchdog.service`、`polymarket861-watchdog.timer` 放到 `~/.config/systemd/user/` 后执行以下命令。

```sh
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-season.service polymarket861-watchdog.timer
systemctl --user list-timers polymarket861-watchdog.timer
journalctl --user -u polymarket861-watchdog.service
```

要求注销后继续运行时，在目标主机按权限执行 `loginctl enable-linger "$USER"`，再用 `loginctl show-user "$USER" -p Linger` 确认。看门狗独立运行，每分钟检查一次。主进程心跳缺失或已超过 90 秒时发出 watchdog 告警。若需要推送，在两个 service 的 `ExecStart` 中都加同一 `--alert-command` 参数，然后重新加载并重启相应服务。看门狗优先投递心跳告警，也可接续一条等待派发的告警。整台机器断电时，本机定时器同样停止。有此监控要求时需要另一台主机观察外部心跳。
