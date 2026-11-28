# 0.5.0a4 独立复核与 NBA 计划审核 · 复现材料

对应两份审核稿：[0.5.0a4 代码复核](../../polymarket861-five-leagues-code-review-0.5.0a4.md)和 [NBA 计划审核](../../polymarket861-nba-plan-review.md)。审核对象是 `main` 的 `60c1a2d`（安装包 0.5.0a4，源码与 `6844caf` 相同）和计划分支 `plan/nba-pregame-20261127` 的 `0c3534a`。

所有脚本都不读写仓库，不触碰任何真实数据目录。除版本切换演练要从 GitHub 取固定版本的 OpenFootball 档案之外，都不联网。脚本用 `python -I` 运行，源码和数据的路径用参数传入。实测环境是 Python 3.12.3、numpy 2.3.5、scipy 1.17.0。

有两份外部文件不随仓库分发，要自己下载，各放进一个单独的空目录：FiveThirtyEight 的 `nbaallelo.csv`，和 OpenFootball 固定提交 `e6744429` 的五联赛文件（76 个）。下载方法在“命令”一节。

## 这次新写的脚本

| 脚本 | 回答什么问题 | 输出 | 耗时 |
|---|---|---|---|
| `probe_wiki_replay_cache_hot_path.py` | 0.5.0a4 给维基百科修订重放加的缓存，冷、热、追加一份之后各要多久，三种情形返回的结果是否相同（V2-3、W2-2） | `probe-wiki-replay-cache-hot-path.json` | 约 12 秒 |
| `probe_feed_calibration_bias.py` | 推荐列表的校准闸门分了多少格、每格多少场、区间多宽、排除了多少格；五联赛合并后哪些格偏差最大；六个已建仓方向落在哪几格（W1-2） | `probe-feed-calibration-bias.json` | 不到 1 秒 |
| `probe_holdout_confidence_slope.py` | 用产品自己的滚动回测逐场重算 2025/26 留出季五个联赛：校准斜率、分段偏差、定价公道的合约在模型眼里凭空多出多少优势、六个仓位按拟合校正之后与限价差多少（W1-2） | `probe-holdout-confidence-slope.json` | 约 2 分 20 秒 |
| `probe_deploy_script_wheel_bindings.py` | `deploy/paper-automation` 下的脚本用到安装包的哪些属性，是否都存在，其中多少是下划线开头的内部函数（W1-4） | `probe-deploy-script-wheel-bindings.json` | 1 秒 |
| `probe_nba_public_scores.py` | 计划对 FiveThirtyEight 文件的描述对不对；球队层各级模型的差别有多大、一个留出赛季能分辨多小的差别；终场比分分布的形状（X2-5、X3-3） | `probe-nba-public-scores.json` | 约 3 秒 |
| `probe_nba_news_gain_power.py` | 新闻增益判定规则在不同真实效应、不同场数下的把握；0.002 相当于多大的新闻影响；改用收盘价打分之后的把握（X3-1） | `probe-nba-news-gain-power.json` | 约 4 秒 |
| `probe_nba_polymarket_sample.py` | 盘口抽样表与 POLYMARKET.md 的表是否逐格一致；各玩法的买入成本；择线规则选到的线离五五开多远；结果的排列次序（X2-1 到 X2-4） | `probe-nba-polymarket-sample.json` | 不到 1 秒 |
| `probe_nba_package_identity.py` | 新增 `nba/` 目录、改共享模块共六种情形，足球配方摘要、安装包文件清单摘要、“选单实现”摘要各自变不变（X4-1 到 X4-3） | `probe-nba-package-identity.json` | 约 11 秒 |

`outputs/environment-and-tests.txt` 记录了三个解释器上的测试结果、持续集成各步在本地的结果、配方摘要和 GitHub Actions 的运行编号。

## 原样重跑的旧脚本

下面这些脚本在[上一个证据目录](../code-review-0.5.0a3-fiveauto.1-2026-11-27/)里，脚本本身没有改，对着 0.5.0a4 重跑，输出存在本目录的 `outputs/rerun-*` 和 `outputs/switch-*`。

