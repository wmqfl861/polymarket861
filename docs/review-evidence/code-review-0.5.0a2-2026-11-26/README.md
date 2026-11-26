# 审核证据 · 五大联赛 0.5.0a2 · 2026-11-26

本目录是[0.5.0a2 审核稿](../../polymarket861-five-leagues-code-review-0.5.0a2.md)里“实测”和“推算”结论的复现脚本和当次输出。它不属于系统代码，也不修改任何仓库文件。审核对象是提交 `6335047`（0.5.0a2）。对照用的旧版本是 `49313f4`（0.5.0a1）和 `8125d57`（0.4.2）。

## 这次新写的脚本

| 脚本 | 对应结论 | 依赖 | 耗时 |
|---|---|---|---|
| `replay_install_switches.sh` | 同一个英超数据目录在几套各自独立安装的版本之间来回切换：新版本批准前拒绝封存、旧版本这时照常封存、按手册三步做完后 1,900 场里 0 场改变、批准后旧版本被拒绝、批准后再切回去也走得通、旧封存原样重开（T0-1、T2-6） | 每个版本一套独立的非可编辑安装；初始化时要访问 GitHub 取赛程和固定版本的 OpenFootball 文件；全部在临时目录里进行 | 每切一次约 35 秒 |
| `probe_watch_service_heartbeat.py` | 值守服务与 systemd 之间的就绪通知和心跳：用一个仿造的通知套接字接收，一共六遍。路径形式和抽象名字空间形式各一遍，分别用 SIGINT 和 SIGKILL 结束；第三遍让上游从一开始就连不上；第四遍让进程启动时 SIGINT 已经被忽略（从 `nohup` 或脚本里的 `&` 启动就是这样），看 SIGINT 还能不能停下它；第五遍让第一次后台目录刷新抛出 `database is locked`，看进程怎样收场；第六遍在它退出 5 秒后于同一个数据目录上再启动一次（T2-4、U0-1、U1-1、U2-1） | 沿用 `tests/test_big5_market.py` 的合成赛事，不联网，不需要 systemd；时钟是真实时钟 | 约 5 分钟 |
| `probe_big5_refresh_overlap.py` | 值守循环里后台的目录刷新和采样任务同时写库时，循环以 `sqlite3.OperationalError: database is locked` 整个退出；0.5.0a1 相同；加一行 `BEGIN IMMEDIATE` 的源码副本不再出错；加 `--restart` 时每次退出后在原目录上重新进入循环，看采集能不能接着做（U0-1） | 自己生成 10 场合成的意甲比赛，用一只模拟时钟驱动真正的 `watch` 循环；`--latency real` 给每个上游请求加一段真实的随机延迟 | 没有延迟时每组约 10 秒到 2 分钟，加延迟时每轮约 1 分钟 |
| `probe_big5_catalog_age.py` | 某场比赛最近一次成功的目录早于 10 分钟时，固定时窗的订单簿不会去取（U1-1） | 同上，时钟是固定的模拟时刻 | 约 1 秒 |
| `probe_big5_catalog_retry.py` | 常驻的值守循环自己多久重试一次目录：一次刷新失败后要等满 5 分钟；连续两次失败就会丢掉紧接着的固定时窗（U1-1） | 同上，用一只模拟时钟驱动真正的 `watch` 循环 | 约 15 秒 |
| `continue_a1_capture_with_a2.py` | 0.5.0a1 采出来的目录交给 0.5.0a2：周报重新判定为收齐，采集可以接着做（T1-1） | 第一步要用 0.5.0a1 的源码运行上一份证据目录的 `probe_big5_missing_flags.py` | 约 1 秒 |
| `probe_paper_entry_fee_switch.py` | 五国模拟账户的一笔入场在四个联赛、有无赛前标记、手续费开关开或关的 16 种组合下，默认档和排期研究档各得到什么结果（T1-1、U1-2） | 沿用 `tests/test_budget_domestic_archive.py` 的受控入场，不联网 | 约 2 秒 |
| `probe_endpoint_v2_power.py` | 新闻前瞻研究 v2 的把握，用另一个随机种子、逐次调用产品登记后真正使用的估计函数重算说明文档表里的六行，再与开发方发布的数值逐格比较（T1-2） | 读 `reports/news-early-seals-0.4.2.json`；从 `--src` 调用 `news_market.price_gap_slope` | 约 30 秒 |
| `probe_v2_registration.sh` | v2 登记在临时数据目录上的表现：登记不可更改；提前封存不被绑定；方案文字改一个逗号的“以后某一版”在已登记的目录上出不了研究报告，封存不受影响（T1-2） | 一份已经批准 0.5.0a2 的临时数据目录（可用切换演练留下的）；Python 3.12 | 约 30 秒 |
| `probe_reference_check_mutations.sh` | 持续集成新增的跨文件引用检查在三种改动面前都以非零状态退出；只删西甲一条写法对照后，五国研究配方的摘要变了，且只有西甲受影响（T2-1） | 在源码副本上改，不改仓库；第二项要固定版本的 OpenFootball 档案 | 约 40 秒 |

