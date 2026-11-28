# NBA 0.6.0a2 独立工程验收

**结论：第一阶段本次软件修订 PASS。第二阶段准入仍未满足。** 2026 年 11 月 28 日，独立审核员以 `gpt-6.2-astra`、`max` 推理强度复核。审核员未参与产品实现。相对基线 `36faed38664b3ce9f703a31f32eb280120083302`，本次验收锁定提交 `dd2d67257812a46803baf8638203827c6f34e589`，Git 树 `d87741329d10fe9b31c4defae464dcd55d6c76b8`。以下冻结结果都来自单独解包的 `git archive`，运行解释器为 Python 3.12.14。[上一轮审核](../polymarket861-nba-phase1-code-review-0.6.0a1.md) · [本次脚本和输出](../review-evidence/nba-0.6.0a2/system/)

## 原生盘口、双源时序与更正

独立程序通过正常 CLI 的 `data sync`、`markets collect`、`run --component forecast`、`forecast-show` 和 `report` 完成 25 个情景。程序在 HTTPX 传输层应答，并控制墙上时钟。比赛规则和盘口结构取自一场有原件的真实挂牌，BDL 行、延时、行情变更及终局用例是合成数据。没有直接写入产品的 SQLite 表。

| 条件 | 冻结源码实测 |
| --- | --- |
| 双方开赛时刻相差 +2 或 −2 分钟 | 在两个五分钟窗交集内收到并落库的四类价格全部进入实际封存。原生窗先于交集采到价格时，后续触发另取一次 `/books`，封存采用 00:27:10.100 UTC 的第二份报价。 |
| 相差 ±5、+10、+31 分钟和 +3 小时 | 各例均能独立封球队预测，四类同窗基准均为空。±30 分钟仍可识别同日同主客的比赛；31 分钟及 3 小时显示 `outside_tolerance`。 |
| 迟到、无报价与提前开赛 | 原生截止前 2 秒发出、3 秒后返回的簿没有生成选择。无盘口时第一次封存触发顺延，最后 65 秒的触发封存竞技预测。BDL 比原生早 10 分钟开赛且其开赛时刻已过时，收盘记录为零。 |
| 两个 Gamma 赛事，或同主客同美东日有两条 BDL 身份 | Gamma 歧义报 `ambiguous_venue_fixture`。BDL 一条精确排期、另一条已开打且 `datetime=null` 时，报告 `ambiguous_score_source_fixture`，没有封存。只有无精确时刻的已开赛行时，报告 `score_source_observed_play`，没有合格盘口。 |
| BDL 改期前后均有合法原生价格 | BDL 后移 1 分钟，旧报价仍落在新旧窗口交集内。新排期公布于 00:27:45.090 前，原 00:27 价格被排除。之后没有新价时新封存为空，有新价时只用 00:28:10.100 的四类报价。后移 10 分钟的新版本同样未继承旧价。 |
| 定时器相位和预算 | 两档网络延迟、11 种相位、每种 12 个偏移，共 264 次，264 次封存及配对，重跑与入仓结果一致。15 场同时采集时先发 24 请求、下一轮 8 请求，滚动 60 秒峰值 24，最后覆盖 15 场 60 个家族。429 的 125 秒冷却期内再触发实际新增请求为零，期满后恢复。 |

冻结前曾实测到无精确时刻的开赛行被忽略、不重叠的 +10 分钟盘口被配对，以及首笔原生报价占用后再无交集报价。以上同一批输入在冻结树上的输出已纠正。[HTTP 边界结果](../review-evidence/nba-0.6.0a2/system/extremes-frozen.json) · [相位结果](../../reports/nba-phase1/a2-timer-phases.json)

## 真实公开原件与兑付

| 数据级别 | 独立核对结果 |
| --- | --- |
| 11 场赛前公开截面，22 份原始响应 | 按挂牌类型、球队及 outcome 名称独立列候选，用十进制价格对两侧全部深度重算最优买卖价，再按中间价距 0.50、condition ID 重新排序。821 条完整比赛挂牌含 486 条四类预测线、335 条让分，两个 token 共 1,642 本，单次最大 192 个 token。44 个场次/家族组合中 33 个有合格选择、11 个无挂牌，8 条 alternate 入选。逐项与冻结报告一致。 |
| 5 个实际固定窗，10 份 Gamma/CLOB 响应 | 逐份重算 SHA-256，核对两侧 token、原生盘口中间价及收件、落库时刻。4 场 T−60m 有 16 份合格参考及 4 条让分主线原件。另 1 场 T−24h 有胜盘，其余三类未挂。 |
| 公开终局，5 场加时、1 场四节对照 | 核对审核现场留存的 24 份比分、Gamma、双 token 原始响应与冻结报告摘要。逐节加总符合终场，加时场第四节结束时均平分。用终场判全部 6 条大小线，原生双边兑付逐笔吻合；其中 3 条若采用第四节结束比分会反转。Gamma 的 `VFT` 本身不能证明加时。冻结报告列有原文 URL 和摘要。 |
| 模拟实收顺序和比分更正 | 胜盘随补比分、改比分、撤回、恢复依次产生 `awaiting_score → match → disagrees → awaiting_score → match` 五版。加时去重数随证据变化为 0、1、0、0、1，50:50 场次始终单列。原封存 SHA 和早期日报修订保持可重读。 |
| 本次无凭据真实 CLI | 复核时第一次线上采集返回 15 份 HTTP 200，实收 4 场 T−60m 共 16 个合格价格，另有 T−24h 胜盘。冻结后以全新目录再次运行 `nba run --component markets` 和独立盘口 watchdog，均 `ok`，4 页目录有 22 场，运行时未到新报价窗口。两次均使用公开只读入口。 |

