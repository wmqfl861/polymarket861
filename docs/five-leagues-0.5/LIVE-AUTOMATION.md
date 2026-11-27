# 五大联赛自动预测、新闻与球员资料交接

以下为 0.5.0a3+fiveauto.1 部署当天的实收记录。安装 0.5.0a4、配置新的调度看门狗和核对英超配方，使用[新版升级指南](DEPLOY-0.5.0a4.md)。当前赛季的 API-Football 不在免费层，最新系统会显示该缺项。另见[新版来源核查与评测](CLAUDE-REVIEW-RESPONSE-0.5.0a4.md)。

本次部署使用 `0.5.0a3+fiveauto.1`。本机看板的「五大联赛」显示未来七天的比赛、最近一次预测和封存时点。点选一场可以查看主胜、平局、客胜，以及球队新闻和球员资料。看板仅监听本机回环地址。[五大联赛看板](http://127.0.0.1:8765/leagues)

## 启用时的实收

2026-11-27 UTC，本机以同一未来七天为范围取得 50 场赛前预测。名单栏统计当前赛季每队最近一次收到的名单。新闻栏按联赛在本地 GDELT 首批索引中去重，同一条报道可以提到多队或跨联赛。球员人数按各俱乐部名单行相加。[启用时的验收数据](../../reports/five-leagues-0.5.0a3/live-automation.json)

| 联赛 | 七天内已有预测 | 取得名单的球队 | 名单人数 | GDELT 新闻链接 | 本次核实的人员资料 |
| --- | ---: | ---: | ---: | ---: | --- |
| 英超 | 10 | 20/20 | 554 | 24 | 第 12 轮已有 10 份提前预测。Touchline Wire 已收到 20 队的人员可用性记录，按单场及球员证据筛选。 |
| 西甲 | 12 | 20/20 | 559 | 8 | 10 队的名单上共有 19 条明确伤病标记，涉及的具体比赛有待核实。 |
| 德甲 | 9 | 18/18 | 517 | 3 | 多特蒙德 29 人有其效力本队期间在德甲的累计出场及进球，附来源统计日期。 |
| 意甲 | 10 | 20/20 | 587 | 2 | 取得当季一队名单，保留来源修订和本机收件时间。 |
| 法甲 | 9 | 18/18 | 499 | 14 | 取得当季一队名单，连同其依赖的名单模板修订留存。 |

首批新闻以 02:25 UTC 为本机收件截止，整批有 49 条不同的链接，涉及 22 支球队。把英超已有的球队新闻源也纳入后，02:43 UTC 的逐队检查共对 32 队查到消息，另 64 队已检查、尚无球队级消息。

人员资料附维基修订、署名和许可。名册增减需要当地先后收到两个可比较的修订，首次收件时只记录名单。GDELT 每 15 分钟收取一次索引，展示所匹配的俱乐部、来源域名、链接及索引时刻。来源未列出球员、没有匹配报道、请求受限和未配置供应商各有自己的状态。[Wikipedia 内容复用](https://www.mediawiki.org/wiki/Wikimedia_APIs/Content_reuse) · [GDELT 数据](https://www.gdeltproject.org/data.html) · [Tactical Data by Touchline Wire](https://touchlinewire.com/docs#attribution)

森林对切尔西的单场页面可以看到两队 24 人、30 人的名单。11 月 27 日 00:16 UTC 截止的快照中，分别有 3 条和 6 条适用于该场的 Touchline 人员报告，可从同一页面打开原预测。

## 赛前与赛后处理

英超使用原有赛季调度和按轮签署的 B1 配方。四联赛各自训练 B1，校验本联赛、本季、主客顺序、开球时间和是否仍处于赛前。输入包括九季固定 OpenFootball 文件、带本机收件时刻的当季比分修订及 Gamma 的赛程原件；西甲两场跨日期调整另有俱乐部赛季页的赛程修订佐证。四国按西、德、意、法顺序，投入运行时当季已收到的有效比分数为 129、90、120、99。[OpenFootball](https://github.com/openfootball/football.json) · [皇家社会对皇马的改期记录](https://en.wikipedia.org/w/index.php?oldid=1387899597) · [巴萨对马竞的改期记录](https://en.wikipedia.org/w/index.php?oldid=1388201732)

调度器收到可核实的赛程即生成提前预览。新闻或名单的实质变化可触发更新。开球前 24 小时及 60 分钟两个固定时点各有五分钟的执行窗口，封存前再核验比赛状态。已错过的窗口会保留在分母里。启用时西、德、意、法各有一场已过 T−24h，英超森林对切尔西也已错过该时点。赛后取得可信比分时，调度器才开始给已封存的预测评分，并保留更正或撤回记录。

四国的俱乐部名单或报道链接本身不触发 B1 概率调整。逐场缺阵报告经独立名单确认球员身份后，可额外展示注明「未校准」的伤停情景。四国的逐场伤停和确认首发连接使用 API-Football v3，本次启用时本机缺少该凭据。来源给出本场、双方球队和足够的球员记录后才会展示相应字段。正式前向纸面模拟仍按既有资格检查启动；本次上线时英超完整技术轮为 0，四国研究封存单独记录。[球员数据源](https://www.api-football.com/documentation-v3) · [正式模拟与发布规则](../model-release-governance.md)

## 本机运行和排障

英超的 `--execution-research` 也已启用，会在执行簿时段单独收取其他方向的研究盘口，优先服务有正式截止时间的请求。切换时旧进程的 90 秒停止时限触发了一次强制退出。既有十份英超封存已逐份验过。新单元预留五分钟让进行中的抓取和写入收尾，随后一次重启已正常退出，新进程于 02:51 UTC 启动。[执行簿采集](../EXECUTION-COLLECTION.md)

部署根为 `/mnt/d/polymarket861`。英超使用 `.pm861`，四国盘口归档使用 `.local-deploy/five-league-state`，四国预测、名单、五国新闻索引使用 `.local-deploy/five-automation/state`。查询时须选择相应的目录。

```sh
cd /mnt/d/polymarket861
PM=.local-deploy/releases/0.5.0a3+fiveauto.1-final/venv/bin/pm861
EPL=.pm861
FIVE=.local-deploy/five-automation/state
MARKET=.local-deploy/five-league-state
"$PM" --data-dir "$FIVE" big5-scheduler status
"$PM" --data-dir "$FIVE" leagues health --epl-data-dir "$EPL"
"$PM" --data-dir "$MARKET" market league-watchdog --rolling
"$PM" --data-dir "$EPL" season readiness --year 2026 --round 12
"$PM" --data-dir "$EPL" trial readiness
systemctl status polymarket861-season.service polymarket861-five-forecast.service polymarket861-five-watch.service polymarket861-dashboard.service
systemctl list-timers --all 'polymarket861-*'
journalctl -u polymarket861-five-forecast -u polymarket861-five-press -u polymarket861-five-watchdog --since today
```

对单场获取历次封存时，从状态结果复制完整的 `fixture_id`，执行 `"$PM" --data-dir "$FIVE" big5-scheduler status --league lal --fixture "$FIXTURE_ID"`。其中 `dispatch` 记录每个窗口的状态，`fixture_dispatch` 内的 `input_cutoff`、`sealed` 和 `digest` 可核对原预测；`fixture_results` 列出实收赛果和评分版本。英超的赛前取证、赛果与纸面就绪度使用既有 `season`、`news`、`trial` 命令。

预测和盘口服务常驻。新闻采集和看门狗由定时器启动，单次成功后显示 `inactive (dead)`，同时应有 `Result=success` 且对应定时器处于 `active (waiting)`。盘口与看门狗同时使用 `--rolling`，按 UTC 每天选择自 14 天前零时起、至第 8 天零时之前的赛程。过去发生过服务传入 `--rolling` 而运行的旧可执行文件仅接受起止时间，进程不断重启。修复包括成套更新两个单元指向的 wheel、支持滚动窗口、按联赛核对最新目录成功收件，并在 CI 运行安装包的 systemd 启动测试。[盘口服务说明](WATCH-SERVICE.md)

本机 API-Football 凭据如已获授权，可从权限为 `0600` 的 `/etc/polymarket861-five/player-api.env` 读取 `PM861_API_FOOTBALL_KEY`。保存后重启 `polymarket861-five-forecast.service` 即可从之后的轮询开始采集。供应商的产品权限及每日配额决定实收范围。密钥和收费数据存放在部署机。

## 复现部署和回退

现有英超实例升级前，用新安装核对 `season readiness` 的 `release.ready`、`active_recipe_sha256` 和 `installed_recipe_sha256`，并保留升级前的封存摘要。可用旧安装和新安装分别执行 `forecast get` 校验同一份封存。部署时将 `season` 和英超看门狗的 `ExecStart` 同时指向已校验的 wheel。看板的 `PM861_DATA_DIR` 指向英超库，`PM861_FIVE_STATE_DIR` 指向五联赛研究库。保留英超已有的网络解析配置。

在另一台已配置英超的 Linux 主机上，从需要部署的仓库提交安装四国用户服务的例子如下。退出用户会话后持续运行需要该机器启用 systemd 用户驻留。

```sh
FIVE_BASE="$HOME/.local/share/polymarket861-five"
USER_UNITS="$HOME/.config/systemd/user"
mkdir -p "$FIVE_BASE" "$USER_UNITS"
python3.12 -m venv "$FIVE_BASE/venv"
"$FIVE_BASE/venv/bin/python" -m pip install -c constraints/research.txt .
"$FIVE_BASE/venv/bin/pm861" leagues prepare-history "$FIVE_BASE/openfootball"
install -m 0644 deploy/polymarket861-five-*.service deploy/polymarket861-five-*.timer "$USER_UNITS/"
systemctl --user daemon-reload
systemctl --user enable --now polymarket861-five-watch.service polymarket861-five-watchdog.timer polymarket861-five-forecast.service polymarket861-five-press.timer
systemctl --user start polymarket861-five-press.service
```

本机采用系统级单元，开机后由 `ubuntu` 运行。回退前保留三组状态目录及后续原件。英超若旧安装的活动配方与当前库一致，恢复先前的 `ExecStart` 并复核封存即可；若配方已变，走同库反向五季比较和具名批准。四联赛可暂停新预测和新闻定时器以保留现场，盘口服务应使用支持滚动参数的已核安装。[英超回退步骤](../model-release-governance.md)

## 给下一位维护者的提示词

> 在 `/mnt/d/polymarket861` 接手五大联赛自动预测。先读本交接和验收 JSON，用实际 systemd 的可执行路径、七天比赛、逐队来源和收件核对五个联赛。英超检查已签 B1、固定时点和原预测摘要。西德意法分别检查七天预览、T−24h、T−60m、新证据更新、实收比分及修订。曾有旧 wheel 不识别滚动窗口、改期比赛时间冲突、名单解析时混入其他梯队、看板入口影响旧导航四类问题；对应复验是安装包的看门狗命令、两源主客与开球时间校验、按球队和一队章节定位的修订回放，以及浏览器从英超首页进入每个联赛的比赛。伤停报告须核实本场身份，确认首发须有原始阵容。逐场注明缺项、漏窗和实际采到的时刻。更新代码后跑回归、构建 wheel、核对源码和已安装文件，再检查服务、看板和远端提交。
