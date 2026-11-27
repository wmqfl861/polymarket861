# 0.5.0a3+fiveauto.1 独立复核 · 复现材料

对应[审核稿](../../polymarket861-five-leagues-code-review-0.5.0a3-fiveauto.1.md)。审核对象是 `main` 的 `c613a70`（版本号 0.5.0a3+fiveauto.1），以及它之前的 0.5.0a3（`11a00f0`）和 0.5.0a3+eplfix.1（`0ae69f0`）。

所有脚本都不联网（版本切换演练除外，它要从 GitHub 取固定版本的 OpenFootball 档案），不读写仓库，不触碰任何真实数据目录。脚本用 `python -I` 运行，源码和测试目录用 `--src`、`--tests` 传入。实测环境是 Python 3.12.3、numpy 2.3.5、scipy 1.17.0。

## 这次新写的脚本

| 脚本 | 回答什么问题 | 输出 | 耗时 |
|---|---|---|---|
| `probe_epl_fixed_seal_under_evidence_churn.py` | 英超热修复之后，别的线程不断提交赛程相关证据时，固定窗口的正式封存还能不能按时封出来（V2-1） | `probe-epl-fixed-seal-under-evidence-churn.json`、`…-slow-verification.json` | 约 1 分钟、约 4 分钟 |
| `probe_big5_scheduler_news_updates.py` | 四国研究调度的“新闻更新”封存由什么触发、概率变不变；调度停了以后状态显示什么；轮询线程出错有没有人知道（V1-1、V1-2） | `probe-big5-scheduler-news-updates.json` | 约 20 秒 |
| `probe_league_press_labels.py` | 四国新闻链接按什么字样归到俱乐部头上，哪些报道会漏、哪些会错配（V1-3） | `probe-league-press-labels.json` | 1 秒 |
| `probe_absence_scenario_weighting.py` | 四国“伤停情景”对主力和替补缺阵各算多大，和英超口径差多少（V1-4） | `probe-absence-scenario-weighting.json` | 1 秒 |
| `probe_big5_rolling_day_boundary.py` | 四国值守改成滚动窗口之后，跨 UTC 零点时目录怎么刷新、采样有没有中断 | `probe-big5-rolling-day-boundary.json` | 约 5 秒 |
| `probe_league_inputs_revision_replay_cost.py` | 四国调度每一轮重放全部维基百科修订的耗时随份数怎么涨（V2-3） | `probe-league-inputs-revision-replay-cost.json` | 约 15 秒 |
| `replay_release_chain.sh` | 同一份英超数据目录在 0.5.0a2、0.5.0a3、eplfix.1、fiveauto.1 之间依次交接再退回，哪一步需要比较和具名批准 | `switch-a2-a3-eplfix-fiveauto-eplfix.txt` | 约 6 分钟 |

`replay_release_chain.sh` 是[上一个证据目录](../code-review-0.5.0a2-2026-11-26/)里 `replay_install_switches.sh` 的改写：新旧两版配方相同的时候，`forecast release-compare` 会以退出码 2 拒绝（“已是当前配方”），原脚本在这里中止，新脚本记下这一句后继续。

## 原样重跑的旧脚本

下面这些脚本在[上一个证据目录](../code-review-0.5.0a2-2026-11-26/)里，脚本本身没有改，对着 `c613a70` 重跑，输出存在本目录的 `outputs/rerun-*`。

| 脚本 | 上次（0.5.0a2）的结果 | 这次（0.5.0a3+fiveauto.1）的结果 |
|---|---|---|
| `probe_big5_refresh_overlap.py`（U0-1） | 不带延迟 10 轮全部退出；带延迟 6 轮退出 3 轮 | 不带延迟 10 轮、带延迟 6 轮，共 400 次后台刷新，0 次退出 |
| `probe_big5_catalog_age.py`（U1-1） | 目录超过 10 分钟没刷新时两个固定时窗丢 1 到 2 个 | 10、12、30 分钟三种情形两个时窗都收齐；上游一直不通时仍是 0/2，原因写明 |
| `probe_big5_catalog_retry.py`（U1-1） | 六种情形里三种丢掉决策时窗 | 六种情形全部收齐 |
| `probe_watch_service_heartbeat.py`（U0-1、U2-1） | 注入一次锁错误后进程以退出码 2 结束；启动时已忽略 SIGINT 的进程停不下来；上游不通时看护命令仍报健康 | 进程继续运行；SIGINT 能停、退出码 130；上游不通时看护命令返回 1 |
| `probe_paper_entry_fee_switch.py`（U1-2） | 研究档 0/8 成交 | 研究档 8/8，默认档仍 0/8 |
| `probe_v2_registration.sh`（U1-3） | 登记不可更改；方案文字变了的版本出不了报告 | 行为相同，报错写明了新旧摘要 |
| `probe_endpoint_v2_power.py`（U1-3） | 24 格与发布值最大相差 2.45 个标准误 | 逐格相同 |
| `probe_reference_check_mutations.sh`（第一项） | 三种改动都以退出码 1 结束 | 相同 |

`probe_reference_check_mutations.sh` 的第二项（删掉西甲一条写法对照后重做五国回测）这次没有重跑：它涉及的 `league_registry.py`、`league_study.py`、`football.py`、`football_model.py` 自 0.5.0a2 以来没有改动（`git diff 6335047 c613a70` 对这四个文件为空）。

## 命令

