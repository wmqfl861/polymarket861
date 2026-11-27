# 五大联赛未来七天推荐数据

运行下列命令会刷新同目录的 `feed.json`。程序使用本机部署的预测、新闻、赛程和账户监控组件，只读取生产数据库与共享模拟账户。

```sh
OPENBLAS_NUM_THREADS=1 /mnt/d/polymarket861/.venv/bin/python /mnt/d/polymarket861/.local-deploy/five-automation/recommendation-feed/discover.py
```

## 发现和价格

- 按运行时的 UTC 时钟滚动七天，每个联赛各自读取当前赛程。英超使用两方确认的本地赛程与最新可核验的正式或技术封存，另四个联赛读取仍有效的赛程及最新生产封存。新一轮尚无封存的比赛会进入逐场缺口，后续轮次会重新检查。
- 按球队主客次序、赛事、开赛时间、Gamma 赛事编号与 Polymarket 合约核对 ESPN 上的 DraftKings 同一全场市场。仅采用可以核实的完整胜平负报价或同一半球线的大、小球报价。赔率使用同一庄家的所有互斥结果比例去水。临近各联赛官方首发发布时间时，要求两边都有可核验的首发，且模型已覆盖该信息。伤停来源未配置时在逐场证据中标记 `not_configured`。
- 按最新预测有新闻与无新闻两版本中较低的保守支付估值筛查。两次公开 CLOB 快照至少相隔五秒，包含所选及相反的 token，支持同一笔比较份额的共同深度。价差最多 0.04，保守估值和有新闻估值距各次盘口中点最多 0.08。冻结规则文本、Gamma 公布的费用和实际 CLOB 参数均需能核验。
- 比较份额为 20 份或交易所规定的更高起订量，只用于统一报价比较。买入限价取交易所可报价的最高价位，使模型保守估值减含费成本仍至少为 0.03，保守模型与外部保守概率的均值仍覆盖含费成本，同时原始报价不高于外部去水概率。两次观测的共同深度都要能在这个限价内覆盖比较份额。
- 先剔除现有六项的 token 及其整场比赛。每场其余备选按融合概率减含费成本、再按模型的含费优势排序，最多公布一个方向。第一次正式公布的方向记录在 `published_fixtures`，同一比赛后续只能恢复该 token，避免先后公布相反的方向。

## 留出赛季核验

`calibration.json` 使用已发表的五大联赛 B1 基线研究所固定的 40 份 OpenFootball 历史数据。重放独立于本周赛程的 2025/26 留出赛季，开赛前 60 分钟计算，按已发表的历史数据可用性假设延迟 24 小时，共 1,751 场。五个联赛的样本数与平均评分分别和源报告核对。可用下列命令重建。

```sh
OPENBLAS_NUM_THREADS=1 /mnt/d/polymarket861/.venv/bin/python /mnt/d/polymarket861/.local-deploy/five-automation/recommendation-feed/build_calibration.py
```

按联赛、买入方向、对应进球线和基线概率的十分位寻找历史组，以轮次聚类计算「实际命中率减模型均值」的区间。沿用此前专项复核的六重比较修正，区间上端小于零时剔除该方向，样本无法估计区间时标为证据不足。每条建议的 `research.calibration` 保存样本量、命中数、模型组均值、实际命中率和区间。这是识别模型历史偏高的研究筛查，无法验证交易盈利，细分组与 60 分钟模型也不能代表当前时点的新闻调整模型表现。 [已发表基线研究](https://github.com/wmqfl861/polymarket861/blob/main/reports/five-leagues-0.5.0a2/baseline-study.json)

## 通知接口

`schema_version` 为 `1`，正式规则的 `policy_id` 为 `verified-football-recommendations-v1`。UTC 时间戳均以 `Z` 结尾，价格和金额使用十进制字符串。

| 字段 | 含义 |
| --- | --- |
| `status` | `ok` 为已完成且没有发现来源缺口，`partial` 为部分来源有缺口，`unavailable` 为本轮无法完成可比较的研究。后者返回空建议。 |
| `started_at`、`checked_at`、`window_end` | 本轮开始、完成和七天窗口的结束时间。 |
| `valid_until` | 全部本轮建议仍有效的最早截止时间。每条另有自己的截止时间。 |
| `recommendations[].id` | `condition_id:token_id`。通知可以按完整 ID 或 `token_id` 去重。 |
| `fixture`、`league`、`kickoff`、`display`、`market_url` | Gamma 比赛标识、联赛代码、比赛时间、主客队及选择、Polymarket 市场网址。`fixture_id` 是生产模型的稳定比赛标识。 |
| `best_ask`、`worst_best_ask`、`limit_price` | 后一次最低卖价、两次中较高的最低卖价、仍符合规则的最高买入价。 |
| `checked_at`、`valid_until` | 该方向的验证时间、失效时间。失效取首次盘口接收后 15 分钟与开球时间中的较早者。每 10 分钟扫描一次时通常留有约五分钟重叠，临近开球以实际截止为准。 |
| `reference_stake` | 未入账的模拟美元参考金额。读取近期且与账户输入摘要匹配的已核验可用现金，复用共享账户的四分之一 Kelly 函数，在两次盘口共同深度和限价内逐档计算含费成本及可买份额。每项单独以同一笔可用现金定量，同时买多项需重新核算。余额证明、最低份额或定量条件不足时为 `null`。 |
| `research` | 生产模型、伤停与首发证据，外部去水来源，校准分组、含费成本与优势，以及两个 CLOB 快照、规则和原生费率凭据。 |
| `coverage.verified_fixtures` | 本轮已完成逐场核验的 Gamma 比赛标识。可结合 `coverage.fixture_results` 判断已核验的落选、保留名单、已有冻结方向和证据不足。 |
| `coverage.gaps` | 联赛、比赛、未完成的阶段及原因。`partial` 仍可包含其他比赛独立验证通过的建议。 |

文件发布使用单进程锁和原子替换。通知端按每条的有效期消费，缺少新证据时应保留历史及其真实时间，停止推送过期报价。
