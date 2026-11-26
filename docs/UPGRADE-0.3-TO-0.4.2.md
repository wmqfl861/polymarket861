# 安装 0.4.2 或保留已有预测记录

首次部署可以直接安装 0.4.2。按[比赛日手册](MATCHDAY.md)在新目录准备历史数据、核对赛程并启动采集。若要继续使用 0.3.0 或 0.4.1 的运行库，下面的操作会保留旧预测、收件时间和本机的版本决定。

## 用旧版本保存基线和备份

选择两个赛前决策窗口之间的维护时段。暂停写入该运行库的采集进程。使用仓库自带的 Linux 用户服务时，运行 `systemctl --user stop polymarket861-watchdog.timer polymarket861-season.service`。

在下面的 shell 示例中，`OLD` 和 `NEW` 分别指向原安装环境和 0.4.2 环境中的 `pm861` 可执行文件，`STATE` 是该服务使用的绝对运行目录，`HISTORY` 是包含 2018-19 至 2025-26 原始历史文件的目录。`BACKUP` 和 `RESTORED` 指向两个尚未使用的目录。保留旧安装环境，方便恢复。

```sh
"$OLD" --data-dir "$STATE" forecast release-baseline "$HISTORY"
"$OLD" --data-dir "$STATE" backup create "$BACKUP" --compress
"$OLD" --data-dir "$STATE" backup verify "$BACKUP"
"$OLD" --data-dir "$STATE" backup restore "$BACKUP" "$RESTORED"
```

已有封存时，任选一份摘要，在原库与恢复副本分别运行 `forecast get`，检查两个文件逐字节相同。`OLD_SEAL` 是这次选用的预测摘要。

```sh
"$OLD" --data-dir "$STATE" forecast get "$OLD_SEAL" > old-seal.json
"$OLD" --data-dir "$RESTORED" forecast get "$OLD_SEAL" > restored-seal.json
cmp old-seal.json restored-seal.json
```

## 核对新版本并启动采集

在单独的 Python 环境按仓库 `constraints/research.txt` 安装 0.4.2。对准备继续使用的同一 `STATE` 执行下列命令。`ROUND` 是要检查的官方轮次，示例赛季从 2026 年开始。已安装的程序和当前生效的版本不同时，`release.state` 显示 `approval_required`。

```sh
"$NEW" --data-dir "$STATE" season readiness --year 2026 --round "$ROUND"
"$NEW" --data-dir "$STATE" forecast release-compare "$HISTORY"
"$NEW" --data-dir "$STATE" forecast release-report "$REPORT_SHA256"
```

将比较结果的 `report_sha256` 填入 `REPORT_SHA256`。先核对旧、新实现摘要、五季配对场数、训练输入变化及各盘口概率差异。具名负责人接受报告后，填写自己的 `REVIEWER` 和 `REVIEW_REASON` 并执行签署。签署属于当前运行库。

```sh
"$NEW" --data-dir "$STATE" forecast release-approve "$REPORT_SHA256" --reviewer "$REVIEWER" --reason "$REVIEW_REASON"
"$NEW" --data-dir "$STATE" season readiness --year 2026 --round "$ROUND"
"$NEW" --data-dir "$STATE" forecast get "$OLD_SEAL" > upgraded-seal.json
cmp old-seal.json upgraded-seal.json
```

确认 `release.ready` 为 `true`，`active_recipe_sha256` 与 `installed_recipe_sha256` 一致。将采集进程与看门狗的可执行路径指向新环境，核对两者仍使用同一 `STATE`。Linux 用户服务可运行 `systemctl --user daemon-reload`，再运行 `systemctl --user start polymarket861-season.service polymarket861-watchdog.timer`。通过 `season readiness` 查看协调器心跳、下一决策窗及仍需等待的原始收件。

遇到错误时保留现场。可让旧程序在已核实的 `RESTORED` 恢复副本上运行，恢复点取决于备份时间。[两份旧运行库的升级验证](../reports/news-upgrade-0.4.2.json)保存了五季对照、测试副本签署、升级前后封存摘要和成功生成的新预测。
