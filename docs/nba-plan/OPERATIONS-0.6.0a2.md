# NBA 0.6.0a2 部署与审核交接

本版覆盖 NBA 常规赛含加时的主客胜负、全场大小分、主队和客队大小分。可在独立账户部署球队研究和 Polymarket 只读采集。球队模型输出 N0、N1、N2，NBA 模拟资金及球员/新闻模型仍待后续阶段。现场验收从实际收件开始。[Claude 审核原文](../polymarket861-nba-phase1-code-review-0.6.0a1.md) · [最新开发计划](REVIEWED-DEVELOPMENT.md) · [独立复核与版本记录](INDEPENDENT-REVIEW-0.6.0a2.md)

## 先保全足球旧仓

如果原部署机保存有 `/tmp/pm861-weekly-value-20261127`，请先在重启或清理前执行归档及校验，然后用新版脚本对账本副本逐笔比较六个旧仓、现金及其后兑付。下列操作在足球账户上完成；任一对不上，预检报告会列出具体仓位，保留现役服务。新候选暂停全场大/小 2.5 和“主队不胜”，足球推荐源继续按已登记权限启用。[实际命令、缺件处理和比对报告](../five-leagues-0.5/FOOTBALL-PAPER-REVIEW-20261128.md)

## 安装 NBA 与启动盘口

以下为 Linux 用户级 systemd。使用专用 NBA 系统用户，在本版仓库根目录执行，Python 支持 3.11–3.13。现役足球封存与模拟选单保留已签核的独立足球 wheel。选单登记含物理实现摘要，更换其执行包会改变旧试验的复核依据。交互命令和 systemd 必须指向同一 NBA 数据目录。

```bash
base="$HOME/.local/share/polymarket861-nba"
cfg="$HOME/.config/polymarket861-nba"
units="$HOME/.config/systemd/user"
mkdir -p "$base/wheels" "$cfg" "$units"
chmod 700 "$base" "$cfg"
python3 -m venv "$base/venv"
"$base/venv/bin/python" -m pip wheel --no-deps -w "$base/wheels" .
"$base/venv/bin/python" -m pip install -c constraints/research.txt "$base/wheels/polymarket861-0.6.0a2-py3-none-any.whl"
nba() { "$base/venv/bin/pm861" --data-dir "$base/state" nba "$@"; }
"$base/venv/bin/pm861" --version
install -m 0644 deploy/polymarket861-nba-*.{service,timer} "$units/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-nba-markets.timer
systemctl --user start polymarket861-nba-markets.service
nba markets status
nba watchdog --component markets
```

只开盘口时，可在 `$cfg/providers.env` 中写 `PM861_NBA_WATCH_COMPONENT=markets`，再启用 `polymarket861-nba-watchdog.timer`。赛程与封存启用后把该值改回 `all`。需注销后持续运行时检查该 NBA 用户的 linger。只有比赛目录、当窗两侧真实买卖档、完整规则和有效时间同时满足时才会得到合格报价。报价使用盘口中间价比较概率，实际买入成本和可成交量另需订单簿；本版 NBA 不开仓。

## 认证、预热和启用完整采集

用本人的免费账户提供 `BALLDONTLIE_API_KEY`、`BIGBALLS_API_KEY`、`DUNKAPI_API_KEY`，直接在部署机填写，每行一个环境变量。首次使用时可用 `(umask 077; touch "$cfg/providers.env")` 建空文件，已有文件保留内容并执行 `chmod 600 "$cfg/providers.env"`。密钥始终留在本机。

```bash
set -a
. "$cfg/providers.env"
set +a
nba data verify-schedule
nba model warmup --sync
nba model warmup
nba data capabilities
systemctl --user enable --now polymarket861-nba-{schedule,forecast,sources,watchdog}.timer
systemctl --user list-timers 'polymarket861-nba-*'
```

`verify-schedule` 会核对当前、10 月和上季 12 月三个日期范围，缺改期或延期样本时保留待观察项，可另给 `--exception-day YYYY-MM-DD` 验证有历史收件的日期。技术采集可继续累积所缺回执。回填按本机共用的免费配额运行；遇限流按返回时刻重新执行相同命令，保留已完整批次。仅需订单簿时可停在盘口步骤。带账户的五季复查用 `nba model modern-study --sync`。具体来源、条款文件、状态时间字段和各项缺件示例见[免费来源手册](SOURCE-ACCEPTANCE.md)；历史回放与主场依据见[模型修订](MODEL-REPAIR-0.6.0a2.md)。

## 日常使用与第二阶段验收

```bash
day=$(TZ=America/New_York date +%F)
nba fixtures --date "$day"
nba forecast-show bdl:12345       # 用上一条输出的 game_id 替换
nba report --date "$day"
nba report --week 2026-11-23       # 美东周一
nba markets status
nba data capabilities
nba readiness
journalctl --user -u polymarket861-nba-forecast.service -n 40
```

