# NBA 0.6.0a1 使用与审核交接

本批交付男子 NBA 常规赛的赛前球队模型、四类预测、独立的只读盘口采集及赛后评分。三种免费数据账户在开发环境中的已认证业务回执都是零。Polymarket 无需这些账户即可采集。球员/资讯层、正式增益试验、NBA 模拟资金的进场条件见[审核后开发计划](REVIEWED-DEVELOPMENT.md)。足球部署脚本的修改及六笔旧模拟仓位的原件保全单列在[足球复核回应](../five-leagues-0.5/REVIEW-0.5.0a4-FOLLOWUP.md)。

## 在独立环境安装

以下命令在本分支仓库根目录运行，适用于有 Python 3.11–3.13 的 Linux。NBA 服务从专用路径取程序及状态。同机与旧足球长期共存时，用独立 Linux 用户执行以下命令，专用 home/state 仅授该用户读取与写入权限（0700），两套服务分别运行在各自用户管理器下。原足球服务的 ExecStart 仍指向已签核的足球版本；仅替换足球 wheel 会使运营选单依据变化。Windows 可在单独 venv 运行相同的 Python 命令，定时任务样例面向 Linux 用户级 systemd。

```bash
base="$HOME/.local/share/polymarket861-nba"
mkdir -p "$base/wheels" "$HOME/.config/polymarket861-nba"
python3 -m venv "$base/venv"
"$base/venv/bin/python" -m pip wheel --no-deps -w "$base/wheels" .
"$base/venv/bin/python" -m pip install -c constraints/research.txt "$base/wheels/polymarket861-0.6.0a1-py3-none-any.whl"
cli="$base/venv/bin/pm861"
nba() { "$cli" --data-dir "$base/state" nba "$@"; }
"$cli" --version
nba data capabilities
nba markets collect
nba markets status
```

示例里的 shell 函数始终读取与服务相同的 state。如需直接执行 `pm861 nba status`，请先设 `export PM861_NBA_DATA_DIR="$base/state"`；未设置时交互命令默认读 `~/.polymarket861-nba`。

有 BALLDONTLIE 免费账号后，将自己的密钥以 `BALLDONTLIE_API_KEY=实际值` 写入 `~/.config/polymarket861-nba/providers.env` 并设为仅用户可读。手动命令需在当前 shell 导入，systemd 样例自动读取该文件。Big Balls 与 DunkAPI 的候选来源探针分别读取 `BIGBALLS_API_KEY` 和 `DUNKAPI_API_KEY`。能力表列每源的凭据、可见字段及准入状态。收到伤停字段只证明可读，进入球员层还要满足赛前时点和使用权。

```bash
set -a
. "$HOME/.config/polymarket861-nba/providers.env"
set +a
nba run --component schedule
nba run --component markets
nba run --component forecast
nba status
```

首次安装的训练历史很短。按日期分段运行 `nba data sync --start YYYY-MM-DD --end YYYY-MM-DD` 取本季及更早已结束的常规赛，单个请求支持分页。限流或上游暂不可用时按输出的恢复时刻重试该日期段；新收到的往季比赛从实际收到后才可进入前向预测。自动封存还要求覆盖该比赛日的完整赛程同步在近 35 分钟内成功，查询失败或原生开赛冲突时会给出原因。球队模型至少需要 30 场合格终场，数据库里只有旧的 538 文件时，当前赛程仍须独立收取。自动更新范围为美东当日前 14 天至后 7 天。获知更早赛果有更正时，按相同命令指定该日期重新同步；后续评分会追加版本。逐源状态或暂停原因可在 `nba status` 和 `nba data capabilities` 查看。

## 找到一场比赛并读出概率

```bash
day=$(TZ=America/New_York date +%F)
nba fixtures --date "$day"
# 用上一步 games[].game_id 的值替换 bdl:12345
nba forecast-show bdl:12345
nba model predict bdl:12345 --as-of "$(date -u +%FT%TZ)" --total-line 222.5 --home-line 113.5 --away-line 109.5
nba report --date "$day"
nba report --week 2026-11-23
```

`forecast-show` 返回实际封存版本、截止、使用的赛程和三层模型，以及赛后评分的每次修订。交互预测按指定的过去或当前时点重建，只作预览。查看输出的 `as_of`、`preview_eligibility` 和 `schedule_verification`：最新完整赛程核验合格时显示 `verified_schedule_preview`，仅有陈旧或缺件的赛程时显示 `exploratory_unverified_schedule` 并给出原因。两类预览的 `forward_scoring_eligible` 均为 false。已知开赛、撤回、身份不一致或盘口与赛程冲突时停止计算。按 N0、N1、N2 查看主/客胜率以及 total、home、away 三个整数得分分布。在实际线或自行指定的线上分别取大、等于、小概率。市场和模型使用各自的真实时刻，不能为错过的窗口补签。明确的中立场对称处理。未标明赛地时，`venue_basis=unknown_home_advantage_withheld` 表示该场不加主场优势，训练时也只用已确认非中立场的记录更新主场优势。终场均含加时；盘口分类、队名、线、条款、实收或双边深度缺件时，仍可独立评估竞技预测。