另有一个不是脚本的文件：`u0-1-begin-immediate.diff` 是审核稿 U0-1 建议的那一行修改，相对仓库根目录。它只用在源码副本上，仓库里的产品代码没有改。

## 沿用的脚本

[上一个证据目录](../code-review-0.5.0a1-2026-11-26/)里的 `check_five_league_study.py`、`check_recipe_cross_references.py`、`trace_recipe_coverage.py`、`probe_recipe_mutations.py`、`probe_big5_missing_flags.py`、`probe_big5_watch_faults.py`、`probe_big5_report_during_watch.py`、`check_era_file_churn.py`、`check_report_details.py`，以及[再上一个](../code-review-0.4.2-2026-11-26/)里的 `fetch_openfootball.py`、`probe_empty_wire_reply.py`、`scan_league_assumptions.py`，都在 0.5.0a2 上原样重跑，脚本本身没有改。几份输出里写着 0.5.0a1 或 0.4.2 的说明文字是这些脚本里的固定字样，实际运行的版本以 `package_version` 字段和本说明为准。

## 输出

`outputs/` 是 2026-11-26 在审核环境里运行得到的输出。

- 新脚本的输出：`switch-0.4.2-to-0.5.0a2.txt`、`switch-0.4.2-a1-a2-a1-a2.txt`（两次切换演练的全部输出，后一份连续切换四次）；`probe-watch-service-heartbeat.json`；`probe-big5-catalog-age.json`、`probe-big5-catalog-age-on-0.5.0a1.json`；`probe-big5-catalog-retry.json`、`probe-big5-catalog-retry-on-0.5.0a1.json`；`continue-a1-capture-with-a2.json`、`continue-a2-capture-with-a2.json`（前一份的目录是 0.5.0a1 采的，后一份是 0.5.0a2 自己采的，用来对照）；`probe-paper-entry-fee-switch.json`；`probe-endpoint-v2-power.json`；`probe-v2-registration.txt`；`probe-reference-check-mutations.txt`。
- U0-1 的七份输出：`probe-big5-refresh-overlap.json`（0.5.0a2，没有延迟，10 轮）、`probe-big5-refresh-overlap-on-0.5.0a1.json`（0.5.0a1，同样条件）、`probe-big5-refresh-overlap-with-latency.json`（0.5.0a2，加延迟，6 轮）、`probe-big5-refresh-overlap-after-one-line-change.json` 和 `probe-big5-refresh-overlap-after-one-line-change-with-latency.json`（打了 `u0-1-begin-immediate.diff` 的源码副本，没有延迟 10 轮、加延迟 3 轮）、`probe-big5-refresh-overlap-restarted.json` 和 `probe-big5-refresh-overlap-restarted-with-latency.json`（0.5.0a2 加 `--restart`，没有延迟 10 轮、加延迟 3 轮）。
- 沿用脚本在 0.5.0a2 上的输出：`check-five-league-study.json`、`check-recipe-cross-references.json`、`trace-recipe-coverage.json`、`probe-recipe-mutations.json`、`probe-big5-missing-flags.json`、`probe-big5-watch-faults.json`、`probe-big5-report-during-watch.json`、`check-era-file-churn.json`、`check-report-details.json`、`rerun-probe-empty-wire-reply.json`、`rerun-scan-league-assumptions.json`。
- `environment-and-tests.txt`：审核对象和改动规模；三个 Python 版本和数值库的版本；全部 629 项测试在三个版本上的结果（接了真实的 Chrome for Testing，零跳过）；`ruff`、`mypy --strict`、`scripts/check_league_boundaries.py`、`scripts/check_recipe_references.py`、`git diff --check` 的结果；配方身份在三个解释器上的数值；独立安装与源码树的比较（108 个文件）；独立复审记录的三棵树与仓库的对应；GitHub Actions 在 `6335047` 上的结果。
- `recipe-digest-by-python.json`：`scripts/check_recipe_identity.py` 在三个解释器上的输出合在一起。除 `python` 字段外完全相同。
- `developer-report-reruns.json`：开发方六份报告的重跑记录和与发布文件的逐字节比较。五国回测、历史清册、资金压力测试、新闻盘口研究、v2 把握表相同；新闻吸收报告只有 `/news_policy` 一项不同（0.5.0a1 起新闻策略改过，0.5.0a2 没有再改）。另记了 0.5.0a1 与 0.5.0a2 两份发布的五国回测之间只差新增的 `research_recipe` 一块。

