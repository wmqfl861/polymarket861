# 四国盘口值守和心跳

这套服务按西甲、德甲、意甲、法甲的计划开球时间选择比赛，归档公开 Gamma 与 CLOB 原件。在另一份安装和数据目录中运行，可以同时保留原英超服务和它的正式封存。下例适用于有 systemd 用户服务的 Linux 主机，独立安装使用普通 wheel。当前实例启动、停止、状态与是否有双时点实收都由该主机报告。

## 首次安装

在部署机执行下列命令，示例中的源码目录供跟随主分支的全新安装使用。按本次发布的固定提交安装、新装英超与四国两个服务，请使用部署手册。若该目录已经存在，先核对它的远端和目标提交。当前服务按 UTC 每日滚动，包含前 14 天至第 8 天零时之前的开球。采集从启动那一刻开始，故要早于第一场比赛的 T−24h。[0.5.0a3 部署手册](DEPLOY-0.5.0a3.md)

```sh
BASE="$HOME/.local/share/polymarket861-five"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$BASE" "$UNITS"
git clone --single-branch --branch main https://github.com/wmqfl861/polymarket861.git "$BASE/source"
python3.12 -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install -c "$BASE/source/constraints/research.txt" "$BASE/source"
"$BASE/venv/bin/pm861" --version
install -m 0644 "$BASE/source"/deploy/polymarket861-five-*.* "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-five-watch.service polymarket861-five-watchdog.timer
systemctl --user status polymarket861-five-watch.service polymarket861-five-watchdog.timer
```

机器重启或用户退出后需要继续值守时，运行 `loginctl show-user "$USER" -p Linger`。返回 `Linger=no` 的部署由有相应权限的操作者执行 `loginctl enable-linger "$USER"`。用户服务在首次调度检查完成后发送 READY，之后随实际调度进度发送 WATCHDOG。启动时上游无法访问也可以就绪，采集器会重试。异常退出后 systemd 延迟 5 秒重启。服务就绪后 3 分钟没有调度心跳也会触发重启。独立定时器每分钟检查一次心跳和四国目录，失败会记入本机 journal。[systemd 服务定义](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) · [定时器定义](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)

## 跟踪第一批固定时窗

2026-11-26 实收目录中，不来梅–门兴排在 **11 月 28 日 03:30（台北／北京时间）** 开球。其 T−24h 在 **11 月 27 日 03:30**，本周末的服务宜在 **03:25 之前**完成首次目录拉取。意甲和法甲首场的 T−24h 为 03:45，西甲首场为 04:00。这些是本次原件中的计划开球时间，真正时窗随当时可得的排期处理。

```sh
PM="$HOME/.local/share/polymarket861-five/venv/bin/pm861"
STATE="$HOME/.local/share/polymarket861-five/state"
"$PM" --data-dir "$STATE" market league-watchdog --rolling
"$PM" --data-dir "$STATE" market league-report --start 2026-11-26T19:00:00Z --end 2026-11-26T20:30:00Z
journalctl --user -u polymarket861-five-watch -u polymarket861-five-watchdog --since today
```

建议在首个时窗前（本例为 03:20 左右）运行 `league-watchdog`。在线模式的退出码 0 要求同一组联赛、开球窗口和模式内至少一个调度器正在运行，并且每个联赛都在阈值内完整收过目录。退出码 1 表示这两项中至少一项未通过。命令参数或数据库错误会返回 2。

| JSON 字段 | 含义 |
| --- | --- |
| `scheduler_alive`、`state`、`age_seconds` | 调度器是否在推进、进程状态、距最近一轮心跳的秒数。默认阈值 180 秒，可用 `--max-age` 调整。同主机 Linux 在可读取进程身份时也能识别被杀死或 PID 被复用的旧进程。 |
| `catalog_by_league` | 每个联赛分别列出 `last_success_at`（UTC）、`age_seconds`、`state`。时间来自最近一次完整目录遍历最后一页的收件时间。部分翻页、失败重试、单场刷新和重复心跳都不能延后这个时间。完整的空目录也算成功。 |
| `catalog_state`、`catalog_healthy` | 在线模式逐联赛检查，全部新鲜才是 `fresh`。任一从未成功为 `missing`，超过默认 600 秒为 `stale`，收件时间晚于查询时钟为 `clock_conflict`。有多种异常时先报时钟、再报缺失，再报过期，逐联赛字段保留每一项。阈值可用 `--max-catalog-age` 调整。 |
| `catalog_complete_by_league` | 当前调度器最近一次目录尝试的分页结果。某联赛本次失败时，先前仍在阈值内的成功收件仍然有效。 |

四国服务按上面的命令查询。单联赛参数对应另一个采集范围。以 `--local-catalog` 手工运行的连续进程须用相同参数查询，它只验证调度心跳，返回 `catalog_state=local_only`、`catalog_by_league=null`、`catalog_healthy=null`。手工 `--cycles 1` 只执行一次，无法提供持续服务心跳。旧健康库在首次新版值守时自动补充记录表，旧版的完成标记缺少逐联赛收件时间，须等到一次新的完整遍历才能确认目录新鲜。固定窗实收和成对可用性请看周报，例如 03:31 起查看首批 T−24h 收件。

需要单独审计固定窗口时，在独立状态目录运行带 `--start`、`--end` 的采集器并以同一参数执行看门狗。原窗口仍有未兑付市场时可用 `market league-terminal --fixture 赛事slug` 补收终态。在没有 systemd 的系统上，可由自己的进程管理器运行相同的 `market league-watch --start … --end … --cycles 0`，并独立执行上述 CLI 心跳检查。手工后台启动时可以用 SIGINT 或 SIGTERM 正常收尾。值守器等待已派发任务收尾、写入停止状态并发送 STOPPING，CLI 返回 130，服务单元把 130 视为正常停止。强杀不会触发收尾，同机可验证的 Linux 进程按 PID 与启动身份识别退出，其他环境在心跳超时后识别失联。

## 可选的本机桌面通知

默认故障记录可用上面的 `journalctl --user` 命令查看。在有图形用户会话且已安装 `/usr/bin/notify-send` 的机器上，可为检查单元增设本机通知。以下配置使用登录用户的桌面通知服务。持续故障可能每分钟提醒一次。[systemd OnFailure](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html#OnFailure=)

```sh
UNITS="$HOME/.config/systemd/user"
mkdir -p "$UNITS/polymarket861-five-watchdog.service.d"
cat > "$UNITS/polymarket861-five-alert.service" <<'EOF'
[Unit]
Description=Display a local market collector health alert
ConditionFileIsExecutable=/usr/bin/notify-send

[Service]
Type=oneshot
NoNewPrivileges=yes
ExecStart=/usr/bin/notify-send -u critical -a pm861 "四国盘口值守检查失败" "运行 market league-watchdog 查看调度器和逐联赛目录时间。"
EOF
cat > "$UNITS/polymarket861-five-watchdog.service.d/alert.conf" <<'EOF'
[Unit]
OnFailure=polymarket861-five-alert.service
EOF
systemctl --user daemon-reload
```

Claude 根据 38 场历史试采估计一个周末约占 5 GB，建议预留 15 GB。这是容量规划。实际 HTTP 数、请求延迟、数据库和原件大小由部署目录的周报提供。升级或回退既有英超实例前可在其自身安装运行 `season readiness --year 2026 --round 12`。[估算输入及假设](../review-evidence/code-review-0.5.0a1-2026-11-26/outputs/estimate-weekend-capture-volume.json) · [版本比较与回退](../model-release-governance.md)
