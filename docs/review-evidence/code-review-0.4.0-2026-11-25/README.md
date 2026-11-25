# 审核证据 · 0.4.0 · 2026-11-25

本目录是 [0.4.0 独立审核与优化建议](../../polymarket861-EPL-code-review-0.4.0.md)里“实测”结论的复现脚本和当次输出，不属于系统代码，也不修改任何仓库文件。审核对象是提交 `554a57c`（0.4.0），对照基线是 `12d1717`（0.3.0）。

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `check_live_example.py` | 由实采报告里森林对切尔西两组概率反解进球率和 ρ，再独立重算全部公开数字 | 只读 `reports/news-live-0.4.0.json`，不导入本项目的包 | 不到 1 秒 |
| `check_live_events.py` | 十场事件预测的两组概率各自都能由同一个比分分布重算，并得到每场的进球率调整量 | 同上 | 约 2 秒 |
| `probe_growth.py` | 状态没有任何变化时，伤停流每轮询一次仍为名单上每名球员新增一个版本、一个原件文件和一组数据库行（N1-1） | 直接调用 `news.sync` 和真实的 Touchline 适配器，响应是合成的 | 约 6 秒（模拟一天 72 次轮询） |
| `probe_kickoff_correction.py` | 占位开球时刻被更正后，更正前采到的官方消息不再进入这场比赛的预测（N0-1、N1-2） | 同上，另用 `news.add_feed` 登记一个合成的官方源 | 约 2 秒 |
| `probe_relevance.py` | 写明比赛日的句子在占位时刻下被丢弃；提到杯赛、友谊赛或国家队的句子一律被丢弃（N1-2、N1-4） | 只调用 `news.fixture_relevant` | 不到 1 秒 |
| `probe_wire_mapping.py` | 供应方每种状态最终变成多大的模型权重；一行无法识别会让整队这次采集失败；名单里找不到的球员不产生结论（N1-3、N2-3） | 调用适配器、随包识别器和 `news_model.POLICY` | 不到 1 秒 |
| `probe_power.py` | 在最有利的假设下，一个完整赛季得出“有新闻组更好”的概率（N2-1） | 读实采报告，调用 `football_model.score_probabilities` 和 `experiment_statistics.clustered_t` | 2,000 个模拟赛季约 45 秒 |
| `upgrade_replication.sh` | 0.3.0 库原地换成 0.4.0 后的报错，以及按文档顺序做完基准、对照、批准后的结果（N0-2，以及交接文档的升级对照结论） | 需要能访问 GitHub 以取得固定版本的 OpenFootball 文件；全部在临时目录里进行 | 约 45 秒 |

`probe_common.py` 是几个探针共用的夹具：可控时钟、合成的维基名单响应和合成的 Touchline 响应，形状与仓库自己的 `tests/test_news_roster.py`、`tests/test_news_sources.py` 一致。

`outputs/` 是 2026-11-25 在审核环境里运行得到的输出。`freshness-fresh-database.json` 是只执行了 `data refresh`、没有接入任何赛果来源的全新 0.4.0 数据目录上 `result-feed freshness 2026` 的摘要。`upgrade-replication.json` 是升级对照的两步输出。

运行方法（在仓库根目录，Python 3.11 以上，已安装 NumPy、SciPy、httpx）：

```sh
export PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src
E=docs/review-evidence/code-review-0.4.0-2026-11-25
python $E/check_live_example.py reports/news-live-0.4.0.json
python $E/check_live_events.py reports/news-live-0.4.0.json
python $E/probe_growth.py --listed 4 --polls 72
python $E/probe_kickoff_correction.py
python $E/probe_relevance.py
python $E/probe_wire_mapping.py
python $E/probe_power.py reports/news-live-0.4.0.json --seasons 2000
sh $E/upgrade_replication.sh "$PWD" "$(command -v python)" "$(mktemp -d)"
```

几点说明：

- 除 `upgrade_replication.sh` 外全部离线运行，数据写在临时目录。探针里的球员姓名都是虚构的，不对应任何真实球员的伤停状态。
- 审核环境的网络策略不允许访问 Touchline Wire、Wikimedia、OpenLigaDB 和 Polymarket，所以这些脚本没有一个访问线上接口，也无法重新采集随仓报告里的真实数据。
- `probe_growth.py` 的前提是供应方每次响应都带一个新的快照时间戳。依据是随仓报告里四份 Touchline 收件的快照时间都只比本机收件早 0.07 到 0.18 秒。这一点没有在线上复核。磁盘占用按文件表观大小统计，每次测量前先合并数据库的预写日志。
- `probe_power.py` 的模拟是随机的，种子固定为 861，重复运行结果相同。它假设有新闻组的概率就是真实概率，这是对新闻组最有利的假设。
- `upgrade_replication.sh` 里的具名批准只发生在脚本自己建立的临时目录。
