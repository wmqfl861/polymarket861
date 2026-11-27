# 英超采集器 SQLite 写锁修复交接

修复包为 `0.5.0a3+eplfix.1`，基线为 `0.5.0a3` 的 `11a00f0`。英超实例于 2026 年 11 月 26 日 23:15 UTC 切换。文中的测试、比赛窗口和来源覆盖均对应这次验收。[运行代码提交](https://github.com/wmqfl861/polymarket861/commit/4ae8aa963e3fbf2589650a5d36c9ce60089fdedf) · [验收数据和五季签核记录](../reports/five-leagues-0.5.0a3/epl-sqlite-hotfix.json)

## 故障和修复

`pm861 season run --repeat` 的多个任务和每分钟运行的看门狗共用一个 SQLite WAL 数据库。旧版在 `BEGIN IMMEDIATE` 之后读取、解压并校验 Gamma 市场目录，再解析 JSON。一次现场样本涉及 210 份目录，解压后约 1.13 GB。期间其他任务写入时报 `database is locked`。11 月 26 日 19:55–20:00 UTC，诺丁汉森林对切尔西的 T−24h 窗口内，预测续租线程在 19:58:57 遇锁。该窗口保留为 `missed`，原始原因是 `elapsed without a timely seal`。

- 赛程预检、来源同步和人工复核在一致的 WAL 读快照中校验原件。写入时复核相关表的版本、原件身份和证据生效时间。插入、更新、删除、替换写入及原件缺失后恢复都能触发复核。[赛程核验](../src/polymarket861/schedule_consensus.py) · [原件核验](../src/polymarket861/dbscope.py)
- 固定时点的 B1 预测在最终封存前检查赛程、证据时效和截止时间。快照失效时先释放写锁，再读取证据。两个任务竞争同一个窗口时，后到者可以取回已完成的封存。[预测封存](../src/polymarket861/pipeline.py)
- 新闻重算按场在开工时领取租约。发布前复核租约归属、截止时间、赛果、新闻版本和上一份预测摘要。重复出现的压缩目录仍校验请求收据和实际字节。[新闻重算](../src/polymarket861/news_events.py) · [市场收据](../src/polymarket861/archive_proof.py)

发布前还复核了赛程模块新增的英超表和触发器。已部署配方通过 `require_fixed` 执行固定窗口的准入检查。重开读事务的 `refresh_fixed` 和安装证据版本触发器的 `prepare_evidence_version` 由仓库 CI 额外核对语法树摘要。今后若改变它们的准入行为，应将改变纳入正式配方并重新做五季比较。[配方依赖审核](../scripts/check_recipe_references.py) · [已复核的实现摘要](../scripts/recipe-reference-policy.json)

## 已验证的结果

| 检查 | 结果 |
| --- | --- |
| 本机回归 | 定向 48/48 通过。全量 697 项中 694 项通过，3 项可选 Chromium 测试跳过。 |
| 并发写入 | 在生产库副本运行赛程就绪检查和来源同步时，独立连接分别提交 226 次和 110 次。共 336 次提交，0 次锁错误，最长一次 0.9077 秒。 |
| 同输入回放 | 第 12 轮 10 场赛程报告在冷、热缓存下逐字段一致。五季 1,900 场、190 轮的预测和训练视图各有 0 场变化。五季档案缺少逐场同期新闻和首发收据。 |
| 上线观测 | 23:15:48–23:52:06 UTC，看门狗 36 次均取得新鲜心跳，日志中写锁错误为 0。实际完成预测调度 65 次、新闻 10 次、历史回补 10 次、市场目录 2 次、赛果 2 次。 |
| 历史和数据缺口 | 第 12 轮就绪检查通过，10 场开球时间获确认，10 份原预测的内容和摘要均复核通过。两次赛果检查都隔离了伯恩茅斯对布伦特福德的旧比分冲突，两份来源分别报 2–1 与 2–2。 |

本轮 20 队都有名单，二手来源已归档 84 条伤病和 6 条停赛声明。伤停流最近一次轮询有 14 队完整、6 队部分完成。10 场比赛有 12 次新闻研究预测修订。验收时启用的俱乐部官网一手 RSS 为 0，收到的正式首发为 0。新闻研究修订与 T−24h、T−60m 固定封存各自计数。[球队新闻及预测口径](TEAM-NEWS.md)

验收时登记的下一批 T−24h 是 11 月 27 日 14:55–15:00 UTC 的利兹联对考文垂、曼联对布伦特福德。森林对切尔西的 T−60m 窗口于当日 18:55 UTC 开启。接手时用当期 `season readiness` 核对最新窗口。

## 在已有实例切换和回退

保留两套独立安装。`OLD_PM`、`NEW_PM` 指向各自的 `pm861` 可执行文件，`STATE` 是同一个实例的数据目录，`HISTORY` 是含固定五季及所需训练季的历史目录，`BACKUP` 是新备份目录。选在固定窗口以外操作，停止该实例的值守及看门狗，再核验备份和原有预测摘要。

```sh
"$OLD_PM" --data-dir "$STATE" backup create "$BACKUP" --compress
"$OLD_PM" --data-dir "$STATE" backup verify "$BACKUP"
"$OLD_PM" --data-dir "$STATE" forecast release-baseline "$HISTORY"
"$NEW_PM" --data-dir "$STATE" forecast release-compare "$HISTORY"
```

核对配对数、逐场预测和训练视图，再把**本实例**实际生成的摘要、审核人和理由分别填入 `REPORT_SHA256`、`REVIEWER`、`REASON`。切换时让英超值守、看门狗和仪表盘使用同一套已批准安装，再恢复调度。

```sh
"$NEW_PM" --data-dir "$STATE" forecast release-approve "$REPORT_SHA256" --reviewer "$REVIEWER" --reason "$REASON"
"$NEW_PM" --data-dir "$STATE" forecast release-report "$REPORT_SHA256"
"$NEW_PM" --data-dir "$STATE" season readiness --year 2026 --round 12
"$NEW_PM" --data-dir "$STATE" news status
```

上线后检查活动配方和安装配方的 SHA-256 是否相同，按切换前保存的摘要取回封存。以新进程启动时间为起点，核对看门狗日志和 `season_services.updated`，直到新闻、历史回补、预测、目录和赛果都出现新完成记录。目录的正常刷新间隔为 30 分钟，赛果为 15 分钟。

首次启动时，其他采集线程可能在三次赛程核验中先后提交新证据。此时预测任务记录 `schedule evidence changed repeatedly before the fixed B1 decision`，保留 `forecast_dispatch.available` 指定的重试时间。单次 `season run` 完成后返回，常驻模式会在下一次预测轮询重新核验。比赛日集成测试会推进到实际登记的下次轮询时间，并检查截止前恰好生成一份正式封存。[比赛日集成测试](../tests/test_season_operations.py)

已批准后需要回退时，先暂停调度并备份当时的状态，用现用安装冻结基准，再用旧安装在**同一数据目录**进行反向五季对照。审核并具名批准反向报告，核对旧安装就绪状态和已有封存后切回服务。此流程保留升级期间新收到的原件和预测。[版本签核及回退命令](model-release-governance.md)

## 英超服务的 DNS 配置

本次部署同时发现主机默认解析使部分 Polymarket 请求超时。对于以系统级 systemd 单元运行、且 `/etc/resolv.conf` 指向 `/run/systemd/resolve/stub-resolv.conf` 的部署，仓库提供了当时使用的可选配置。它把整个解析器目录只读挂入英超单元，从而能承受宿主机原子替换 stub 文件。验收时该单元使用 1.1.1.1 和 8.8.8.8，宿主机使用 127.0.0.53。[配置与 HTTPS 探测工具](../deploy/examples/epl-resolver/)

以下命令从仓库根目录执行。先确认系统单元及解析器布局。示例把 drop-in 安装到该实例原有的文件名。服务在下次启动时取得配置，可并入上面的版本切换。服务已经运行时，应用配置需要重启对应英超单元。

```sh
readlink -f /etc/resolv.conf
systemctl cat polymarket861-season.service
sudo install -D -m 0644 deploy/examples/epl-resolver/stub-resolv.conf /etc/polymarket861/epl-resolver/stub-resolv.conf
sudo install -D -m 0644 deploy/examples/epl-resolver/60-wikimedia-resolution.conf /etc/systemd/system/polymarket861-season.service.d/60-wikimedia-resolution.conf
sudo systemctl daemon-reload
```

启动后用实际英超进程的挂载命名空间探测。脚本保留 TLS 域名校验，依次访问 Gamma、CLOB、Data API、Wikipedia、Wikimedia、OpenLigaDB 和 Touchline Wire，对每个端点输出 HTTP 状态，任一失败时返回非零退出码。此次上线初期与验收结束前，两轮均为 7/7 返回 200。

```sh
epl_pid=$(systemctl show --value -p MainPID polymarket861-season.service)
probe=$(realpath deploy/examples/epl-resolver/verify.py)
sudo nsenter --target "$epl_pid" --mount -- /usr/bin/python3 -B "$probe"
```

## 可复制的维护者提示词

> 请复核英超 SQLite 写锁修复 `4ae8aa9`。在相同生产数据的副本上，同时运行赛程核验和独立写入，记录写入次数、最大延迟与锁错误。检查原件变动、替换写入、未来证据生效、新闻租约过期和固定窗口截止时的行为。运行仓库 CI 和完整测试，对比固定五季的逐场概率及训练视图。部署时保存旧环境和封存摘要，在本实例完成五季签核。上线后从数据库和日志实证新闻、赛果、目录、历史回补和心跳持续推进。保留森林对切尔西的漏封记录，并单列上游比分冲突、部分伤停覆盖和实际收到的首发。