```sh
REPO=<c613a70 的检出目录>; E=$REPO/docs/review-evidence/code-review-0.5.0a3-fiveauto.1-2026-11-27
E2=$REPO/docs/review-evidence/code-review-0.5.0a2-2026-11-26
cd $REPO

# 测试和持续集成的各步（三个解释器各跑一遍 unittest）
python -m unittest discover -s tests
python scripts/check_league_boundaries.py && python scripts/check_recipe_references.py
ruff check src tests scripts
python scripts/check_recipe_identity.py > recipe-3.12.json     # 3.11、3.13 同样各生成一份，再 --compare

# 新脚本（T1..T6 是各自的空临时目录）
python -I $E/probe_epl_fixed_seal_under_evidence_churn.py --src src --tests tests "$T1" \
    --intervals none,0.05,0.01,0.002 --runs 5
python -I $E/probe_epl_fixed_seal_under_evidence_churn.py --src src --tests tests "$T2" \
    --intervals none,3,1.5,0.8,0.3 --runs 3 --verify-seconds 1
python -I $E/probe_big5_scheduler_news_updates.py --src src "$T3" --links-per-day 0,2,6,24
python -I $E/probe_league_press_labels.py --src src
python -I $E/probe_absence_scenario_weighting.py --src src
python -I $E/probe_big5_rolling_day_boundary.py --src src "$T4"
python -I $E/probe_league_inputs_revision_replay_cost.py --src src "$T5"

# 版本切换演练：四个版本各自独立安装（python3.12 -m venv + pip install -c constraints/research.txt <检出目录>）
sh $E/replay_release_chain.sh "$T6" inst-0.5.0a2/bin/pm861 inst-0.5.0a3/bin/pm861 \
    inst-eplfix.1/bin/pm861 inst-fiveauto.1/bin/pm861 inst-eplfix.1/bin/pm861

# 旧脚本重跑
python -I $E2/probe_big5_refresh_overlap.py --src src "$T7"
python -I $E2/probe_big5_refresh_overlap.py --src src "$T8" --latency real --runs 6
python -I $E2/probe_big5_catalog_age.py --src src --tests tests "$T9"
python -I $E2/probe_big5_catalog_retry.py --src src --tests tests "$T10"
python -I $E2/probe_watch_service_heartbeat.py --src src --tests tests "$T11"
python -I $E2/probe_paper_entry_fee_switch.py --src src --tests tests "$T12"
sh $E2/probe_v2_registration.sh "$PWD" "$PY312" "$T13" "$T6/data"      # 用切换演练留下的、已批准当前配方的临时目录
python -I $E2/probe_endpoint_v2_power.py --src src reports/news-early-seals-0.4.2.json \
    --published reports/five-leagues-0.5.0a2/news-endpoint-power-v2.json
sh $E2/probe_reference_check_mutations.sh "$PWD" "$PY312" "$T14"       # 两个路径参数都要写绝对路径
pm861 --data-dir <$T6/data 的副本> leagues news-endpoints preflight --version v2
```

## 读这些输出时要知道的几点

- `probe_epl_fixed_seal_under_evidence_churn.py` 里“另一个线程提交证据”是直接执行触发器做的那一句 `UPDATE epl_schedule_epoch SET revision=revision+1`，间隔是真实秒数；封存流程里的时刻由测试夹具的模拟时钟给出。它量的是“两次证据提交的间隔”与“一次赛程核验的耗时”的比值，不是真实部署的耗时。`--verify-seconds 1` 给每次核验加 1 秒等待，用来模拟真实库上更慢的核验。不带延迟的那份里，没有外来提交的对照组也有一次在第一个轮询点报“证据反复变化”、下一个轮询点封存，这是同一轮里目录和赛果任务自己提交证据造成的，与仓库比赛日集成测试接受的两种结果一致。
- `probe_big5_scheduler_news_updates.py` 的 GDELT 索引是合成的，链接域名是 example.com。它说明的是触发规则和每份封存的体积，真实的链接出现速度见审核稿引用的部署报告。名单库是空的，所以每份封存的体积不含两队名单。
- `probe_league_press_labels.py` 的网址是为了说明匹配规则写的，不代表真实报道，也不代表真实的漏配、错配比例。
- `probe_absence_scenario_weighting.py` 里“英超口径”一列是按 `news_model.POLICY` 的常数手算的，不是英超代码的输出。
- `probe_big5_rolling_day_boundary.py` 每一轮只派发最多 4 个采样任务（产品的并发上限），模拟时钟一轮前进 60 秒，所以每分钟完成的采样数少于比赛场数，这是模拟步长造成的，不是产品行为；00:29 那一分钟落在 T−60 分钟固定时窗里。
- `switch-a2-a3-eplfix-fiveauto-eplfix.txt` 里的具名批准只发生在脚本自己建立的临时目录。不要对真实的数据目录运行 `replay_release_chain.sh` 和 `probe_v2_registration.sh`。
- `rerun-probe-big5-refresh-overlap.json` 第 3 轮（`run: 2`）有 1 个采样任务的状态是 `partial`：这场比赛的目录行在“选合约”和“取订单簿”之间被后台刷新换成了新的收据，程序按设计放弃这一次（`catalogue changed before book request`），不计入重试上限。5,750 个任务里出现 1 次，带延迟的 3,450 个里 0 次。
- 这台机器访问不了 Polymarket、维基百科、GDELT、Touchline Wire、OpenLigaDB 和 API-Football，所以真实采集无法在这里重做，审核稿里凡是来自部署机的数字都标了“随仓报告”。