上一份证据目录里的 `estimate_weekend_capture_volume.py` 和 `probe_endpoint_study_power.py` 也重跑过，前者输出与上次逐字节相同，后者只有 `package_version` 一个字段不同，没有再存一份。

## 运行方法

在仓库根目录，已安装 NumPy、SciPy、httpx。`$PY312` 是装好依赖的 Python 3.12，下面没有写解释器的命令都用它。`$HISTORY` 是存放固定档案的目录，`$OUT` 是放输出文件的目录，`$W` 是放三套独立安装的目录，`$A`、`$B` 是两次切换演练各用一个的空临时目录，`$T1` 到 `$T24` 是其余脚本各用一个的空临时目录。

```sh
export PYTHONDONTWRITEBYTECODE=1
E=docs/review-evidence/code-review-0.5.0a2-2026-11-26
E1=docs/review-evidence/code-review-0.5.0a1-2026-11-26
E0=docs/review-evidence/code-review-0.4.2-2026-11-26

# 三套互相独立的安装：各自的源码检出、各自的虚拟环境，都是 Python 3.12，都不是可编辑安装
git worktree add --detach "$W/src-0.4.2" 8125d57
git worktree add --detach "$W/src-0.5.0a1" 49313f4
git clone --single-branch --branch main https://github.com/wmqfl861/polymarket861.git "$W/src-0.5.0a2"
for v in 0.4.2 0.5.0a1 0.5.0a2; do
  python3.12 -m venv "$W/inst-$v"
  "$W/inst-$v/bin/python" -m pip install -c "$W/src-$v/constraints/research.txt" "$W/src-$v"
done

# 版本切换：0.4.2 直接到 0.5.0a2；以及 0.4.2、0.5.0a1、0.5.0a2、退回 0.5.0a1、再到 0.5.0a2
sh $E/replay_install_switches.sh "$A" "$W/inst-0.4.2/bin/pm861" "$W/inst-0.5.0a2/bin/pm861"
sh $E/replay_install_switches.sh "$B" "$W/inst-0.4.2/bin/pm861" "$W/inst-0.5.0a1/bin/pm861" \
    "$W/inst-0.5.0a2/bin/pm861" "$W/inst-0.5.0a1/bin/pm861" "$W/inst-0.5.0a2/bin/pm861"

# 四国采集
python -I $E1/probe_big5_missing_flags.py --src src --tests tests "$T1"
python -I $E1/probe_big5_watch_faults.py --src src --tests tests "$T2"
python -I $E1/probe_big5_report_during_watch.py --src src --tests tests "$T3"
python -I $E1/probe_big5_missing_flags.py --src "$W/src-0.5.0a1/src" --tests "$W/src-0.5.0a1/tests" "$T4"
python -I $E/continue_a1_capture_with_a2.py --src src --tests tests "$T4"
python -I $E/continue_a1_capture_with_a2.py --src src --tests tests "$T1"      # 对照：0.5.0a2 自己采的目录
python -I $E/probe_big5_catalog_age.py --src src --tests tests "$T5"
python -I $E/probe_big5_catalog_age.py --src "$W/src-0.5.0a1/src" --tests "$W/src-0.5.0a1/tests" "$T6"
python -I $E/probe_big5_catalog_retry.py --src src --tests tests "$T7"
python -I $E/probe_big5_catalog_retry.py --src "$W/src-0.5.0a1/src" --tests "$W/src-0.5.0a1/tests" "$T8"
python -I $E/probe_watch_service_heartbeat.py --src src --tests tests "$T9"

# 后台目录刷新和采样任务同时写库（U0-1）；中间三条用打了一行修改的源码副本，最后两条在每次退出后原地重启
python -I $E/probe_big5_refresh_overlap.py --src src "$T17"
python -I $E/probe_big5_refresh_overlap.py --src "$W/src-0.5.0a1/src" "$T18"
python -I $E/probe_big5_refresh_overlap.py --src src "$T19" --latency real --runs 6
cp -R src "$T20/" && patch -d "$T20" -p1 < $E/u0-1-begin-immediate.diff
python -I $E/probe_big5_refresh_overlap.py --src "$T20/src" "$T21"
python -I $E/probe_big5_refresh_overlap.py --src "$T20/src" "$T22" --latency real --runs 3
python -I $E/probe_big5_refresh_overlap.py --src src "$T23" --restart
python -I $E/probe_big5_refresh_overlap.py --src src "$T24" --latency real --runs 3 --restart
python -I $E/probe_paper_entry_fee_switch.py --src src --tests tests "$T10"

# 新闻前瞻研究 v2
python -I $E/probe_endpoint_v2_power.py --src src reports/news-early-seals-0.4.2.json \
    --published reports/five-leagues-0.5.0a2/news-endpoint-power-v2.json
sh $E/probe_v2_registration.sh "$PWD" "$PY312" "$T11" "$A/data"
python -I $E1/check_era_file_churn.py --src src --repo . "$T12" \
    --releases 0.3.0=12d1717,0.4.1=ea58105,0.4.2=8125d57,0.5.0a1=49313f4,0.5.0a2=6335047

# 配方的覆盖范围（后两条用切换演练留下的、已经批准 0.5.0a2 的临时数据目录 $A/data）
python -I $E0/fetch_openfootball.py "$HISTORY"
python scripts/check_recipe_references.py
sh $E/probe_reference_check_mutations.sh "$PWD" "$PY312" "$T13" "$HISTORY"
python -I $E1/check_recipe_cross_references.py --src src
cp -R "$A/data" "$T14/data"
python -I $E1/trace_recipe_coverage.py --src src --data "$T14/data" \
    --inputs "$T14"/data/datasets/football-inputs/*/sources --fixture epl/2026-27/nottingham-forest/chelsea
python -I $E1/probe_recipe_mutations.py --src src --data "$A/data" --python "$PY312" --scratch "$T15" \
    --history "$HISTORY" --fixture epl/2026-27/nottingham-forest/chelsea

# 五国回测，随仓报告的细节，更早的两个探针
OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 python -I $E1/check_five_league_study.py --src src \
    --probe $E0/probe_league_baselines.py --published reports/five-leagues-0.5.0a2/baseline-study.json \
    "$HISTORY" "$OUT/check-five-league-study.json"
python -I $E1/check_report_details.py . --compare "$A/compare-1.json"
python -I $E0/probe_empty_wire_reply.py --src src
python -I $E0/scan_league_assumptions.py .

# 开发方的报告
python -m polymarket861 leagues baseline-study "$HISTORY" --workers 4 --output "$OUT/baseline-study.json"
python -m polymarket861 leagues inventory "$HISTORY" > "$OUT/history-inventory.json"
python -m polymarket861 paper-risk stress --write-input "$T16/scenario.json" --output "$OUT/capital-stress.json"
PYTHONPATH=src python scripts/reproduce_news_market_study.py reports/news-early-seals-0.4.2.json
PYTHONPATH=src python scripts/reproduce_news_absorption.py
PYTHONPATH=src python scripts/reproduce_news_endpoint_power.py reports/news-early-seals-0.4.2.json \
    --output "$OUT/news-endpoint-power-v2.json"
python scripts/check_recipe_identity.py            # 三个解释器各运行一次
```