| 脚本 | 上次（0.5.0a3+fiveauto.1）的结果 | 这次（0.5.0a4）的结果 |
|---|---|---|
| `probe_big5_scheduler_news_updates.py`（V1-1、V1-2） | 每天 0、2、6、24 条链接：新闻更新封存 0、13、39、115 份，封存共 17,318、122,910、446,531、1,692,558 字节。调度停三天后状态仍是 `ok`。名单来源抛错后状态里看不出来 | 新闻更新封存都是 0 份，封存共 17,693、24,385、35,504、46,636 字节。停三天后是 `stale`。抛错写进日志（`Domestic news poll bun …: rosters: TypeError: unexpected source structure`），状态里这条轮询记为 `retry` |
| `probe_league_press_labels.py`（V1-3） | 136 个字样，60 家俱乐部只能靠正式全名命中；4 篇足球报道漏掉，6 个错配样例命中 | 272 个字样，其中 93 个是单词字样；只能靠全名命中的俱乐部 0 家；4 篇都归到俱乐部，6 个都不再命中；只在网址里有队名、没有足球语境的一条现在弃权 |
| `probe_absence_scenario_weighting.py`（V1-4） | 前锋缺阵 −1.78%；第三门将缺阵让对手进球率 +2.05%；六人缺阵 −5.77% 和 +5.69% | −0.99%；第三门将被排除，变化为 0；−3.21% 和 +1.95% |
| `probe_league_inputs_revision_replay_cost.py`（V2-3） | 2,000 份修订读一次：德甲 1.70 秒，意甲 5.28 秒 | 1.09 秒、3.28 秒。这个脚本每个库只读一次，量到的是冷的那一次，缓存的效果要看上面的新脚本 |
| `probe_big5_rolling_day_boundary.py` | 59 轮、230 个采样任务全部完成，跨零点时目录窗口换一次 | 相同 |
| `probe_epl_fixed_seal_under_evidence_churn.py`（V2-1） | 每组 5 轮：没有外来提交 5/5 封出，每 0.05 秒一次提交 5/5，每 0.01 秒 5/5，每 0.002 秒 1/5 | 每组 3 轮，没有跑 0.01 秒一组：3/3、3/3、2/3 |
| `replay_release_chain.sh` | 0.5.0a2、0.5.0a3、eplfix.1、fiveauto.1 依次交接再退回 | fiveauto.1 换到 0.5.0a4 再退回，输出是 `switch-fiveauto-a4-fiveauto.txt`。两个方向都要求先比较和具名批准，比较结果都是 1,900 场、190 轮、0 处改变，换手之前的封存逐字节读回 |

## 命令

```sh
REPO=<60c1a2d 的检出目录>; E=$REPO/docs/review-evidence/code-review-0.5.0a4-nba-plan-2026-11-27
E3=$REPO/docs/review-evidence/code-review-0.5.0a3-fiveauto.1-2026-11-27
cd $REPO

# 测试和持续集成的各步（三个解释器各跑一遍 unittest）
python -m unittest discover -s tests
python scripts/check_league_boundaries.py && python scripts/check_recipe_references.py
ruff check src tests scripts
python scripts/check_recipe_identity.py > recipe-3.12.json     # 3.11、3.13 同样各生成一份，再 --compare

# 外部文件，各放进一个单独的空目录（D1、D2）
curl -fsSL -o "$D1/nbaallelo.csv" https://raw.githubusercontent.com/fivethirtyeight/data/master/nba-elo/nbaallelo.csv
python -I $REPO/docs/review-evidence/code-review-0.4.2-2026-11-26/fetch_openfootball.py "$D2"
git show origin/plan/nba-pregame-20261127:docs/nba-plan/POLYMARKET-SAMPLE.csv > "$D3/POLYMARKET-SAMPLE.csv"

# 新脚本，足球部分（T1、T2 是各自的空临时目录）
python -I $E/probe_wiki_replay_cache_hot_path.py --src src "$T1"
python -I $E/probe_feed_calibration_bias.py deploy/paper-automation/five-automation/recommendation-feed/calibration.json
python -I $E/probe_holdout_confidence_slope.py --src src --raw "$D2" \
    --report reports/five-leagues-0.5.0a2/baseline-study.json \
    --calibration deploy/paper-automation/five-automation/recommendation-feed/calibration.json "$T2"
python -I $E/probe_deploy_script_wheel_bindings.py --src src deploy/paper-automation

# 新脚本，NBA 计划部分
python -I $E/probe_nba_public_scores.py "$D1/nbaallelo.csv"
python -I $E/probe_nba_news_gain_power.py
python -I $E/probe_nba_polymarket_sample.py "$D3/POLYMARKET-SAMPLE.csv"
python -I $E/probe_nba_package_identity.py --src src

# 旧脚本重跑（T3..T7 是各自的空临时目录）
python -I $E3/probe_big5_scheduler_news_updates.py --src src "$T3" --links-per-day 0,2,6,24
python -I $E3/probe_league_press_labels.py --src src
python -I $E3/probe_absence_scenario_weighting.py --src src
python -I $E3/probe_league_inputs_revision_replay_cost.py --src src "$T4"
python -I $E3/probe_big5_rolling_day_boundary.py --src src "$T5"
python -I $E3/probe_epl_fixed_seal_under_evidence_churn.py --src src --tests tests "$T6" \
    --intervals none,0.05,0.002 --runs 3

# 版本切换演练：两个版本各自独立安装（python3.12 -m venv + pip install -c constraints/research.txt <检出目录>）
sh $E3/replay_release_chain.sh "$T7" inst-fiveauto.1/bin/pm861 inst-0.5.0a4/bin/pm861 inst-fiveauto.1/bin/pm861
```