`nba report` 的日期是美国东部比赛日，周报参数为周一。统计分母为本机确实观察到的合格赛程；市场来源是独立样本。晚收到的赛程、漏窗、未定和补正不会覆盖旧预测，按 `--revision N` 可复核旧报表。初装或缺账户时 `nba status`、`nba watchdog` 会返回非零，结合输出的分组件状态判断故障；看到没有合格新闻或持仓属当前交付范围。

## 定时运行和停用

```bash
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 deploy/polymarket861-nba-*.{service,timer} "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-nba-{markets,schedule,forecast,watchdog}.timer
systemctl --user list-timers 'polymarket861-nba-*'
journalctl --user -u polymarket861-nba-forecast.service -n 40
```

盘口和封存每分钟运行、赛程每 15 分钟、看门狗每分钟检查各自的新鲜度。0.6 的两个运动入口会主动校验目录。旧足球包直接开主库的路径另有保护；它的独立文件导出/辅助脚本运行在原足球用户下，由操作系统保护 NBA 私有目录。同一帐号下任意指定输出路径的脚本仍有该帐号本身的写权限。只有盘口需求或尚无比分密钥时，可以先只启用 `polymarket861-nba-markets.timer`。需要注销后持续运行时，确认该 Linux 用户的 systemd 用户管理器已启用 linger。停止 NBA 定时任务使用 `systemctl --user disable --now polymarket861-nba-{markets,schedule,forecast,watchdog}.timer`。已开始的 oneshot 会自行结束；保留专用 state 目录供复核。若部署环境没有 systemd，可用相同的独立组件命令交给现有调度器。

## 历史证据与限制

初版按固定哈希校验 FiveThirtyEight 的 NBA Elo 归档（赛果上游 Basketball-Reference），仅由已完成的早期比赛重新学习。归档的最后一场为 2015-06-16。以下回放用 2005/06–2014/15 十个常规赛季的 12,059 场，2005 和 2014 是赛季开始年份。全档按 NBA 身份去重有 59,008 场。归档滞后是对缺少早期原始收件时间的一项假设。 [数据、出处和授权](DATA-SOURCES.md)

```bash
curl --fail --location https://raw.githubusercontent.com/fivethirtyeight/data/master/nba-elo/nbaallelo.csv -o "$base/nbaallelo.csv"
nba model backtest --archive "$base/nbaallelo.csv" --first-season 2005 --last-season 2014 --lag-days 1 --replicates 10000
nba model backtest --archive "$base/nbaallelo.csv" --first-season 2005 --last-season 2014 --lag-days 2 --replicates 10000
```

| 赛果滞后假设 | N0 联盟基准 Brier | N1 球队攻防 Brier | N2 加背靠背 Brier |
|---|---:|---:|---:|
| 比赛日结束后 24 小时 | 0.24110 | 0.20666 | 0.20604 |
| 比赛日结束后 48 小时 | 0.24110 | 0.20700 | 0.20635 |

越低越好。24 小时口径下，N2−N1 为 −0.000623，按季分层、7 日区块的探索性 95% 区间为 [−0.001069, −0.000213]；N2 的全场/主队/客队分 CRPS 为 9.9951/6.0270/6.0114。N2 对三分以内分差给出 18.49%，实测 15.27%；一分分差给出 6.22%，实测 4.12%。这项已暴露的回放反映旧时代比赛，现代五季、对 Polymarket 优势、新闻影响和资金收益均需另行验证。[24 小时报告](../../reports/nba-phase1/baseline-538-lag24.json) · [48 小时报告](../../reports/nba-phase1/baseline-538-lag48.json)

11 月 27 日的真网目录观察到了美东 27 日 11 场、28 日 5 场。采集启动时，27 日 11 场的 T−24h 已经过期，四类市场共 44 个机会登记为漏窗。另一个窗外探针核对 MIA @ NYK 的 6 个合约、12 个代币，读取胜方及全场、两队大小的双边报价；另取已完赛 CHI @ CHA 的 227.5 总分盘核对 Over/Under 兑付。这些探针只核验接口和方向。真正的配对统计必须来自部署后实际到窗收取的报价、预测及赛果。[真实盘口与兑付凭据摘要](../../reports/nba-phase1/market-observations.json) · [数据源能力实测](../../reports/nba-phase1/source-capabilities.json)

首 14 个比赛日应按每个时点分别盘点应有赛程、实际封存、窗内已收盘口和预测提交时实际配对的场数。并发定时器可能先封球队预测，随后同窗才得到价格；后一份价格可复查，但不回填到该次预测。现场还需取得有钥免费比分回执、三类时点的前向配对及连续 14 个比赛日覆盖；验证报告、模型和部署模板不能代替这些观测。新闻模型的决定点、人工盲标及何时可以研究模拟资金见[分阶段验收](REVIEWED-DEVELOPMENT.md)。
