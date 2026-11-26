# 四国盘口值守和心跳

这套服务按西甲、德甲、意甲、法甲的计划开球时间选择比赛，归档公开 Gamma 与 CLOB 原件。在另一份安装和数据目录中运行，可以同时保留原英超服务和它的正式封存。下例适用于有 systemd 用户服务的 Linux 主机，独立安装使用普通 wheel。当前实例启动、停止、状态与是否有双时点实收都由该主机报告。

## 首次安装

在部署机执行下列命令，示例中的源码目录供全新安装使用。若该目录已经存在，先核对它的远端和目标提交。把时间换成打算跟踪的比赛开球窗口，右端不含。采集从启动那一刻开始，故要早于第一场比赛的 T−24h。

```sh
BASE="$HOME/.local/share/polymarket861-five"
CONF="$HOME/.config/polymarket861-five"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$BASE" "$CONF" "$UNITS"
git clone --single-branch --branch main https://github.com/wmqfl861/polymarket861.git "$BASE/source"
python3.12 -m venv "$BASE/venv"
"$BASE/venv/bin/python" -m pip install -c "$BASE/source/constraints/research.txt" "$BASE/source"
"$BASE/venv/bin/pm861" --version
cat > "$CONF/window.env" <<'EOF'
PM861_KICKOFF_START=2026-11-27T00:00:00Z
PM861_KICKOFF_END=2026-12-01T00:00:00Z
EOF
install -m 0644 "$BASE/source"/deploy/polymarket861-five-*.* "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-five-watch.service polymarket861-five-watchdog.timer
systemctl --user status polymarket861-five-watch.service polymarket861-five-watchdog.timer
```

机器重启或用户退出后需要继续值守时，运行 `loginctl show-user "$USER" -p Linger`。返回 `Linger=no` 的部署由有相应权限的操作者执行 `loginctl enable-linger "$USER"`。值守服务在调度循环完成第一次检查后发送 READY，之后随实际调度进度发送心跳。异常退出由 systemd 重启，已启动后 3 分钟没有心跳也会触发重启。独立定时器每分钟检查指定窗口，即使主进程已停仍会在 journal 留下失败记录。[systemd 服务定义](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html) · [定时器定义](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)

## 跟踪第一批固定时窗

2026-11-26 实收目录中，不来梅–门兴排在 **11 月 28 日 03:30（台北／北京时间）** 开球。其 T−24h 在 **11 月 27 日 03:30**，本周末的服务宜在 **03:25 之前**完成首次目录拉取。意甲和法甲首场的 T−24h 为 03:45，西甲首场为 04:00。这些是本次原件中的计划开球时间，真正时窗随当时可得的排期处理。

```sh
PM="$HOME/.local/share/polymarket861-five/venv/bin/pm861"
STATE="$HOME/.local/share/polymarket861-five/state"
"$PM" --data-dir "$STATE" market league-watchdog --start 2026-11-27T00:00:00Z --end 2026-12-01T00:00:00Z
"$PM" --data-dir "$STATE" market league-report --start 2026-11-26T19:00:00Z --end 2026-11-26T20:30:00Z
journalctl --user -u polymarket861-five-watch -u polymarket861-five-watchdog --since today
```

`league-watchdog` 返回 JSON。正在按指定联赛、开球窗口和目录模式推进的进程退出码是 0。缺少记录、过期、停止、启动中或时钟冲突时退出码为 1。手工 `--cycles 1` 不会刷新服务心跳。`catalog_complete_by_league` 显示上次目录遍历是否完整，固定窗实收、状态标记和成对可用性请看周报。可在首个时窗结束后复查，例如 03:31 起查看首批 T−24h 收件。

更换赛程范围时更新 `window.env` 并执行 `systemctl --user restart polymarket861-five-watch.service`。原窗口仍有未兑付市场时可用 `market league-terminal --fixture 赛事slug` 补收终态。在没有 systemd 的系统上，可由自己的进程管理器运行相同的 `market league-watch --start … --end … --cycles 0`，并独立执行上述 CLI 心跳检查。

Claude 根据 38 场历史试采估计一个周末约占 5 GB，建议预留 15 GB。这是容量规划。实际 HTTP 数、请求延迟、数据库和原件大小由部署目录的周报提供。升级或回退既有英超实例前可在其自身安装运行 `season readiness --year 2026 --round 12`。[估算输入及假设](../review-evidence/code-review-0.5.0a1-2026-11-26/outputs/estimate-weekend-capture-volume.json) · [版本比较与回退](../model-release-governance.md)
