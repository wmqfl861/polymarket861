# NBA 免费数据调查与接入验收

> 2026-11-28 实施范围及生效顺序见[审核后开发计划](REVIEWED-DEVELOPMENT.md)。本页保留 11 月 27 日送审设计及调查依据。

调查日期为 2026-11-27。建议用 BALLDONTLIE 普通免费层取已赛比分和未来赛程，用 Big Balls Sports Data 的篮球免费层验证轮换、逐场技术统计、阵容及伤停，用 Wikimedia 历史修订交叉核对比赛身份。DunkAPI 的循环免费额度适合抽样补证。五季基础回测可先用比分开展，球员增强回测另列实际覆盖分母。这四条路径的到达时间、覆盖深度和权利各异，调度器应分别记账。[BALLDONTLIE 数据与档位](https://docs.balldontlie.io/) · [Big Balls 篮球文档](https://bigballsdata.com/docs/basketball) · [DunkAPI 时间口径](https://dunkapi.com/docs/timing-and-rights)

## 来源与选用理由

| 来源 | 可用内容与配额 | 入场前须核对的差异 | 用途与降级 |
|---|---|---|---|
| BALLDONTLIE | 普通免费层每分钟 5 次。Teams、Players、Games 可用，Games 支持按日、季、类型及游标查，每页最多 100 场。提供方称历史可至 1946 年。 | 免费钥匙与 7 天付费计划试用分开。逐场分钟、事件、首发、伤病、赔率为收费端点。普通 Players 目录含历史球员，不能证明某球员现役或今晚可出场。 | 优先作比分、赛程、主客及终场状态来源。历史可取得年数、每种赛事和季内缺失数用实际免费钥匙逐季验收。 |
| Big Balls Sports Data | NBA 路由清单将逐场已存球队/球员统计、球员比赛日志、当前伤停和逐场名单列为 Free。普通额度文档为每日 1,000 次，预算按篮球一个运动、无 GitHub 奖励设计。 | 免费密钥的历史深度与服务端全库深度不同。官网发布过不同覆盖口径；逐端点、逐季记录实际响应、样本量和档位。全场逐回合为 Pro，精确回看历史伤停为 Edge。 | 优先验证当前赛季实收和上一季可取区间。首发必须在赛前收到、确认阵容性质及比赛身份。返回比赛结束后的 starter 标志仅属赛后事实。 |
| DunkAPI | 不绑卡、邮箱验证后余额每月补到 500 点，按注册周年月日续和补发规则执行。逐场双方及球员统计每次 5 点、逐回合 10 点、整联盟伤停 2 点。 | 球员日志的 season 是**结束年**。每日 10:00 UTC 更新 box/PBP，赛后约一天半内可再取更正；来源覆写旧版。伤停历史快照始于 2026-10-09，日频，分类为 Day-To-Day / Out，没有上游发布时间。 | 可建带抽样概率的轮换核验集，或预先指定球队/日期的补证样本。一次 30 日预算示例：30 次伤停、60 场 box、10 场逐回合和 40 点备用合计 500。31 日月份先扣 62 点再排。遇到 402 等下个免费周期。 |
| Wikimedia 球队赛季页 | 带修订编号、修订时间，可以定点取旧页。样本可见比赛编号、主客、比分、赛场、个别单项领跑球员。 | 表格、模板和球队命名随编辑变化。各队页面可能间接引用相同来源。把对手页当作第二视角，记录共同源；同源重复不能增加独立证据数。 | 作为比分二次检查、球队沿革及历史对时样本。球队赛季末球员均值不能代表赛季早期状态。严格实时回测仍需要本机赛前实收。 |
| FiveThirtyEight NBA Elo | 在已固定文件中可无钥复核多年主客、比分、场地类型和比赛阶段。同场按球队视角出现两行。 | 仓库数据默认 CC BY 4.0，目录列明赛果来自 Basketball-Reference。记录两方出处及具体用途，保留数据集署名。此固定文件止于 2015 年。 | 做历史身份、Elo 重建和对照数据去重测试。现代五季回测优先使用覆盖这些赛季的赛果来源。原文件预测列留作外部参照，建模从比分重新滚动。 |

参考页面：[BALLDONTLIE 端点与分页](https://nba.balldontlie.io/) · [BALLDONTLIE 权利第 6、11 节](https://www.balldontlie.io/terms.html) · [Big Balls 机器路由和服务档位](https://bigballsdata.com/league-endpoints.json) · [NBA 球员字段](https://bigballsdata.com/docs/nba-player-stats) · [Big Balls 调整记录](https://bigballsdata.com/whats-new) · [Big Balls 使用权](https://bigballsdata.com/legal/terms) · [DunkAPI 价格和续额](https://dunkapi.com/pricing) · [单场数据](https://dunkapi.com/docs/get-game) · [逐回合文档](https://dunkapi.com/docs/get-plays) · [Wikimedia 使用条款](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) · [FiveThirtyEight 字段及上游](https://github.com/fivethirtyeight/data/tree/master/nba-elo) · [FiveThirtyEight 授权](https://github.com/fivethirtyeight/data#license)

### 本次实际取得的证据

11 月 27 日 13:25–13:28 UTC（台北 21:25–21:28）进行了不带付费账户或 API 密钥的读取。下列样本描述的是来源当时的回答。开发阶段需重测篮球专用免费账户、本地网络以及真实比赛窗口。

| 查询 | 观察 | 可复核对象 |
|---|---|---|
| BALLDONTLIE 公共计划与 OpenAPI | HTTP 200。NBA 免费计划返回 0 美元、5 次/60 秒。比赛 schema 包含原生 id、两队 id、两队得分、日期、可空 UTC 开赛时间、状态及 NBA Cup 阶段。该文档写明部分历史加时赛终场 period 仍为 4，不能靠它唯一判定加时。未带 key 请求 2026-11-26 比赛返回 401。 | [计划 API](https://api.balldontlie.io/account/v1/plans) · [OpenAPI](https://www.balldontlie.io/openapi/nba.yml)，本次后者 SHA-256 为 `9c8e40b11a588cbec0b51095ec34ea94de729cb9644ff72fc932893527282d4b` |
| Big Balls 篮球 coverage | HTTP 200。被取到的覆盖快照生成于 11 月 25 日。NBA 全库 2019/20–2026/27 的已完赛分母 9,069 场，球队技术统计 9,004、球员技术统计 9,054、首发/阵容 1,557、逐回合 4,006，名册标为缺失。伤停状态仅报告 current；没有逐人、逐日或赛前分母。 | [覆盖 API](https://api.bigballsdata.com/v1/coverage?sport=basketball)，本次响应 SHA-256 为 `ef7037f5dd89badb7c916fc510285ef75d7ee37e24c1e5eb0563fe29c53d2d8f`。只读覆盖及档位数据，尚未取得认证业务收件。 |
| Big Balls 生成的 NBA 路由目录 | HTTP 200。Free 登记为 `/v1/stored/matches/{id}/stats`、`/v1/players/{id}/gamelog`、`/v1/injuries`、`/v1/nba/games/{id}/lineups`。过去时点的 `/v1/as-of/injuries` 为 Edge，`/v1/nba/games/{id}/plays` 为 Pro。 | [机器档位](https://bigballsdata.com/league-endpoints.json)，SHA-256 为 `c7f5af7a1118aa29608ce9ada3ee5575391183efb0d961207b4161d1744592c8`。留意通用 match UUID 与 NBA 专用路由参数的区别。 |
| FiveThirtyEight 固定文件 | HTTP 200，18,173,265 字节，126,314 行。从 1946-11-01 至 2015-06-16。按 lg_id 过滤后 NBA 118,016 条球队视角、59,008 个独立比赛编号；其余 8,298 条为 ABA 视角。 | [文件目录记录](https://api.github.com/repos/fivethirtyeight/data/contents/nba-elo/nbaallelo.csv)；Git blob `6d6ccd07151aed322e1a77c57ea83b8ea58c5523`，SHA-256 `d46ed3540ee8d9eca31b3e94cc8c777e0be5156173d814ebf65b8195e8d616bc`。 |
| 英文维基球队赛季修订 | 读取两支 2026/27 球队页，常规赛日志可见带前导零的比赛号码、队视角 pf/pa 与球馆。另取波士顿 2025/26 页，名单、比赛日志、赛季球员统计、交易各有独立段落。两支现季球队样本不能推断全联盟逐场覆盖。 | [波士顿现季修订](https://en.wikipedia.org/w/index.php?oldid=1387746248) · [湖人现季修订](https://en.wikipedia.org/w/index.php?oldid=1388204679) · [波士顿上季修订](https://en.wikipedia.org/w/index.php?oldid=1383158390) |

### 许可、留存与免费预算

BALLDONTLIE 第 6 节允许免费档实际取得的数据做归档、回测、模型训练、合法预测应用及 AI 处理，禁止将自身做成替代数据分发服务；第 11 节保留第三方权利。Big Balls 允许本地存储、内部训练、评测和发布派生指标，其原始字段不得打包公开为数据文件或服务。给外部 AI 的发送权逐源登记；未获得该项权利的原始回执和正文留在本机。DunkAPI 允许存储及训练，停止账户后保留内部研究使用，继续服务外部用户须维持账户；也明确未代授联盟权利。Wikimedia 保存永久修订号、署名和许可，涉及再发布文章/改编文本按具体页面标注执行。复审材料优先提交字段表、样本数、程序和哈希。[BALLDONTLIE](https://www.balldontlie.io/terms.html) · [Big Balls](https://bigballsdata.com/legal/terms) · [DunkAPI](https://dunkapi.com/terms) · [Wikimedia](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use)

NBA.com 使用条款第 9 节直接限制其统计与博彩活动结合，也限制综合、定期更新统计库。`nba_api` 的 MIT 许可只涵盖其客户端。NBA CDN、stats.nba.com 及从它们衍生的 Kaggle、GitHub 或 SportsDataverse 镜像默认不作本项目的授权训练入口。NBA 规则和伤停报告制度可作解释依据，涉及伤停公告原文或可自动处理的字段另核条款。第三方服务可申请体育许可，也可能只是重新整理公开信息。发现上游明确冲突时，在追溯清楚相应权利前隔离字段，保留其他合格来源；不能以免费档名义绕过付费档。[NBA.com 第 9 节](https://www.nba.com/termsofuse) · [nba_api 的数据声明](https://github.com/swar/nba_api#license-and-terms-of-use) · [镜像示例的上游清单](https://github.com/sportsdataverse/hoopR-data)

拟定限流策略为 BDL 单账户统一令牌桶，每分钟先排 4 次，为恢复和漂移留余量。按“NBA 所有队”一次查整段日期并按游标翻页，避免按球队逐场重复拉取。在稳定 4 次/分钟、每页 100 场的条件下，5 × 1,230 场常规赛约 62 页、约 16 分钟的网络预算，只是容量估算；实际另计分季、阶段、修订、补赛和重试。Big Balls 先依据当日免费上限将 20% 预留给纠错，剩余额度优先联赛级伤停、固定窗附近变更和已完赛逐场统计。在 15 场比赛日，可测算 96 次十五分钟联赛查询、75 次名单复查和 45 次赛后/次日修订查询，共 216 次，再计目录和分页；回放和补档在剩余配额中排队。实际返回的 quota、cache age、空包及重试计入每周报告。持续 401/403 停用对应免费字段，429 尊重 Retry-After，额度耗尽留缺件。长期免费循环额度不能依赖绑卡、升级、充值或多账户轮换。[BDL 档位](https://docs.balldontlie.io/) · [Big Balls NBA 档位](https://bigballsdata.com/nba-api) · [DunkAPI 续额](https://dunkapi.com/pricing)

维基历史页的 oldid 固定该页正文，但当下渲染结果可能使用新模板。历史对时优先从该版原始 wikitext 读取明文；来自模板、Lua 或 Wikidata 的字段要固定当时的全部依赖版本，无法还原就记缺件。以同一 oldid 配两版模板作回归，确保后期比分不会混入早期日程。[维基历史版本说明](https://en.wikipedia.org/wiki/Help:Page_history#Linking_to_a_specific_revision_of_a_page)

## 从收件到可建模字段

| 对象 | 标识与时间 | 验证和失败处理 |
|---|---|---|
| 赛程、球队与场地 | 内部独立 `nba/game` 身份、联盟/赛事、赛季起始年、阶段、主客、场馆、IANA 时区、排期版本；保留供应商字符串 ID 和原始时区。 | 多源匹配须同时满足队伍、阶段、主客、时间范围；同日多种赛事、搬迁、共用球馆、中立场、临时换主场须显式处理。BDL 以起始年为季号，DunkAPI 以结束年为季号。历史日级日期仅用于日级回测。 |
| 球员、阵容与转会 | 球员独立 id 与球队任职区间、常规/双向合同、上下放、球衣号码和别名分表。 | 名字、无重音拼写、球衣号码只作候选，须结合球队和日期核对。变更有效日与首次收件并列，处理赛季中交易、多队出场、重名及伤停和交易交叉。阵容未到、未知、DNP、教练决定缺阵分别编码。 |
| 比分、节次与赛果 | 保存最终状态、全场含加时及可证实的常规时间比分、每节和加时得分、首次收到终场及后续修订。 | 逐节完整才校验分节和；常规时间平局进入加时，每次加时 5 分钟。原始赛程/状态为空不推断真实开赛。不同来源赛果冲突时暂停该标签，修订以新版本恢复评分和训练，保留旧评分。 |
| 球队与球员 box | 按比赛、球队、球员、修订存秒数、首发、DNP 原因、投中/出手、三分、罚球、攻防篮板、失误、助攻等。 | 得分可由命中数校对。分钟精度足够时，每队目标为 240 分钟加每个加时 25 分钟；只报整数时按出场人数和舍入精度设误差范围。缺换人或团队篮板等造成的不等式单独归因。赛后确认的首发仅用于日后训练及评估赛前名单。 |
| 逐回合和来源修订 | 使用“来源、原生比赛 id、事件 id/序号、版次”，统一节次、剩余秒、事件队和赛后改判。 | 同一秒可发生多条事件，技术罚球、进攻篮板、跳球等不能简单增加一回合。缺页或缺球权信息标记影响范围；全场比分及箱分与逐回合终点相符才评估衍生变量。无完整逐回合时用可核对箱分估算回合，标明估算口径。 |

每条记录分别绑定来源许可版本、文档/端点、比赛发生或更新时刻、来源发布时间/更新时间、本机首次收到时间、首次入库时间、原件摘要、关联解析器和前一版本。遇到发布时刻缺失，不根据比赛日期推造分钟精度。点时预测使用截止前已收到且已登记的合格数据，隔天回填的数据只参加更正重放和后续时点。多源依赖同一上游时记录依赖关系。当前比赛事件、比赛中的场上五人、终场的技术统计只可用于该场赛后检验。[BDL 比赛状态字段](https://www.balldontlie.io/openapi/nba.yml) · [DunkAPI 更正说明](https://dunkapi.com/docs/timing-and-rights)

### 球馆、旅程与体能负荷

用赛前可见的比赛地点，连接 Wikidata 馆名、坐标和 IANA 时区的固定版本，计算两场之间大圆距离、前进/后退时差、休息小时及连续客场。球队的真实航线、交通延误和睡眠只有取得可核实消息时才使用。实际读取丹佛 Ball Arena 的 Wikidata 修订 2541967177 可取坐标，但无场馆海拔属性 P2044。海拔缺件标为未知，另找有许可的场馆或高程数据。中立场、海外赛和赛场迁移按实际球馆版本计算，避免用名义主队城市代替。时区换算固定 tzdb 版本并覆盖夏令时。[Ball Arena 的固定修订](https://www.wikidata.org/w/index.php?oldid=2541967177) · [Wikidata 结构化数据 CC0](https://www.wikidata.org/wiki/Wikidata:Licensing) · [IANA 时区数据库](https://www.iana.org/time-zones)

## 数据工作包的验收

1. **免费钥匙验收。** 在自管机选普通免费档，记录档位、端点、合法用途和许可全文指纹。对已结束、未来、缺席和缺字段的比赛分别留一份回执，在 30 支球队上出逐赛季覆盖矩阵。若供应商赛后数据显示球员但不给赛前首发，则分别验收两种能力。无钥环境仍可运行固定历史和受控样本。
2. **历史清册。** 逐季、逐阶段统计计划场次、唯一比赛、重复、别名冲突、正常完赛、比分更正、box 双方充足、首发完整、合格赛前伤停与双时点真实盘口。按城市/赛季版本映射球队身份；因缩季、杯赛决赛、附加赛或季后赛产生的分母变化须引用当季赛历。五季核心赛果缺失先补来源或披露排除，不把缺件填零。
3. **历史诚实性。** 将最终档案、可追溯的历史公开修订及本机实际赛前收件分层报告。回放同一时点两次应给同样的来源、身份、数值、资格与摘要。用二次更正、同刻矛盾、跨太平洋日期、夏令时、加时、推迟后恢复和转会反例验证，禁止未来赛况反填。
4. **增强输入的覆盖。** 对拟纳入的阶段逐场计数，分母为全部应测比赛，列出两队箱分、关键轮换分钟可信、双边首发、每个截止前伤停/缺件占比。按球队、主客、连续比赛日和来源拆分。免费样本不足时继续给全体合格比赛的比分基线，具备资料的子集用于条件诊断，报告其选择偏差。预登记新闻主比较保留全部赛前机会、回退和漏封，详见[验证方案](VALIDATION.md)。
5. **运行和复核。** 一次完整比赛周验证每日用量峰值、补抓延迟、成功/空响应/429、成本为零，以及回退到无球员输入时对基础预测的影响。每周重检权限/档位/模式变更，逐场纠错留存受影响预测清单。新增来源先影子比对历史错误和新增覆盖，运营者核验权利后再入模，原始篮球数据依供应商条款留本地。
