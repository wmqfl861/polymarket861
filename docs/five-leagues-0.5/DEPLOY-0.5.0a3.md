# 部署 0.5.0a3

本页保留原版 `0.5.0a3` 的固定窗口部署命令。当前 `0.5.0a3+fiveauto.1` 使用滚动盘口窗口，另有四国预测服务和五国新闻定时器。[当前版部署与验收](LIVE-AUTOMATION.md)

建议用一台持续联网、时间自动同步的 Linux 主机，安装 Git、CPython 3.12、venv 和 systemd 用户服务。英超主服务负责赛前封存、球队新闻、轮报与模拟账本；西甲、德甲、意甲、法甲的第二个服务负责连续采集只读盘口。五个联赛都能跑历史比分回测。新增四国仍处于前向研究阶段。两个服务均使用免费公开数据，不需要交易所私钥。

## 从干净目录安装

以下命令适用于首次安装。默认签出本次发布分支的快照。也可先把环境变量 `PM861_REV` 设为交付的完整 Git 提交号，安装时打印的 HEAD 应与交付记录一致。已有英超服务可以先安装独立四国服务；若保留已有预测库，先看本机的版本状态和[发布治理](../model-release-governance.md)。

```sh
PM861_REV="${PM861_REV:-origin/release/0.5.0a3}"
SRC="$HOME/src/polymarket861-0.5.0a3"
EPL="$HOME/.local/share/polymarket861"
FIVE="$HOME/.local/share/polymarket861-five"
CONF="$HOME/.config/polymarket861-five"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$HOME/src" "$EPL" "$FIVE" "$CONF" "$UNITS"
git clone https://github.com/wmqfl861/polymarket861.git "$SRC"
git -C "$SRC" checkout --detach "$PM861_REV"
git -C "$SRC" rev-parse HEAD
python3.12 -m venv "$FIVE/venv"
"$FIVE/venv/bin/python" -m pip install -c "$SRC/constraints/research.txt" "$SRC"
"$FIVE/venv/bin/pm861" --version
```

四国服务的版本输出应为 `0.5.0a3`。下列命令沿用同一终端的变量。若希望机器重启或退出登录后持续采集，检查 `loginctl show-user "$USER" -p Linger`；具有权限的操作者可用 `loginctl enable-linger "$USER"` 启用。

## 先启动四国盘口值守

比赛选择使用 UTC 开球区间，包含起点，排除终点。例子覆盖 11 月 27 日至 30 日。按 11 月 26 日收到的排期，不来梅对门兴的第一个 T−24h 窗口在 11 月 27 日 03:30（台北／北京），希望覆盖时宜在 03:25 前取得首份完整目录。如果安装晚了，周报仍保留已知漏窗的原因。

```sh
cat > "$CONF/window.env" <<'WINDOW'
PM861_KICKOFF_START=2026-11-27T00:00:00Z
PM861_KICKOFF_END=2026-12-01T00:00:00Z
WINDOW
install -m 0644 "$SRC"/deploy/polymarket861-five-*.* "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-five-watch.service polymarket861-five-watchdog.timer
systemctl --user status --no-pager polymarket861-five-watch.service polymarket861-five-watchdog.timer
"$FIVE/venv/bin/pm861" --data-dir "$FIVE/state" market league-watchdog \
  --start 2026-11-27T00:00:00Z --end 2026-12-01T00:00:00Z
```

正常时退出码为 0，`healthy` 与 `scheduler_alive` 均为真，`catalog_by_league` 中四国均为 `fresh`。服务显示 active 但目录检查失败时，查看每国最后成功时间、距今秒数和日志。每个新比赛周将 `window.env` 换成所需开球区间，执行 `systemctl --user restart polymarket861-five-watch.service`。周报的两端指定的是**采样发生时间**。下例查询首个 T−24h 附近的实收与漏窗。[持续运行、周报与日志](WATCH-SERVICE.md)

```sh
"$FIVE/venv/bin/pm861" --data-dir "$FIVE/state" market league-report \
  --start 2026-11-26T19:00:00Z --end 2026-11-26T20:30:00Z
journalctl --user -u polymarket861-five-watch.service -u polymarket861-five-watchdog.service --since today
```

## 启动英超预测与新闻

下列步骤用于全新的英超安装。已有英超进程正在使用此路径时，只需完成上面的四国步骤即可。希望日后切换旧英超实例时，再按[英超版本治理](../model-release-governance.md)准备独立解释器并核对该实例的配方。

```sh
python3.12 -m venv "$EPL/venv"
"$EPL/venv/bin/python" -m pip install -c "$SRC/constraints/research.txt" "$SRC"
PM="$EPL/venv/bin/pm861"
"$PM" --version
STATE="$EPL/state"
"$PM" --data-dir "$STATE" data refresh --first 2022-23 --last 2026-27
install -m 0644 "$SRC/deploy/polymarket861-season.service" \
  "$SRC/deploy/polymarket861-watchdog.service" "$SRC/deploy/polymarket861-watchdog.timer" "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-season.service polymarket861-watchdog.timer
"$PM" --data-dir "$STATE" season readiness --year 2026 --round 12
"$PM" --data-dir "$STATE" news status
"$PM" --data-dir "$STATE" leagues news-endpoints preflight --version v2
```

对于新库，赛程、赛果和球队名单会从本机首次收件时开始累积。`season readiness` 会列本机版本、下一封存时间、仍缺的输入；`forecast round 2026-27 12 --mode technical` 可查看技术预测的逐轮评分。新闻预检只读。它列出哪些加权事实有可复验原件、哪些仍缺证、登记以后需保持哪一版新闻规则。正式登记是操作者另行作出的研究决定。[赛前到赛后](../MATCHDAY.md) · [新闻协议](NEWS-ENDPOINT-V2.md)

```sh
"$PM" --data-dir "$STATE" dashboard serve --port 8765
```

在部署机打开 `http://127.0.0.1:8765/`。使用远程服务器时可在自己电脑运行 `ssh -L 8765:127.0.0.1:8765 用户@服务器`，再打开同一地址。研究档可按账本所列市场、分档深度、费用假设和原生兑付复查每笔模拟结果。正式账继续执行其费用凭据要求。[模拟账本和费用口径](PAPER-RISK.md)
