# 0.5.0a4 独立发布复审

三位未参与实现的复审者均使用 `gpt-6.2-astra`、`max`，对同一冻结版本给出了软件发布通过的结论。起点是已经部署过的 `c613a70`，包含部署后的小修和四国自动预测，Claude 审核文件来自 `ac64dab`。具名生产签核、服务切换和下一次比赛窗口由部署机留证。[原始审核](../polymarket861-five-leagues-code-review-0.5.0a3-fiveauto.1.md) · [逐条修复](CLAUDE-REVIEW-RESPONSE-0.5.0a4.md) · [部署和回退](DEPLOY-0.5.0a4.md)

| 独立复审 | 核验结果 |
| --- | --- |
| 全项目和旧库 | 旧四国数据库里的 118 份预测和 118 条原评分逐字节保留。按每场、每时点评分后，最高新闻频率的 115 次重复封存转为轻量证据。8 个工作线程并发时，两次实际概率变化各落一份预测。实际进程单国失败、恢复、被杀及正常停止均有可辨别的状态。[独立记录](../../reports/five-leagues-0.5.0a4/independent-overall.json) |
| 四国数据、新闻与伤停 | 56 项专项全通过。11 个确定种子下 11,880 次缓存与全量重放结果相同。旧版真实结构的新闻库可直接读取。名单中列为替补的球员被排除，常规门将须在包含缺名单场次的完整分母上首发过半。改期在原开球前收到能封存，原开球后收到又缺少提前取消凭据则搁置。[数据结果](../../reports/five-leagues-0.5.0a4/independent-data.json) · [扩展重放](../../reports/five-leagues-0.5.0a4/independent-data-replays.json) · [复现脚本](../review-evidence/five-leagues-0.5.0a4/verify_domestic_data.py) |
| 英超、费用与协议 | 75 项专项全通过。13 组逐场阶段对照通过，另核对了迟到收件、身份冲突、改期和预计首发的隔离。8 个并发采集器只产生 2 次 HTTP 请求和 1 条观测。正式和技术两条路径分别对照有、无赛前 22 人时的完整预测封存，逐字节相同。合成有费用的 8 笔交易，正式档 0 笔成交、研究档 8 笔。新闻 v2 在全新数据目录预检后仍为未登记。[独立记录](../../reports/five-leagues-0.5.0a4/independent-protocol.json) |

英超正向升级和反向回退，各自完整比较 2021/22–2025/26 五季 1,900 场、190 轮，胜平负、全场和主客单队大小以及训练输入各 0 处变化。两份真实历史比赛的技术预测和一份受控比赛的正式 T−60m 预测可以由新旧安装分别读回。B1 准入摘要从 `8dc2c6c2…` 改为 `e5219639…`，在隔离库中用真实命令验证了未批准新版时拒绝新正式封存，批准后可用旧版反向比较并回退。[安装包验收和完整摘要](../../reports/five-leagues-0.5.0a4/release-wheel.json)

## 验证对象与运行

本地冻结提交为 `769208cceed8a0b0fe31873ec8bbd8be77617f76`。GitHub 上具有相同完整树 `6088cc7b264e2549486831bab9b09e6cdcf4a598` 的候选提交为 `910e6f0613930e9cce6aa8139f641c5223cca03e`。发布分支随后只补充复审报告、复现实验和部署说明。以下对象可在发布检出中逐项核对。[候选提交](https://github.com/wmqfl861/polymarket861/commit/910e6f0613930e9cce6aa8139f641c5223cca03e) · [逐项 Git 对象、测试与证据摘要](../../reports/five-leagues-0.5.0a4/release-validation.json)

| 对象 | Git 标识 |
| --- | --- |
| `src` | `435a2d7410591c6790ea3344f8f72eebb0fcebd2` |
| `tests` | `92ef024f5172bd54fa076cd9caa69358997035b0` |
| `scripts` | `61e98de8799e6957a76c5580738a5d8386f2d3f7` |
| `.github` | `6598c48ce4e969e05c02d94d6cd522d0ef6043a3` |
| `deploy` | `d727867d5f43f2dbb5f0ea31915fa3b3db13442a` |

本地 Python 3.11、3.12、3.13 各发现 800 项，799 项通过。宿主禁止 Unix 域数据报，各跳过 1 项。GitHub 的 Ubuntu 同一三个版本各 800 项全通过，跨解释器配方一致。安装 wheel 后在 Ubuntu 的 systemd 用户管理器中分别启动四国盘口与预测服务。模拟上游故障，验证独立定时检查失败、健康联赛继续、杀进程后自动恢复、暂停进程时看门狗触发重启，最后正常停止。CI 将盘口和预测服务看门狗分别加速为 35 和 50 秒，发布配置均为 3 分钟。[候选持续集成](https://github.com/wmqfl861/polymarket861/actions/runs/45953404817) · [原生服务验收](https://github.com/wmqfl861/polymarket861/actions/runs/45953404817/job/140354232109)

```sh
git rev-parse HEAD:src HEAD:tests HEAD:scripts HEAD:.github HEAD:deploy
PYTHONPATH=src:tests python docs/review-evidence/five-leagues-0.5.0a4/verify_domestic_data.py
PYTHONPATH=src:tests python docs/review-evidence/five-leagues-0.5.0a4/verify_domestic_data.py --extra-replays
```

这些实验使用独立数据库、真实固定历史档案、两段实收 GDELT 索引和受控的故障响应。它们验证时点、数据处理及历史一致性。真实周末请求量和限流、英超 T−60m 是否收到完整首发、各国球员覆盖、新闻预测增益以及扣费后收益，仍须在部署机按场次和轮次积累。正式费率继续等待可复核的逐条依据，英超 v2 保持自愿的本地前瞻登记。[后续取证计划](IMPLEMENTATION-PLAN.md)
