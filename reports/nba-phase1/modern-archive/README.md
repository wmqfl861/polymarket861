# 2021/22–2025/26 NBA 五季固定参数回放

五个完整常规赛季各 1,230 场，共 **6,150 场**。另用 2020/21 的 1,080 场预热。按两种训练标签滞后各跑一次，N2 的 24 小时主胜 Brier 为 **0.2137202**，48 小时为 **0.2140122**；数值越低越好。全场与主、客队得分用 CRPS，单位是分。下表均为 24 小时技术回放。

| 赛季 | 场数 | N2 主胜 Brier | N2 全场得分 CRPS |
| --- | ---: | ---: | ---: |
| 2021/22 | 1,230 | 0.2183731 | 10.4596 |
| 2022/23 | 1,230 | 0.2262915 | 10.4479 |
| 2023/24 | 1,230 | 0.2112401 | 10.3529 |
| 2024/25 | 1,230 | 0.2081579 | 10.3456 |
| 2025/26 | 1,230 | 0.2045387 | 10.6703 |
| 五季合并 | 6,150 | 0.2137202 | 10.4553 |

合并主胜 Brier 的 N0/N1/N2 依次为 0.2473043、0.2147997、0.2137202。N2 对 N1 的差为 −0.0010795，以赛季分层的七个日历日为连续区块，10,000 次重抽样的探索性 95% 区间为 [−0.0015758, −0.0006367]。两种滞后均冻结 1.5 分背靠背参数。回放使用赛后最终档案，历史时点存在的新闻、订单簿、手续费和可成交数量均无法从这些比分推断。结果用于检验现代得分环境下的球队模型。[24 小时全结果](study-24h.json) · [48 小时全结果](study-48h.json)

## 比分如何复核

先按英语维基百科两队赛季日志核对比赛日、主客与终场比分。6,066 个目标赛季记录在两份日志中相符。随后用英语/法语固定修订、FiveThirtyEight 的固定再发布版本，以及发生争议的单场史料裁决，发现两队页面可能共同抄错。2024-11-17 波特兰对亚特兰大两页记成 114:107，复核比分为 114:110。整个六季数据（含预热）有 156 个新增或替换、一个错误日期撤回。180 个队季分别含 72 或 82 场，单队同日重复为零；第三方核出的全部 7,230 个赛果规范化 SHA-256 为 `403506b8029c1126a9096b34a70de46a2e0af2947fef9f648fce142cf8cb0b89`。[原英文修订](wikipedia-revisions.json) · [逐项裁决、来源版本、署名和许可](crosschecks/README.md)

英文和法文维基百科贡献者按所列固定修订署名；本研究提取、规范化并交叉校勘比赛事实。维基资料依 [CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/) 提供；作品修订链接可见作者历史。FiveThirtyEight 历史数据沿用原作者 CC BY 4.0 声明，版本和出处在交叉核据中。原随仓 FiveThirtyEight 归档仍保留原先的 2015 年终点，本目录使用单独的后续数据版本。[Wikimedia 复用条款](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use#7._Licensing_of_Content) · [FiveThirtyEight 数据](https://github.com/fivethirtyeight/data/tree/master/nba-forecasts)

## 复现

在仓库根目录及项目 Python 环境执行。第一组全程使用随仓固定事实和修订，不需要账户或联网。需要独立核对源网页时再运行后一组，缓存保存在本机。

```bash
export PYTHONPATH=src OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
archive=reports/nba-phase1/modern-archive
python scripts/nba_wikimedia_archive.py study --directory "$archive" --lag-days 1
python scripts/nba_wikimedia_archive.py study --directory "$archive" --lag-days 2
```

```bash
scratch=$(mktemp -d)
python scripts/nba_wikimedia_archive.py build --pins "$archive/wikipedia-revisions.json" --cache "$scratch/en" --directory "$scratch/raw"
cmp "$scratch/raw/agreed-finals.csv" "$archive/agreed-finals.csv"
cmp "$scratch/raw/withheld.json" "$archive/withheld.json"
python scripts/nba_wikimedia_archive.py export --directory "$archive" --output "$scratch/verified-finals.csv"
python "$archive/crosschecks/verify_cross_sources.py" --repo . --cache "$scratch/cross" --fetch --final-csv "$scratch/verified-finals.csv"
```

交叉裁决的复现验证程序及命令见 [核据包](crosschecks/README.md)。原始不一致行 `withheld.json` 属于逐队核对的中间结果，完整回放另外应用 `resolved.csv` 和 `withdraw.csv`，校验全部比赛摘要后才评分。带密钥的生产比分来源按 `nba model modern-study --sync` 另行复核。
