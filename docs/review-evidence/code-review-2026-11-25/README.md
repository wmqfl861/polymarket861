# 代码审核证据 · 2026-11-25

本目录是[代码审核与优化建议](../../polymarket861-EPL-code-review.md)里“实测”结论的复现脚本，不属于系统代码，也不修改任何仓库文件。审核对象是提交 `400022b`。

| 脚本 | 对应结论 | 依赖 |
|---|---|---|
| `rescoring_and_power.py` | 独立重算 3,800 行评分、校准斜率、按轮波动、检验功效、向 B0 收缩的留一季检验（附录 A、P1-3、P1-5） | 只读回测明细文件，不导入本项目的包 |
| `hyperparameter_sensitivity.py` | 16 组衰减和岭参数的五季回测对比（P1-5） | 导入 `src/` 里的回测，在内存里替换两个常量 |
| `bootstrap_coverage_sim.py` | 按轮重抽样分位数下界的误判率（P1-3） | 只用 NumPy |
| `capture_schedule_check.py` | 采样时刻能否落进收盘窗口（P0-5） | 直接调用 `market_archive.next_capture` |
| `decision_window_benchmark.py` | T−60 冻结循环的耗时随账本增长的变化（P0-4） | 复用 `tests/test_opportunity.py` 的合成夹具，数据写在临时目录 |

运行方法见审核文档附录 B。全部脚本离线运行，只有准备输入的 `pm861 football fetch` 需要访问 GitHub。

几点说明：

- `hyperparameter_sensitivity.py` 要重跑 16 次五季回测，在 4 核机器上约需 3 分钟，结果是确定的。
- `decision_window_benchmark.py` 的默认参数会预置最多 16,000 条意图，全程约 1 分钟。同一台机器两次运行的耗时相差在 20% 以内，绝对值与机器有关，随意图数线性增长这一点与机器无关。
- `bootstrap_coverage_sim.py` 是随机模拟，种子固定为 1，约需 20 秒。每个误判率有约 ±0.3 个百分点的抽样误差。
- 数据来自 [openfootball/football.json](https://github.com/openfootball/football.json)（公共领域），本仓库不含数据。