[11 场独立核算](../review-evidence/nba-0.6.0a2/system/originals-frozen.json) · [固定窗独立核算](../review-evidence/nba-0.6.0a2/system/fixed-frozen.json) · [历史兑付报告](../../reports/nba-phase1/v2-overtime-reconciliation.json)

## 安装、隔离与足球私账前置条件

从冻结内容构建并安装 `0.6.0a2` wheel，从 `60c1a2d` 单独构建并安装已发布的 `0.5.0a4` wheel。隔离解释器均从各自的安装目录导入。两套虚拟环境借用本机已安装的数值依赖。逐个执行安装包对应的市场、赛程、封存、看门狗、来源探针五个 service 命令，全部退出 0。带非空赛程、模拟 HTTP 的安装演练封出 `bdl:32` 的 T−60m、四类价格和 N0/N1/N2；`readiness` 为 `collecting`。本容器没有用户 systemd 总线及 cgroup，未启动实际 timer 或模拟部署机运行时长。[安装记录](../review-evidence/nba-0.6.0a2/system/install-frozen.json)

新旧足球包分别对 NBA 私有目录执行 `jobs status`、`fixture list`，新版 NBA 对足球目录执行 `markets collect`，五次均退出 2，目标逐文件摘要不变。B0–B3 的 primary/paired 八个配方摘要通过基准检查，101 个足球部署引用通过。另独立跑完冻结树 137 个 NBA 测试和 19 个足球模拟账户、通知测试，全部通过。

用六个必须存在的原仓位 ID 构造合成账本，以真实旧引擎生成已发布状态，在旧包解释器上从预检脚本的命令行重放。6 仓、5 次兑付（含同期及零兑付）、1 笔未结，两引擎和公布账表逐笔相同，现金 10,000、已结盈亏 50、未结成本 50。改动未结仓原买价时退出 1，指出具体仓位、现金和未结成本；修改已结仓报价或移走第六仓也均退出 1。四轮中被检查的 42 份原文件摘要均保持一致。真实六仓账本、原 `/tmp` 选单、生产看门狗与足球同机 429 仍须在原部署环境核对。[六仓预检四个结果](../review-evidence/nba-0.6.0a2/system/football-frozen.json)

## 复现入口与现场留项

从含本报告和独立脚本的 Git 工作区执行。`python` 使用已有研究依赖的解释器。命令在临时目录展开受审提交，保留 Git 工作区以读取已发布的旧版。

```bash
repo=$(pwd)
audit_dir="$repo/docs/review-evidence/nba-0.6.0a2/system"
audit_out=$(mktemp -d)
export AUDIT_SOURCE="$audit_out/src-a2" PYTHONDONTWRITEBYTECODE=1
mkdir "$AUDIT_SOURCE"
git -C "$repo" archive dd2d67257812a46803baf8638203827c6f34e589 > "$audit_out/frozen.tar"
tar -xf "$audit_out/frozen.tar" -C "$AUDIT_SOURCE"
cd "$AUDIT_SOURCE"
export PYTHONPATH="$AUDIT_SOURCE/src"
python "$audit_dir/audit_http.py" --out "$audit_out/extremes.json"
python "$audit_dir/audit_fixed.py" "$AUDIT_SOURCE"
python scripts/verify_nba_market_lines.py
python docs/review-evidence/code-review-0.6.0a1-nba-phase1-2026-11-28/probe_nba_pairing_by_timer_phase.py --src src --tests tests
python -m unittest discover -s tests -p 'test_nba*.py'
PYTHONPATH=src:tests python -m unittest test_football_paper_review test_paper_opening_notifications
python "$audit_dir/audit_install.py" "$AUDIT_SOURCE" "$repo" --work "$audit_out"
git -C "$repo" archive 60c1a2d > "$audit_out/football-legacy.tar"
mkdir "$audit_out/football-legacy"
tar -xf "$audit_out/football-legacy.tar" -C "$audit_out/football-legacy"
python "$audit_dir/audit_football.py" "$AUDIT_SOURCE" "$audit_out/football-legacy" "$repo" --work "$audit_out"
```

缺带钥 BDL 认证样本及三个完整的双源对齐比赛日，缺部署机 14 个成熟比赛日和连续 7 个比赛日的合格球员来源。冻结版以新采公开目录运行 `nba readiness` 时四项闸门均关闭，`phase_two_activated=false`。本报告通过的是可交付的软件行为；实际部署、现场足球账本及第二阶段授权须分别取得现场证据。[部署步骤与门槛](OPERATIONS-0.6.0a2.md)