## 读这些输出时要知道的几点

- `probe_holdout_confidence_slope.py` 先核对自己重算得对不对：五个联赛的场数和 B1 平均分与已发布的基线报告一致到小数点后六位，重建的 494 格校准表逐格与随仓的 `calibration.json` 相同（输出里的 `checks`）。用于分析的是 B1 打分的 1,745 场，另有 6 场走了回退模型，只计入场数核对。区间按“联赛 × 官方轮次”共 182 个单位重抽样 2,000 次，种子固定，重跑结果逐字相同。
- 同一个输出里的 `apparent_edge_by_contract_type` 是把拟合的校准关系反过来用：一份合约的价格如果正好等于校准后的真实概率，模型会给出多少。它依赖 2025/26 一个赛季的拟合，区间很宽。`opened_positions_after_calibration` 里六个仓位的模型概率不是原值，仓库里没有原值，用的是“限价加 0.03、加 0.055、加 0.08”三个假设值；数字没有扣手续费。
- `probe_feed_calibration_bias.py` 里五联赛合并的 z 值是简单二项近似，没有按轮次聚类，只用来指方向。要引用区间请用上一个脚本。
- `probe_nba_news_gain_power.py` 是算术加一次模拟核对，不读任何数据。它假设各场独立、新闻臂完全正确地吸收了消息，这是对新闻臂最有利的设定，所以算出的把握是上限。收盘价终点那一部分依赖“模型与收盘价之间、与新闻无关的差距有多大”这个假设，给了 0.03、0.05、0.08 三档，真实数值要等采集器跑起来才知道。
- `probe_nba_public_scores.py` 用的文件止于 2015 年，没有加时标记。N1 用的是文件自带的 Elo 胜率，N2 只加了两个“背靠背”系数。它说明的是层间差别的量级和一个留出季的分辨力，不代表现在的联盟，也不是计划里 N1、N2 的实现。
- `probe_nba_polymarket_sample.py` 只核对抽样表内部是否自洽、与计划正文是否一致。样本是 2026 年 11 月 27 日 13:25 UTC 的一个时点，离开赛十小时以上，不代表 T−60m 的盘口。买入成本按计划写的费率公式计算，这个费率我没有向交易所核对。
- `probe_nba_package_identity.py` 里的“改动”都是在临时副本的函数体开头加一条不改变行为的语句。它说明的是哪些定义计入足球配方：`market_http.read` 和 `store.digest` 计入，`store.connect` 不计入。
- `probe_deploy_script_wheel_bindings.py` 只解析这些脚本的语法树并导入安装包模块，不运行脚本。它查得出“属性不存在”，查不出参数个数或返回格式的变化。
- `probe_big5_scheduler_news_updates.py` 的 GDELT 索引是合成的，`probe_league_press_labels.py` 的网址是为说明匹配规则写的，`probe_absence_scenario_weighting.py` 不带出场数据。这三点与上一个证据目录的说明相同。
- `switch-fiveauto-a4-fiveauto.txt` 里的具名批准只发生在脚本自己建立的临时目录。不要对真实的数据目录运行 `replay_release_chain.sh`。
- 这台机器访问不了 Polymarket、ESPN、BALLDONTLIE、Big Balls、DunkAPI、NBA 官网、维基百科、GDELT 和 API-Football。部署脚本没有运行过，计划里关于这些站点的条款、接口返回和报价也都没有重取，两份审核稿里凡是来自部署机或计划自己取样的数字都标了“随仓报告”或“随仓证据”。
