# 审核证据

本目录是[审核与交接文档](../polymarket861-EPL-review-handoff.md)附录 B 的可复现材料，不属于系统代码。

- `score_noise_check.py`：按蓝图公式验算 0–100 分的区分度和噪声，内含 85.69 分算例、滚动 Dixon–Coles 和防泄漏检查。
- `score_noise_results.json`：2026-11-25 运行得到的全部数值。

数据来自 [openfootball/football.json](https://github.com/openfootball/football.json)（公共领域），需自行检出，本仓库不含数据。运行方法：

```
python3 -I docs/review-evidence/score_noise_check.py <football.json 检出目录> --out out.json
```
