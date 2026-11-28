# NBA 免费来源验收（0.6.0a2）

在运行 NBA 的机器上执行命令。赛程和伤停分开验收。项目的球队层模型、真实盘口观测和部署机的覆盖率由各自的报告记录。球员和新闻模型仍待下一阶段评估。

## 认证赛程

BALLDONTLIE 的公开接口文档列出 `/nba/v1/games` 和 `season_type`。验收使用自己的免费账户，分页读取 regular、preseason 和 NBA Cup 的 ist。引入杯赛前的历史范围只查前两个阶段。查询之间共享每 15.01 秒一次的预算。密钥写在部署机的 `~/.config/polymarket861-nba/providers.env`，也可以在执行命令的终端中导出。不要把密钥和原始业务数据放进审核仓库。[BALLDONTLIE 接口](https://www.balldontlie.io/openapi/nba.yml)

```bash
nba_dir="$HOME/.local/share/polymarket861-nba"
nba() { "$nba_dir/venv/bin/pm861" --data-dir "$nba_dir/state" nba "$@"; }
set -a
. "$HOME/.config/polymarket861-nba/providers.env"
set +a
nba data verify-schedule
nba data capabilities
```

| 检查项 | 报告中的证据 |
| --- | --- |
| 最近三个美东比赛日 | 从近两周有回执的实际比赛日中取最新三个，并额外查未来三天，以便核对已排期的时刻。输出具体比赛日、已排期分母和时刻缺失数。休赛期缺所需样本会报出缺项。可用 `--through YYYY-MM-DD` 指定参考日，接收时刻仍记实际执行时间。 |
| 10 月季前赛和揭幕周 | 查询当季 10 月 1–31 日。要求 regular、preseason 都有例证，两个集合没有交集，季前赛不能进入常规赛训练。 |
| 上赛季 12 月 NBA Cup 淘汰赛 | 按 12 月整月查询，要求响应里实际出现八强、半决赛或决赛标记，另记杯赛决赛。 |
| 参数、原件与隔离 | 按段给出各阶段分页行数、各页原件 SHA-256、入库见证时刻、扫描凭据 SHA-256、阶段冲突和隔离数。同场出现在多个阶段会使同步报 `source_quality`。确认互斥前不发布可训练的推断阶段。回放失败后下次完整实收产生新的可追踪版本。 |
| 时间与场地 | 已排期比赛需有带时区且与美东比赛日相符的分钟时刻。已结束比赛可用于日级历史比分回测。报告分别统计来源是否给出赛地布尔值、模型使用挂牌主场/来源确认/公开中立场名单的场数。近期常规赛主场项取实际最近三个比赛日，列出分母和占比。 |
| 延期与改期 | 延期、延迟或暂停有独立状态。逐场改期须有同一比赛、相同主客队的两份排期，且每份均在当时记录的开赛前收到并完成入库。输出旧、新开赛时刻，两次收件及入库见证时刻和各自的原件摘要。赛后重发旧赛程只作历史记录。无实例时输出 `not_observed` 并保持验收待办。 |

如已知某天有延期或改期，可运行 `nba data verify-schedule --exception-day YYYY-MM-DD`，多个日期可重复传入。变化发生后才安装的实例无法凭单份历史终态重建原排期。已有普通同步保留的赛前版本可作为改期证据。三段各项齐全且有异常赛事实例后，命令返回 0；密钥缺失、过滤异常、关键样本缺失或例证仍待观察均返回 1。每次完整的认证验收摘要按实际执行时间归档。NBA 专用 SQLite 的 `nba_game_blobs` 保留私有原件，`nba_schedule_evidence` 的请求清单可按摘要追到具体分页，`nba_schedule_acceptance` 可重读每项验收。有认证业务原件后可在本机将对应源行作为私有回归夹具，供审批方查看字段和解析结果。

旧版收件缺少跨阶段验证标记。新版本读取时会显示 `legacy_requires_rescan`。重新执行赛程同步或历史预热即可为同一比赛留下已验证的新版本。既有球队预测的原始封存仍可审计。

## 七个比赛日的伤停来源证据

安装并启用随包的用户服务。调度器每分钟检查一次，平时不发 HTTP 请求。比赛日和每日限额使用 America/New_York，夏令时随时区数据库切换。

```bash
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-nba-sources.timer
systemctl --user list-timers 'polymarket861-nba-*'
nba data probe-due
nba data capabilities
```

| 来源 | 采集与本地预算 | 逐日核验 |
| --- | --- | --- |
| Big Balls | 当日第一场常规赛的 T−90、T−60、T−15 各取一次全联盟当前伤停的首屏。每个窗口从截止前五分钟打开。每个美东日最多三次，人工探针也占同一预算。 | 每个时点有独立请求编号、收件时刻、入库见证时刻、原件及排序无关的内容摘要、可识别球员状态行数。记录三个窗口的实际覆盖。 |
| DunkAPI | 有赛时首选第一场的 T−60，每美东日最多一次，全联盟伤停查询为 2 点。错过时留单独的每日回执；无可靠赛程时在 21:00 美东以后取每日样本。一个 31 天月最多 62 点。 | 当天返回的 `date` 是来源报告日。当天收到的昨日快照只能证明 HTTP 可用。来源说明承认没有逐条发布时间。 |
| 额度与故障 | 单来源独立限额账本；429 遵守 Retry-After，402 暂停 31 天；不升级付费。已经预留的同一比赛日同一时点在重启后不会重发。 | 失败、空表、超时、重复运行和截止后才落盘均保留原状。其他来源继续按各自预算执行。 |

外层响应和排序无关的内容摘要用于判断“重新取到了相同内容”与“内容确实更新”。伤停探针只保留字段清单、计数、来源时钟统计和摘要，原始球员标识、姓名及伤情描述不落入探针表。每个样本只归属美东当日首场，不会复制给较晚场次。查看 `admission.sources.<来源>.days`，其中包括每份回执离 T−60 的秒数，以及缺收的比赛日。[Big Balls 免费配额](https://bigballsdata.com/nba-api) · [DunkAPI 伤停接口](https://dunkapi.com/docs/get-injuries) · [DunkAPI 时间说明](https://dunkapi.com/docs/timing-and-rights)

## 负责人确认的材料

请将每个来源的条款页面、能支持上游身份和时间语义的文档保存在部署机上。以下路径和身份是占位示例。根据实际条款选择 `allowed`、`denied` 或 `unverified`。不清楚上游时可写 `unknown` 并略去 `--upstream-file`。时间字段含义未经验证时略去最后两个参数。

```bash
nba data record-terms --source dunkapi --reviewer "负责人姓名" \
  --terms-file /path/to/dunkapi-terms.html \
  --upstream "文件中证实的上游名称" --upstream-file /path/to/upstream-evidence.html \
  --prediction allowed --retention allowed \
  --timing-field date --timing-file /path/to/dunkapi-timing.html
nba data capabilities
```

程序在本机归档文件内容和 SHA-256，记录来源、条款网址、审核人、审核时刻、预测及留存用途和已核实的时间字段。修订审核会追加一份记录。为 DunkAPI 审核 `date` 时只匹配响应里的 `date`。为 Big Balls 审核 `data.snapshot_date` 时只匹配真实该路径，`date` 和 `as_of_date` 各要独立审核。行级时间可选择 `data.injuries[].reported_at` 等路径，只汇总含可识别球员状态的行。响应明示其他联赛、项目或无法识别的范围时隔离该次收件。日期、上游时间、本机收件时间和本机完成持久化的时间分别呈现。[Big Balls 条款](https://bigballsdata.com/legal/terms) · [DunkAPI 条款](https://dunkapi.com/terms)

`seven_day_coverage` 按每家来源单独计算最近七个已到 T−60 的比赛日。每一天都须由该来源在首场 T−60 前 15 分钟内收到含可识别球员状态的本次回执，并在截止前完成持久化。`seven_day_all_windows` 另检查已经走完全部窗口的最近七个比赛日。`source_verified` 同时要求窗口覆盖、经核实的当天来源状态、具名预测/留存权及已知上游。逐日检查两个参赛日中间的每个美东日。只有比分源整日扫描及完整分页的原生盘口目录都覆盖整个美东日期，才略去休赛日。同场两个开赛时刻相差超过 30 分钟，或窗口截止时已达到盘口开赛时刻、已出现比赛开始的观测，相关样本不能作为赛前证据。只有盘口列出比赛、或整日没有日历证据，都会阻断连续七日。不同来源的天数不能相加。`eligible_player_sources` 是可进入下一阶段人工评估的来源；训练和发布 N3/N4 另有模型评估门槛。

随仓的两份执行结果来自当前环境，三个免费密钥均未配置，已认证业务回执为零。离线系统测试的七日样例用于验证计数逻辑，不作为部署证据。[三段验收结果](../../reports/nba-phase1/a2-sources/schedule-acceptance.json) · [来源能力结果](../../reports/nba-phase1/a2-sources/source-capabilities.json)