| 读数 | 如何使用 |
| --- | --- |
| 当日各时点 | 日报区分到期、封存、预热充分、当窗合格胜盘和实际配对，同时保留错钟、过窗、同步失败等原因。赛程不够新或来源质检失败时自动留缺。竞技预测的原记录始终可查。 |
| 盘口与赛程时差 | 独立按双方已知的主客、身份、日期及时间核对。暂用 30 分钟容差；真正配对要求价格能同时满足两个决策窗口。两边开赛差 10 分钟时竞技结果仍可封存，两组五分钟窗口没有交集，报价独立留档；三小时偏差阻断市场配对。身份冲突、已开赛或提前开赛会阻断不安全的预测。 |
| 预测与兑付 | 日报/周报分别统计比分推导方向、原生兑付一致/不一致及 50:50。明确期次或足够的独立证据才计为加时先例。比分后来更正会产生新修订。 |
| 冷启动和漏采 | `calendar` 的计数从第一份成熟 T−60 封存开始，分母同时包含已知赛程、目录独有场及缺时刻比赛。按连续已结束的美东比赛日计算，完全缺采的一天不能变成休赛日。 |
| 14 日门槛 | T−60 成熟封存率和合格胜盘配对率均至少 90%；无合格胜盘当天显示证据不足。查看 `calendar.days` 的分子、分母和原因；按 `--through YYYY-MM-DD` 核对历史结束日。 |
| 球员来源 | 每源独立测量美东首场 T−60 前 15 分钟内的真实状态，并核对上游身份、日期语义和预测/保留用途。至少一个来源连续覆盖七个比赛日才满足来源条件。 |
| 是否进入第二阶段 | `readiness` 同时列出真实认证赛程、三个完整双源对齐日、14 日运行、7 日来源四个条件，只在都符合时返回 `eligible_for_review`。另核同机足球 429、看门狗和六仓预检。程序不自动开放 NBA 球员、新闻或资金。 |

停用 NBA 时执行 `systemctl --user disable --now polymarket861-nba-{markets,schedule,forecast,sources,watchdog}.timer`。保留专用 state，可从封存、逐源收件及各版报告重建审查过程。

## 本轮证据

| 范围 | 实测材料 |
| --- | --- |
| 盘口先到/稍后落库 | 两档网络延迟，各 11 种相位乘 12 次触发，合计 264 次。全量结果见[相位探针](../../reports/nba-phase1/a2-timer-phases.json)。 |
| 规则、盘口和市场缺件 | 11 场真实赛前原件、821 条挂牌和 1,642 本双代币订单簿；主线入选包含 8 条 alternate。正常时点能选出的四类家族共 33 个，另有 11 个在对应场次没有挂牌。此外由生产采集入口在真实 T−60 窗取得 4 场的 16 个盘口和 4 条让分原件。[真实固定窗](../../reports/nba-phase1/market-collector-20261128T0126Z.json)。[11 场核对](../../reports/nba-phase1/market-lines-20261128.json) · [原件重放](../../scripts/verify_nba_market_lines.py) |
| 真实加时 | 5 场历史全场大小合约，用独立节次比分核对双 token 终局。3 场若错用常规时间比分会把方向判反。另列一场四节比赛作对照。用于研究兑付规则，部署机前向对账仍需另行累计。[历史对账](../../reports/nba-phase1/v2-overtime-reconciliation.json) |
| 历史回测 | 旧归档 12,059 场在赛地缺失部署情形修复后的 N2 主胜 Brier 为 0.20604。近五季 6,150 场保留固定系数并按 24h/48h 各自重跑，N2 主胜 Brier 分别为 0.2137202 和 0.2140122；来源修订、实际样本和限制见[现代回测](../../reports/nba-phase1/modern-archive/README.md)。 |
| 现场凭据 | 本环境在开发时缺少部署机的三个私人密钥，真实带钥赛程、连续部署时长和球员来源准入仍待现场计数。[空账户报告](../../reports/nba-phase1/a2-sources) · [原生市场与赛程差分](../../reports/nba-phase1/kickoff-differences-20261128.json) |

## 审核意见处理索引

| Claude 编号 | 已实施及仍需取得的证据 |
| --- | --- |
| Y1-1、Y1-5 | 以同窗合格胜盘触发封存，必要时等下一次调度。时间不足时保全竞技预测。赛程更新前的报价不能进入新排期的正式配对。跨时差的单边价格仍可从原生采集复核。现场三个完整比赛日再核 30 分钟同场容差。[双源对照命令](../../src/polymarket861/nba/markets_schedule.py) |
| Y1-2、Y1-4 | 官方来源明确的中立场例外和日常挂牌主场在训练与推断共用，缩写归一。预热按球队、对阵和日期验证；原收到日早于所称终场日期的比分会隔离。旧缓存须重新收取可验证阶段。[模型与独立未来数据反例](MODEL-REPAIR-0.6.0a2.md) |
| Y1-3、Y2-1、Y3-1、Y3-4 | 阶段过滤失效、缺时刻、未知状态、时钟冲突、漏日与不可信伤停时间字段分别可见；延期与改期有赛前证据要求。伤停三窗口及每日探针、盘口单组件看门狗和按原因的漏封统计都能独立运行。三段认证和两个来源均保留实际尚缺项目。[来源验收](SOURCE-ACCEPTANCE.md) |
| Y2-2、Y2-4、Y3-2 | 实现逐合约原生兑付对账及版本、11 场全部盘口线重放和低优先级让分主线原件归档。每日优先采集四类已支持玩法，盘口有限时让分原件可留缺。[实际固定窗](../../reports/nba-phase1/market-collector-20261128T0126Z.json) |
| Y2-3、Y3-3 | 完成五个现代赛季，N2 保留固定系数。逐季记录小分差概率及实际频率，待让分模型立项后验证小分差形状。[历史五季](../../reports/nba-phase1/modern-archive/README.md) |
| Y3-5、Y3-6 | 七种情景加入常规持续集成，安装包实际连接比赛目录、双边簿、比分、封存和报告。新闻正式试验保留四个终点、校正和既定功效限制，尚未登记。 [端到端回归](../../tests/test_nba_review_pipeline.py) · [试验口径](VALIDATION.md) |
