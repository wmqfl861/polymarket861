# 0.5.0a4 部署与验收

适用于已运行英超、四国预测、四国盘口和五国新闻服务的机器。0.5.0a4 可在 Linux、macOS、Windows 上运行 Python 命令。以下常驻和看门狗验收使用 Linux/systemd 与 CPython 3.12。仓库发布分支为 `release/0.5.0a4`。生产机器在部署前核对该提交、独立复审记录及 GitHub Actions 成功状态。[发布检查](https://github.com/wmqfl861/polymarket861/actions/workflows/test.yml) · [独立复审](INDEPENDENT-REVIEW-0.5.0a4.md)

## 确认实际路径与窗口

先看正在运行的服务、解释器和数据目录。旧机交接记录为系统级单元、服务账户 `ubuntu`，英超库位于 `/mnt/d/polymarket861/.pm861`，四国预测库为 `.local-deploy/five-automation/state`，盘口为 `.local-deploy/five-league-state`。这些是上次部署记录。当前机器以实际的 `ExecStart`、`EnvironmentFile`、联赛历史目录和 `User` 为准。勿将 API 密钥、环境文件或数据库上传。[旧机路径与服务](LIVE-AUTOMATION.md)

```sh
systemctl cat polymarket861-season.service polymarket861-five-forecast.service
systemctl show polymarket861-season.service polymarket861-five-forecast.service -p User -p ExecStart
systemctl list-timers --all 'polymarket861-*'
```

用在役程序执行 `season readiness --year 2026 --round 12` 及四国 `big5-scheduler status` 查看当天窗口。将涉及英超的停机或重启安排在固定窗口一小时前以外，并留足全部服务恢复时间。审核记录的 2026-11-27 22:55–23:00（UTC+8）是利兹联–考文垂和曼联–布伦特福德的 T−24h。最新状态以赛程更正及本机收件为准。过去的 `missed` 留在报表中。

## 准备独立安装和同库比较

先从在役程序备份并验证英超状态，同时为四国预测和盘口目录做一致性备份。保留旧可执行文件、服务覆盖文件、历史归档和至少一份英超封存摘要。已有 SQLite 库正在写入时，使用项目备份或支持一致快照的方式。准备新安装可以与旧服务运行并行。

```sh
SRC="$HOME/src/polymarket861-a4"
REL="$HOME/.local/share/polymarket861/releases/0.5.0a4"
mkdir -p "$HOME/src" "$REL"
git clone --branch release/0.5.0a4 --single-branch https://github.com/wmqfl861/polymarket861.git "$SRC"
git -C "$SRC" rev-parse HEAD
python3.12 -m venv "$REL/venv"
"$REL/venv/bin/python" -m pip install -c "$SRC/constraints/research.txt" "$SRC"
NEW_PM="$REL/venv/bin/pm861"
"$NEW_PM" --version
```

下文的 `OLD_PM` 是在役程序的绝对路径，`EPL`、`HISTORY`、`FIVE` 和 `MARKET` 是现用的英超库、英超五季归档、四国预测库和盘口目录，`DIGEST` 取自本机旧封存。用服务账户运行数据库命令。先在旧程序保持生效期间创建基准，使用新程序生成本机比较报告。

```sh
"${OLD_PM:?}" --data-dir "${EPL:?}" forecast release-baseline "${HISTORY:?}"
"$NEW_PM" --data-dir "$EPL" forecast release-compare "$HISTORY"
"$OLD_PM" --data-dir "$EPL" forecast get "${DIGEST:?}" > "$REL/seal-before.json"
"$NEW_PM" --data-dir "$EPL" forecast get "$DIGEST" > "$REL/seal-after.json"
cmp "$REL/seal-before.json" "$REL/seal-after.json"
```

新版 B1 准入摘要与 fiveauto.1 不同。发布测试的 1,900 场比较中，概率和训练视图变化均为 0。负责人核对本机报告的配方、样本、逐项差异及封存，并在维护窗口协调切换。使用新程序批准后，旧安装无法继续签新的正式封存。以下变量必须来自本机比较报告和实际负责人的决定。[版本签核与反向比较](../model-release-governance.md)

```sh
"$NEW_PM" --data-dir "$EPL" forecast release-report "${REPORT_SHA256:?}"
"$NEW_PM" --data-dir "$EPL" forecast release-approve "$REPORT_SHA256" --reviewer "${REVIEWER:?}" --reason "${REASON:?}"
"$NEW_PM" --data-dir "$EPL" season readiness --year 2026 --round 12
```

## 切换服务与确认

现有系统级部署保留 `User`、工作目录、四组实际路径、原网络解析选项和 `--execution-research` 等在役参数。将英超、英超看门狗、四国预测、盘口、两类看门狗、新闻采集和看板的可执行路径对应到已核验的安装。四国预测主服务从分发单元采用 `Type=notify`、`NotifyAccess=main`、`WatchdogSec=3min` 及停止信号。新的一对单元为 `polymarket861-five-forecast-watchdog.service` 与 `.timer`。系统级检查单元使用与四国预测主服务相同的服务账户和研究目录，其 `ExecStart` 应等价于下面第一条命令。安装或修改单元后 `sudo systemctl daemon-reload`，重启变更的常驻服务，并执行 `sudo systemctl enable --now polymarket861-five-forecast-watchdog.timer`。不要把使用 `%h` 的用户模板直接当作系统级部署配置。[分发单元](../../deploy)

```sh
"$NEW_PM" --data-dir "${FIVE:?}" big5-scheduler watchdog
"$NEW_PM" --data-dir "$FIVE" big5-scheduler status
"$NEW_PM" --data-dir "${MARKET:?}" market league-watchdog --rolling
"$NEW_PM" --data-dir "$EPL" run status
journalctl -u polymarket861-season -u polymarket861-five-forecast -u polymarket861-five-forecast-watchdog --since today
```

正常时两套看门狗均返回 0。四国调度超过 120 秒未完成、常驻进程消失、来源故障等会返回 1；首次初始化期间也可能返回 1。按联赛看 `last_cycle_age_seconds`、最近成功、未完成周期数和 `news_poll_failures`。看板 `/leagues` 可以查到逐场消息和收件时间。英超在真实固定窗口过后执行 `run status`，核对 `done`、封存摘要、`attempts[].schedule_verification`、失败原因和下次可重试时间。赛后以实际收件的比分核对四国 `scored_samples_by_horizon`。

例如旧机采用系统级 `ubuntu` 服务，准备好与原生产路径相符的覆盖配置后，先核对 `systemctl cat` 的生效内容，再分别运行下列命令。新检查服务可由仓库模板复制到 `/etc/systemd/system`，并用 `sudo systemctl edit polymarket861-five-forecast-watchdog.service` 设置 `User=ubuntu`、实际 `PM861_DATA_DIR`、空的 `ExecStart=` 行和新的绝对 `ExecStart=/实际版本/venv/bin/pm861 --data-dir /实际四国预测目录 big5-scheduler watchdog`。预测主服务加载本版 notify/watchdog 指令。仍在等待英超签核期间可单独切换四国。

```sh
sudo systemctl daemon-reload
sudo systemctl restart polymarket861-five-forecast.service
sudo systemctl enable --now polymarket861-five-forecast-watchdog.timer
sudo systemctl start polymarket861-five-forecast-watchdog.service
systemctl show polymarket861-five-forecast.service -p Type -p MainPID -p WatchdogUSec -p ExecStart
systemctl list-timers --all 'polymarket861-*'
# 英超完成本机签核并进入维护窗口后
sudo systemctl restart polymarket861-season.service polymarket861-dashboard.service
```

首次采用仓库默认的**用户级**部署时，将 wheel 安装到 `$HOME/.local/share/polymarket861-five/venv`，使用以下命令。该模板与旧机的系统级部署是两个独立安装方式。

```sh
FIVE_BASE="$HOME/.local/share/polymarket861-five"
UNITS="$HOME/.config/systemd/user"
mkdir -p "$FIVE_BASE" "$UNITS"
python3.12 -m venv "$FIVE_BASE/venv"
"$FIVE_BASE/venv/bin/python" -m pip install -c "$SRC/constraints/research.txt" "$SRC"
"$FIVE_BASE/venv/bin/pm861" leagues prepare-history "$FIVE_BASE/openfootball"
install -m 0644 "$SRC"/deploy/polymarket861-five-*.service "$SRC"/deploy/polymarket861-five-*.timer "$UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-five-watch.service polymarket861-five-forecast.service \
  polymarket861-five-watchdog.timer polymarket861-five-forecast-watchdog.timer polymarket861-five-press.timer
```

回退时先暂停切换的调度、保留新增收件与全部 SQLite 状态。英超已批准新版时，由当前版本在同一数据目录冻结基准，旧版本执行反向比较和具名批准后再启动。四国旧程序可读取原有预测、比分和名单。若回退到 fiveauto.1，应同时停用新版预测看门狗并恢复原预测服务单元，旧版会恢复原有的重复新闻封存行为。保留新增证据表供日后审计。[历史实收与数据目录](LIVE-AUTOMATION.md)