几点说明：

- 除 `fetch_openfootball.py` 和 `replay_install_switches.sh` 外全部离线运行。审核环境访问不了 Polymarket（代理返回 403），也访问不了 Touchline Wire、OpenLigaDB 等来源，所以没有一个脚本访问这些接口，随仓报告里的实采内容无法重新采集。
- 读取下载档案或随仓数据的脚本都带 `-I` 运行，包和测试的位置用 `--src`、`--tests` 传入。开发方的命令（最后一组）按仓库文档的写法运行，要先设 `PYTHONPATH=src` 或安装这个包。
- 上一份说明里写的 `leagues inventory … --output` 是错的，这条命令没有 `--output`，要像上面这样把标准输出存成文件。
- 审核环境没有 systemd。`probe_watch_service_heartbeat.py` 证明的是进程一侧：就绪和心跳通知按时发出，停止时发出停止通知，出错退出时退出码是 2，看护命令的退出码与文档一致。三个服务单元文件没有真正装载过，`systemctl`、`journalctl`、`loginctl` 的表现，包括进程退出后的自动重启，要以部署机为准。
- 打了 `u0-1-begin-immediate.diff` 的源码副本上还做了两件事，没有另存输出：仓库的全部测试（Python 3.12.3，`Ran 629 tests`，`OK (skipped=3)`，跳过的是没有接浏览器的 3 项浏览器测试）；`scripts/check_recipe_identity.py`，与原来的输出相比只有 `package_files_sha256` 一项不同，B0 到 B3 四个配方的各项摘要都相同。
- 四国采集的几个探针用的是仓库测试自带的合成赛事和订单簿。它们证明的是采集程序怎样处理这些情形，不是交易所实际返回什么。
- `probe_big5_catalog_retry.py` 和 `probe_big5_refresh_overlap.py` 用模拟时钟驱动真正的值守循环：每走一步时钟前进一格（前者 2 秒，后者 60 秒），这一步提交给线程池的任务全部结束之后才走下一步，所以一次目录刷新在模拟时间里是瞬间完成的。真实的刷新要几秒到十几秒，这段时间只会让目录更旧，不会让 U1-1 的结论变好。任务之间谁先谁后是真实线程的先后，U0-1 靠的就是这一点。
- 这两个探针最初的写法是“先等 0.02 秒让任务开始，再等任务做完”。机器忙的时候，任务有可能在时钟前进之后才开始，于是 `probe_big5_catalog_retry.py` 在 0.5.0a1 上有一组的订单簿总数出现过 232 和 220 两种结果（时窗是否收齐不受影响）。现在改为从任务提交起就计数，`probe_big5_catalog_retry.py` 在两个版本上各连跑两次，输出逐字节相同，0.5.0a2 的结果与改之前存下的那一份除新增的一个字段外也完全相同。
- `probe_big5_catalog_retry.py` 改严之前跑过五次，其中一次因为 U0-1 中途退出，U0-1 就是这样发现的。现在碰上时这一组会换一个新目录重来，放弃的次数记在输出里；存下的两份输出里都是 0。
- `probe_big5_refresh_overlap.py` 每一轮在哪一次刷新退出是随机的，重跑时各轮的数字会不同，稳定的是结论：没有延迟时每一轮都很快退出，加延迟时每几十次刷新出一次，打了那一行修改之后一次都没有。加延迟的那一组，探针改严之前跑过一次，是 96 次后台刷新里 4 次；存下的这一份是 116 次里 3 次。不带 `--restart` 的五份输出是在加上这个选项之前生成的；不带它时脚本的做法没有变，加上之后我把没有延迟的两组各重跑了一遍核对过，输出的字段相同，10 轮仍然全部中途退出。
- `--restart` 的重启是在同一个进程里重新调用 `watch`，模拟时钟先前进 5 秒。它说明的是数据目录的状态经得起这样一次退出，采集能接着做；换一个进程重新启动的那一面由 `probe_watch_service_heartbeat.py` 的第六遍说明。没有延迟的那一份里有 114 个采样时段没有完成，都是和后台刷新落在同一步的采样只收到一半或没有收到；探针一步 60 秒，没有真实循环里 2 秒后的重试。
- `probe_watch_service_heartbeat.py` 的第五遍是在目录刷新的位置直接抛出同一个异常，证明的是进程怎样收场，不是异常怎样产生；后者由 `probe_big5_refresh_overlap.py` 证明。第六遍用的是第五遍留下的数据目录。这个探针在子进程启动前把 SIGINT 的处置显式定下来，所以从前台还是后台启动它，结果一样。提交前的那次重跑是从后台启动的，当时还没有这样做，第一遍和第三遍的 SIGINT 因此没有起作用，第四遍就是由此加上的。
- `probe_paper_entry_fee_switch.py` 在手续费开着的那八组里，用英超影子账户的研究用费用估计（`shadow_market._fee`）补上了产品拒绝给出的条款，为的是让回放走到核对那一步。它是探针的做法，不是产品现有的功能。手续费参数（rate 0.05、exponent 1、只收吃单方）取自随仓文档对 11 月 26 日实采合约的记载。
- `replay_install_switches.sh` 和 `probe_v2_registration.sh` 里的具名批准和研究登记只发生在脚本自己建立的临时目录。不要对真实的数据目录运行这两个脚本。
- `probe_endpoint_v2_power.py` 的收盘价是合成的，做法与上一份审核相同，只说明样本量够不够，不说明新闻有没有用。随机种子固定为 20261126。24 格里与开发方数值相差最大的一格是 2.45 个标准误，平均偏差是 −0.12 个标准误，没有系统性的偏离。
- `check_report_details.py` 的第三部分拿这次的 0.4.2 到 0.5.0a2 比较报告去对上一版发布的 0.4.2 到 0.5.0a1 报告，68 个字段里 62 个相同，不同的 6 个都是候选版本自己的身份和运行环境。它在这里的用处是说明基线一侧和评分没有变。
- `continue-a1-capture-with-a2.json` 里的 `slot_records` 是旧版本当时写下的时窗记录，0.5.0a2 不改写它们；现在是否收齐以周报按原件重算的结果为准。
- 提交前我把当时的九个脚本按上面的命令从头又跑了一遍（Python 3.12.3）。`probe_big5_catalog_age.py`（两个版本）、`continue_a1_capture_with_a2.py`、`probe_paper_entry_fee_switch.py`、`probe_endpoint_v2_power.py`、`probe_v2_registration.sh`、`probe_reference_check_mutations.sh` 的输出与 `outputs/` 里的文件逐字节相同。`replay_install_switches.sh` 的输出里有批准时刻和每次新写的封存的摘要，其余各行相同。这之后我只改了这两个 shell 脚本拼接多行输出的写法（`tr '\n' ' '` 换成 `paste -sd' ' -`），为的是去掉行尾多出的一个空格，然后把两个脚本又各跑了一遍，`outputs/` 里存的三份是这最后一遍的输出：`probe-v2-registration.txt` 与前一遍相比只少了 4 处行尾空格；两份切换演练的输出除批准时刻、封存摘要和行尾空格外各行相同，1,900 场 0 场改变、各次拒绝的退出码和四份比较报告的摘要都没有变。`probe_big5_catalog_retry.py` 和 `probe_watch_service_heartbeat.py` 在这次重跑里出了上面两条说的情况，两个脚本都因此改过，`outputs/` 里存的是改过之后的输出；`probe_watch_service_heartbeat.py` 的时刻精确到 0.1 秒，各次运行会有零点几秒的出入。`probe_big5_report_during_watch.py` 里第二个进程完成的轮数取决于机器快慢。
