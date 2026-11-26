# 审核证据 · 0.4.1 · 2026-11-26

本目录是 [0.4.1 独立审核与优化建议](../../polymarket861-EPL-code-review-0.4.1.md)里“实测”结论的复现脚本和当次输出，不属于系统代码，也不修改任何仓库文件。审核对象是提交 `ea58105`（0.4.1），对照基线是 `554a57c`（0.4.0）。

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `check_published_probabilities.py` | 两份概率报告共 1,200 个数，都能由公开的主客进球率和 ρ 在独立写的比分格上重算；并列出每场两组的差 | 只读两份 `reports/` 报告，不导入本项目的包 | 不到 1 秒 |
| `upgrade_replication.sh` | 0.4.0 的库原地换成 0.4.1 后，`season readiness` 在窗口之前就给出待批准和具体步骤；按手册顺序做完基准、对照、批准后的结果（R0-1，以及交接文档的升级对照结论） | 需要能访问 GitHub 以取得固定版本的 OpenFootball 文件；全部在临时目录里进行 | 约 45 秒 |
| `probe_usage_directory.py` | 赛果被再次确认后，球员出场目录不可用，全队回退到权重 1.0，12 小时一次的复查连续返回“未变化”，满 6 天才恢复；比分被隔离、有首发记录的球员离队时同样回退；真实对账路径上主来源晚到会追加同比分的新版本（R1-1、R1-2） | 直接使用仓库自己的测试夹具 `tests/test_news_importance.py` 和 `tests/test_result_identity.py` | 约 3 秒 |
| `probe_fixed_seal_schedule.py` | 仍在占位时刻上的比赛，定时封存要求两个来源各有 24 小时内的回执；缺口在窗口打开前不出现在预测服务里（R1-5） | 使用 `tests/test_season_readiness.py` 的夹具 | 约 3 秒 |
| `probe_power.py` | 研究计划的两个指标在新闻一组最有利情形下的检出率；与回归斜率的对比（R2-1） | 读 `reports/news-early-seals-0.4.1.json`，调用 `football_model` 和 `experiment_statistics` | 约 7 秒 |
| `probe_absorption.py` | B1 自己吸收一次长期缺阵的速度，与新闻政策里 42 天归零的时长折扣对比（R2-2） | 调用 `football_model.fit_team_model`，数据是合成的期望进球 | 约 1 秒 |
| `probe_storage.py` | 状态不变的轮询每次仍写多少、写在哪几张表（R3-1） | 调用 `news.sync` 和真实的 Touchline 适配器，响应是合成的；共用 0.4.0 证据目录的 `probe_common.py` | 约 7 秒 |

`outputs/` 是 2026-11-26 在审核环境里运行得到的输出，文件名与脚本对应。另有：

- `environment-and-tests.txt`：三个 Python 版本的测试结果、Ruff、严格 mypy、空白检查、独立发布审核所签树对象与公开提交的对应、GitHub Actions 结果，以及自建 wheel 与源码的逐字节比对、三份随仓报告的重跑比对、开发方复算脚本的结果和新增外联主机的清点。
- `upgrade-replication.txt`：升级对照脚本的全部输出。
- `rerun-0.4.0-probe-growth.json`、`rerun-0.4.0-probe-kickoff-correction.json`、`rerun-0.4.0-probe-relevance.json`、`rerun-0.4.0-probe-power.json`：上一次审核的四个探针在 0.4.1 上原样重跑的结果，用来核对 N1-1、N1-2、N1-4 的修复和交接文档引用的 1.0%。上一次的 `probe_wire_mapping.py` 走的是 0.4.1 已不再使用的文字识别通路，没有沿用。

运行方法（在仓库根目录，Python 3.11 以上，已安装 NumPy、SciPy、httpx）：

```sh
export PYTHONDONTWRITEBYTECODE=1
E=docs/review-evidence/code-review-0.4.1-2026-11-26
python $E/check_published_probabilities.py reports/news-early-seals-0.4.1.json reports/news-event-probabilities-0.4.1.json
PYTHONPATH=src:tests python $E/probe_usage_directory.py
PYTHONPATH=src:tests python $E/probe_fixed_seal_schedule.py
PYTHONPATH=src python $E/probe_power.py reports/news-early-seals-0.4.1.json
PYTHONPATH=src python $E/probe_absorption.py
PYTHONPATH=src:docs/review-evidence/code-review-0.4.0-2026-11-25 python $E/probe_storage.py
sh $E/upgrade_replication.sh "$PWD" "$(command -v python)" "$(mktemp -d)"
```

几点说明：

- 除 `upgrade_replication.sh` 外全部离线运行，数据写在临时目录。探针里的球员和球队多数是仓库测试夹具里的虚构名字，不对应任何真实球员的伤停状态。
- 审核环境的网络策略不允许访问 Touchline Wire、Wikimedia、OpenLigaDB 和 Polymarket，所以这些脚本没有一个访问线上接口，也无法重新采集随仓报告里的真实数据。
- `probe_usage_directory.py` 第一部分里“同比分再确认”是直接调用 `store.record_result_db` 追加的，写法与 `result_admission._result` 相同；第二部分用真实的对账路径证明这种追加确实会发生。
- `probe_power.py` 的模拟是随机的，种子固定为 861，重复运行结果相同。模型与收盘价之间的常见差距（3、5、8 个百分点）是假设，仓库里还没有收盘价样本可以校正它。
- `probe_absorption.py` 用期望进球代替真实比分，所以没有抽样噪声，测的是 B1 的拟合方法本身对一次持续变化的响应。
- `probe_storage.py` 用 SQLite 的 `dbstat` 把增长归到各张表，每次测量前先合并预写日志。外推假设 60 个默认流都像伤停流这样增长，另外两类流实际略小。
- `upgrade_replication.sh` 里的具名批准只发生在脚本自己建立的临时目录。
