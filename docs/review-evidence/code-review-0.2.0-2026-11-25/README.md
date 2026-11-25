# 复审证据 · 0.2.0 · 2026-11-25

本目录是[0.2.0 复审与优化建议](../../polymarket861-EPL-code-review-0.2.0.md)里“实测”结论的复现脚本，不属于系统代码，也不修改任何仓库文件。复审对象是提交 `432e0af`。上次审核的脚本在[相邻目录](../code-review-2026-11-25/)，其中三个在这次复审里原样重跑过，用来比较 0.1.0 和 0.2.0。

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `placeholder_rounds_check.py` | 赛季文件里从第 10 轮起是整轮占位的开球时刻（R0-1、R1-2） | 只读 OpenFootball 赛季文件，不导入本项目的包 | 不到 1 秒 |
| `paper_slot_summary_check.py` | 调度进程给一次成功的纸面决策留下的摘要里，时间和影子账户的笔数都是空的（R0-2） | 复用上次的 `decision_window_benchmark.py` 夹具 | 约 4 秒 |
| `shadow_moving_clock.py` | 影子账户在会前进的时钟下，冻结和持有的结果与固定时钟一致（R0-2、P0-3） | 复用 `tests/test_shadow_settlement.py` 的夹具 | 约 2 秒 |
| `paper_failure_alert_check.py` | 两个账户的处理函数都抛异常时，决策时段记为完成，没有告警（R0-3） | 直接调用 `season.Coordinator._finish` | 约 1 秒 |
| `shadow_budget_order.py` | 同一场景正式账户冻结 6 笔，影子账户按合约编号顺序冻结前 3 笔（R1-1） | 复用上次的夹具 | 约 4 秒 |
| `schedule_shift_check.py` | 过去五季里有多少比赛的真实开球时刻离占位超过 36 小时（R1-2） | 只读 OpenFootball 赛季文件 | 不到 1 秒 |
| `cluster_t_interval_check.py` | 独立重算按轮聚类的 t 区间，测误判率和功效，说明只改动少数几轮的候选通不过（P1-3、R1-4） | 只读回测明细文件；给出仓库根目录时与产品函数比对 | 约 2 秒 |
| `coordinator_tick_scaling.py` | 调度主循环的耗时随累积的旧记录线性增长，加一个部分索引后回到基线（4.3 节） | 直接调用 `season._plan` 和 `Coordinator.tick`，不联网 | 约 35 秒 |
| `shadow_window_benchmark.py` | 影子账户一场比赛的处理耗时和持有写锁的时间（4.3 节） | 复用上次的夹具 | 约 16 秒 |

运行方法见复审文档附录 A。全部脚本离线运行，数据写在临时目录，只有准备输入的 `pm861 football fetch` 需要访问 GitHub。运行时建议设 `PYTHONDONTWRITEBYTECODE=1`，避免在仓库里留下缓存文件。

几点说明：

- 耗时类脚本的绝对值与机器有关，同一台机器多次运行可以相差一倍。结论依据的是量级和随记录数增长的趋势。
- `cluster_t_interval_check.py` 的模拟部分是随机的，种子固定为 1，重复运行结果相同。
- `coordinator_tick_scaling.py` 预置的行数按产品自己的采样计划推算，不是真实运行目录里量出来的，真实数字要等第一轮跑完再核对。
- `schedule_shift_check.py` 的占位规则是本脚本的假设：取每轮比赛最多的那一天，周六用 15:00，其他日子用当天最常见的时刻。它与 2026/27 赛季文件里看到的占位方式一致。
- 数据来自 [openfootball/football.json](https://github.com/openfootball/football.json)（公共领域），本仓库不含数据。
