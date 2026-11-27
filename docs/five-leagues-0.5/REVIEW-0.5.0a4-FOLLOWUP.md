# 五大联赛部署复审回应

本次改动覆盖足球推荐、历史手工选单、部署检查及微信回执。五大联赛的正式赛前概率和历史模拟账本继续按原版本重放。下表区分仓库内可验收的修复、需要部署机原件的工作和需要后续赛果的判断。[Claude 审核](../polymarket861-five-leagues-code-review-0.5.0a4.md) · [审核探针及原始输出](../review-evidence/code-review-0.5.0a4-nba-plan-2026-11-27/)

| 审核项 | 处理及验证范围 |
| --- | --- |
| W1-1 外部赔率 | 推荐扫描、持仓复查和旧选单读取 ESPN/DraftKings 共用免费授权登记，按联赛、请求路径、书面许可文件摘要和有效期检查。登记为空，当前在 HTTP 请求之前返回缺口，推荐列表无法新增候选。持仓复查仍可读取 Polymarket。到账和正式结算凭原始回执独立重放。旧手工开仓工具拒收缺少授权摘要、过期或赛后的外部报价。 |
| W1-2 校正与偏差 | 从推荐层移除 494 个稀疏分箱的二元闸门。固定五联赛 2025/26 赛季 1,745 场 T−60 B1 逐场拟合的连续校正，映射主、平、客和全场大 2.5。三个胜平负概率归一，反方向和小球取补数。取校正后的无新闻支付估值和有新闻估值的较低值，再核对独立赔率、费用、两份盘口和深度。支持范围外的时点及盘口留缺口。新旧规则沿用已发布比赛的锁定方向。拟合样本已经用于估参，尚无新一季的独立前瞻评估。 |
| W1-3 历史六单 | 专项脚本改读可核验的私有归档。国际米兰场次必须从原始预测读出相符摘要，不能用手填摘要覆盖输入。归档、复核及重试方式见下文。未把推荐接入自动开仓。 |
| W1-4 部署绑定 | 分发模板将扫描与账户监控指到同一足球发行解释器，纯标准库通知器沿用系统 Python。CI 扩展到 deploy 的 Ruff 检查、脚本使用的包属性检查和足球部署回归。检查属性时不运行任何定时任务。复核账本时另发现原流程只验证输入卖盘。现在账户从同一份已认证原始回执重建买盘、核对买深和最优买卖价。篡改输入买价会跳过该笔成交。 |
| W2-1 值守状态 | 四国预测 `big5-scheduler watchdog` 返回 0 代表全部正常，2 代表仍在推进但部分数据失败，1 代表至少一项中断、停滞或时钟异常。命令行解析错误也可能返回 2，须以正常输出的状态 JSON 辨别。分发的值守服务带 `--alert-on-unavailable`，只有成功产生健康报告且仅有 partial 时才抑制故障报警，报告仍保留每个错误。 |
| W2-2 重启缓存 | 保留现有进程内修订缓存。审核计得 2,000 份修订冷启动约 1.4 至 4.3 秒。暂无持久缓存改造依据。在部署安排中保留避开临近封存窗口的做法。 |
| W2-3 伤停 | 免费 API 的当季限制仍有效。只有带来源及时间的手工证据能进入相应的当季情景，缺失证据保持未知。 |
| W2-4 新闻召回 | 保留已审核的足球语境筛选。页面链接表示可能相关，尚无全网召回率的分母。 |
| W2-5 新仓提醒 | 已知失败直到开球仍未送达时，在 SQLite 留下 `expired_undelivered`。收件确认不明时留下 `expired_unconfirmed`，可继续只读核对延迟到达的回执。开球后不补发。服务描述同步为新仓提醒。 |
| W2-6 审核范围 | 新回归覆盖无授权时三个采集入口、本地缺口、旧方向锁定、校正到限价、绑定、证据归档、经归档盘口成交、正式兑付、篡改拒收、凭据不足的账本重放和通知过期。部署机六单原始输入、实时消息送达、市场公平价和后续赛季预测能力分别需要现场或前瞻证据。 |

## 在持有原件的部署机保全六单

仓库无法恢复部署机的私有研究输入。同步脚本后，在部署账户下执行以下命令。首次运行复制整个原始目录，校验源和副本的逐文件 SHA-256。重复运行只允许内容一致的副本。第二条只读核对已归档文件。原目录尚存时，应把该归档加入现有的私有账户备份。若原件已失，记录缺失，不把摘要当作原件。

```bash
cd /mnt/d/polymarket861
python3 .local-deploy/paper-10000/archive_slate_evidence.py
python3 .local-deploy/paper-10000/archive_slate_evidence.py --verify
```

默认读取 `/tmp/pm861-weekly-value-20261127`，归档至 `.local-deploy/paper-10000/source-evidence/pm861-weekly-value-20261127`。原先五国模型、账本、`five-automation/reports/epl-round12-recheck-20261127.json` 及 Polymarket 市场回执仍需按原备份方案保留。[模拟账户交接](PAPER-AUTOMATION-HANDOFF.md)

## 复核入口

在已安装当前 wheel 的环境中，从源码树运行。以下检查不会连接赔率、发送微信或写入部署账本。

```bash
python scripts/check_paper_bindings.py
ruff check deploy scripts/check_paper_bindings.py
python -m unittest discover -s tests -p test_football_paper_review.py
python -m unittest discover -s tests -p test_paper_opening_notifications.py
python -m unittest discover -s tests -p test_big5_scheduler_health.py
```

当季是否具有预测或净价优势，需要后续比赛周完整的赛前报价、模型原文、赛果及收盘价共同评估。授权来源为空期间报告建议覆盖率为零。[筛选口径、校正数据和字段说明](../../deploy/paper-automation/five-automation/recommendation-feed/README.md)
