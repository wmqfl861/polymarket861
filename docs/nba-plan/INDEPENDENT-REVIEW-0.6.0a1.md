# NBA 0.6.0a1 独立验收与 Claude 复审入口

2026-11-28。两名未参与实现的审核员使用 gpt-6.2-astra、max 推理，分别从冻结的 Git 树解包运行。只读研究软件通过，现役服务和账户继续按原部署运行。

两位审核员签署的本地源码提交为 `c8514b7`。GitHub 源码提交为 [ccdf4a8](https://github.com/wmqfl861/polymarket861/commit/ccdf4a8265ddd5d6862263271d09c9ea0b2393da)。两者由相同的 Git 树 `31ae699a41c02b60b181d4609a533015e52c0bbe` 生成，可用 `git rev-parse '<提交>^{tree}'` 核对全部受控文件与执行位。随后一笔提交仅附审核文档。

| 复审入口 | 可以核对的问题 |
|---|---|
| [数据、模型和评测独立报告](INDEPENDENT-DATA-0.6.0a1.md) | 赛程修订和同刻矛盾、落盘时钟、三层模型和四类分数、逐字节历史回放，以及哪些结果只能算技术验证。 |
| [市场、隔离和系统独立报告](INDEPENDENT-SYSTEM-0.6.0a1.md) | 合约方向、规则时限、期满与改期、收盘时钟、旧足球 wheel 的误指、五季足球预测兼容。 |
| [生效开发计划](REVIEWED-DEVELOPMENT.md) · [统计协议](VALIDATION.md) | Claude 的 X0–X4 逐项采纳、分阶段交付、新闻的四终点比较和样本规划。 |
| [安装、使用和现场验收](OPERATIONS-0.6.0a1.md) · [足球变更](../five-leagues-0.5/REVIEW-0.5.0a4-FOLLOWUP.md) | 独立安装、定时任务、查看比赛及评分、免费来源缺口及旧账单据保全。 |
| [历史与真网样本](../../reports/nba-phase1/) | 固定文件两次回放、交易所实际收件的哈希摘要和三个免费账户的本机能力表。 |

## 本地全仓验收

最终代码在 CPython 3.11、3.12、3.13 各运行 887 项测试，每组 886 项通过、1 项因宿主限制 Unix 数据报套接字而跳过。70 项 NBA 专项均通过。三种解释器的八个足球配方摘要一致，100 项足球部署脚本绑定、Ruff 与指定模块的严格 mypy 检查通过。真实盘口探针与安装包使用受控上游数据的验收分开记录。Ubuntu 原生用户服务与套接字的实际运行见本 PR 的 CI 检查。

## 复现入口

在项目 venv 中安装仓库与开发工具后执行以下检查。在已安装 headless Chromium 的主机设好 PM861_TEST_CHROMIUM 以运行浏览器测试。完整矩阵和 Linux 用户服务安装由仓库 Actions 工作流运行。

```bash
python -m unittest discover -s tests -p 'test_nba*.py' -v
python -m unittest discover -s tests -v
python scripts/check_league_boundaries.py
python scripts/check_recipe_references.py
python scripts/check_recipe_identity.py --approved scripts/football-recipe-baseline.json
python scripts/check_paper_bindings.py
ruff check src tests scripts deploy
```

部署流程在临时 Linux 用户管理器安装真实 wheel，单独启动盘口、赛程、预测、健康服务并检查定时器。测试对上游网络使用受控响应；真网盘口证据和该流程分开归档。无密钥现场不能完成现代赛季回测、球员时效核验及新闻盲标；这三项在本批证据中保留缺口。

两份报告的临时探针名称为审计留痕。仓库可复跑的反例在 [盘口](../../tests/test_nba_markets.py)、[赛程](../../tests/test_nba_data.py)、[封存与评分](../../tests/test_nba_operations.py) 和 [跨版本隔离](../../tests/test_nba_isolation.py)。第三方原始大响应与部署机私有回执仍由采集方持有。
