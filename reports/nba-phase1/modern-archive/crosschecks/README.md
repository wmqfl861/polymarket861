# 现代 NBA 历史赛果的交叉核据

本包只记录已完赛比赛的当地比赛日期、名义主客队和终场比分，用于历史模型技术回放。记录来自赛季结束后的资料；赛前新闻、阵容、即时盘口、正式比分可用时点和 Polymarket 兑付仍需要各自的证据。

## 已核对的比赛

| 赛季 | 原英语两队一致的场数 | 补入记录 | 已有键改分或改主客 | 撤回错误日期键 | 终场覆盖 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2020/21（热身） | 1,016 | 64 | 3 | 0 | 1,080 |
| 2021/22 | 1,202 | 29 | 1 | 1 | 1,230 |
| 2022/23 | 1,204 | 26 | 1 | 0 | 1,230 |
| 2023/24 | 1,220 | 10 | 0 | 0 | 1,230 |
| 2024/25 | 1,227 | 3 | 1 | 0 | 1,230 |
| 2025/26 | 1,213 | 17 | 1 | 0 | 1,230 |

五个目标赛季共 6,150 场，加上热身共 7,230 场。180 个球队赛季分别达到 72 或 82 场，单队同一当地日期重复比赛数为 0。16 个原待核日期键可匹配到相邻日期同队同分的已核实比赛，已单列以防重复。所有修订行都保留至少两条证据断言。两个语种的维基编辑以及双方球队记录可能引用同一上游，这些分别可核对的载体不能视作相互独立的统计样本。

初始英语版结果与校正来源的冲突包括 2021-12-15 骑士主场 124–89 火箭、2022-11-15 独行侠主场 103–101 快船、2024-11-17 开拓者主场 114–110 老鹰及 2025-11-11 篮网主场 109–119 猛龙。灰熊主场 125–118 森林狼的正确日期是 2021-11-08，英语双方记录的 11-07 要撤回。后面三个赛季中，14 个英语双队一致但法语一队或两队提出异议的比赛逐场查阅了公开赛事记录，其中改正 2 场、保留 12 场。[后段赛季的逐场裁决及公开核对链接](adjudicated-conflicts.json)

## 固定资料与归属

- **FiveThirtyEight，NBA Elo game file，经 Item Response Warehouse 整理。** FiveThirtyEight 的仓库注明，未另行标注的数据集按 CC BY 4.0 提供，NBA 预测归档由其发布。此处实际使用 Item Response Warehouse 的同名数据表，页面将来源归为 FiveThirtyEight NBA Elo 的 Internet Archive 捕获，并对该表标明 CC BY 4.0。IRW 发布页此次实取为 v772，所下载的 Redivis 表为 `irw_competitions v7.2`，页面同时列出的元数据清单版本为 v7.1。硬编码到 v7.2 的免费 CSV 共 73,363 行，SHA-256 为 `46814d80064bf7f7a80eb809b04a8d182cf4c6f53ab9c09f96f14e0e44fa625f`。原发布者的动态 CSV 地址本次重定向到 ABC News 的 HTML；核验工具使用已下载并固定摘要的署名再发布。用于本包时仅筛选 2020/21 至 2022/23 常规赛，统一 BRK/CHO/PHO 代码、UTC 日期及名义主客列。124 条更改有该库逐场支持。早三季全表还独立用于排除多余的日期键。[发布者许可](https://github.com/fivethirtyeight/data#readme) · [发布者的 NBA 归档说明](https://github.com/fivethirtyeight/data/tree/master/nba-forecasts) · [IRW 表归属和范围](https://itemresponsewarehouse.org/tables/fivethirtyeight_nba_1946_2023/) · [固定再发布 DOI](https://doi.org/10.57761/he7v-mt65) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)
- **English Wikipedia 和 Wikipédia en français 各球队赛季条目的贡献者。** 逐页固定修订号、原文摘要、时间与标题。对英语初始双队结果重新取其与修订一致的球队网页，对法语先调查 180 个球队赛季，最终引用英语 84 页、法语 121 页。法国球队网页逐行核对常规赛段、比赛日期、对手和主客、胜负相符的终场比分，以及该行链接中的官方比赛 ID 或赛报文件名。只采用这种核对和英语记载、授权档案或个案赛事记录可以相互印证的更改。修订语料经提取、标准化并交叉比对；本包对这些来源所作的数据整理按 CC BY-SA 4.0 提供。署名链接使用实际修订，页史可追溯编辑者。[逐页归属与修订](revision-pins.json) · [Wikimedia 使用条款第 7 节](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use/en#7._Licensing_of_Content) · [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/)
- 公开赛事链接供逐场人工核对。已审的英文和法文维基文本及 FiveThirtyEight 的署名再发布提供输出比分；本包未再分发比赛单 PDF、NBA 数据接口响应或文字报道。此核据包不注册新的生产数据源或授予任何外部来源的采集权限。
- OpenLigaDB 的体育数据按 ODbL 1.0 提供，此次查阅的 NBA 简写六年查询为空，可用联赛清单也未检出相应赛事。没有把该源计入已核实场次。[OpenLigaDB 许可说明](https://www.openligadb.de/en/license)

## 文件与复核

`resolved.csv` 有 156 行新增或替换，`withdraw.csv` 有 1 行撤销。应用键为 `(season, date, 两支队伍排序后)`，按日期撤旧，再覆盖或新增。这样可以正确处理热身季另一次主客登记颠倒。字段中的赛季是开始年份。`proofs.json` 的每一条均能在原始许可资料中找回。`discarded-claims.json` 列明 16 个被弃用的旧日期及其正确比赛日期。规范化结果摘要、赛数和初始输入摘要在 `counts.json`。

在已装项目依赖的环境中，从仓库根目录运行下列命令。核验器通过 Wikimedia 公开 API 按固定修订批量提取，校验页面 SHA-256，并按注明的 Redivis 版本下载免费 CSV。它直接验证仓库所带英语基线的摘要，并在内存合并修订；如需核对自己导出的最终 CSV，可额外传入 `--final-csv 路径`。已有同样的固定缓存时可以省略 `--fetch`，独立重放不会发起外部请求。

```sh
bundle=reports/nba-phase1/modern-archive/crosschecks
cache_dir=$(mktemp -d)
python "$bundle/verify_cross_sources.py" --repo . --cache "$cache_dir" --fetch
python "$bundle/verify_cross_sources.py" --repo . --cache "$cache_dir"
```

该命令重新验证每项订正所列的证据，检验撤回日期在完整的历史源中确实无此场，并对合并档核对所有队季、比赛键、被弃用日期和完整赛果摘要。前瞻业务验证仍应按项目已批准的独立比分来源、赛前封存与逐场评分流程执行。
