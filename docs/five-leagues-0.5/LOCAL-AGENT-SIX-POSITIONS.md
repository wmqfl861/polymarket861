# 本地 agent 执行单 · 足球首六仓保全与复核

原足球模拟账户所在机器收到本次源码更新后，由负责更新的 agent 执行。先保全 2026-11-27 首次六场选单的原件，再核对现役引擎、候选引擎和已发布账表。此项工作可在 NBA 密钥申请前独立完成。工作到产出本机回执为止，服务升级另按部署计划进行。

## 找到现役账户

从部署仓库根目录进入，使用原足球服务账户和 Linux/WSL 环境。默认账本目录是 `.local-deploy/paper-10000`，解释器是现役足球 release 下的 `venv/bin/python`。自定义部署先用下面的只读命令核实 `User`、`WorkingDirectory` 和 `ExecStart`，以绝对路径设置 `PM861_FOOTBALL_ACCOUNT_DIR`、`PM861_FOOTBALL_PYTHON`。解释器使用服务中写明的虚拟环境路径，保留末尾的 `venv/bin/python`。

```bash
systemctl show pm861-paper-10000.service -p User -p WorkingDirectory -p ExecStart
```

确认该目录属于原账户。账本输入、已发布账表、兑付日志分别是 `account-input.json`、`account-status.json`、`redemptions.json`。新装空库标记为“不适用”；曾部署旧仓但路径暂时找不到时，继续查该服务配置和已知备份。先处理易失的原件，即使账本文件暂时不齐。回放需要的临时空间约为收据库与关联原文的一份副本，可将 `TMPDIR` 指向账户目录外有足够空间的本机私有目录。

## 归档并回放

以下整段使用 Bash，从仓库根目录执行。两条分支均使用同一份经审核的候选工具。脚本从固定 Git 对象导出到本次回执目录，现役引擎和历史账表作为输入。缺少该对象时只从本项目的 `origin` 获取。若原件仍在临时目录、但 Git 或解释器暂时不可用，先把整个原件目录用 `cp -a` 复制到新建的本机私有持久目录，保留源目录；工具可用后再按以下流程核验。

```bash
(
  set -euo pipefail
  umask 077
  repo=$(git rev-parse --show-toplevel)
  account=${PM861_FOOTBALL_ACCOUNT_DIR:-"$repo/.local-deploy/paper-10000"}
  football_python=${PM861_FOOTBALL_PYTHON:-"$repo/.local-deploy/releases/0.5.0a4-6844caf/venv/bin/python"}
  original=${PM861_SLATE_SOURCE:-/tmp/pm861-weekly-value-20261127}
  archive="$account/source-evidence/pm861-weekly-value-20261127"
  reports=${PM861_FOOTBALL_PREFLIGHT_DIR:-"${XDG_STATE_HOME:-$HOME/.local/state}/polymarket861/football-six-preflight"}
  pin=ee42baea050de15a26df5695b1a5b0ebbf72a763
  folder=deploy/paper-automation/paper-10000

  mkdir -p -- "$reports"
  run=$(mktemp -d -- "$reports/$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")
  printf '本机回执 %s\n' "$run"
  printf '%s\n' "$pin" > "$run/tool-commit.txt"
  test -d "$account"
  if ! git -C "$repo" cat-file -e "$pin^{commit}" 2>/dev/null; then
    git -C "$repo" fetch --no-tags origin "$pin" ||
      git -C "$repo" fetch --no-tags origin development/nba-reviewed
  fi
  for name in archive_slate_evidence.py check_account_upgrade.py account_engine.py; do
    git -C "$repo" show "$pin:$folder/$name" > "$run/$name"
  done

  "$football_python" -I -B "$run/archive_slate_evidence.py" --archive-or-verify \
    --source "$original" --destination "$archive" 2>&1 | tee "$run/archive.log"
  sha256sum -- "$archive/_pm861_sha256.json" > "$run/manifest.sha256"

  if cmp -s -- "$account/account_engine.py" "$run/account_engine.py"; then
    printf '%s\n' '现役引擎与候选已相同。原件已归档，请定位升级前的一致备份再核对旧版。' |
      tee "$run/baseline-unverified.txt"
    exit 1
  fi
  status=0
  "$football_python" -I -B "$run/check_account_upgrade.py" \
    --account-dir "$account" --candidate-engine "$run/account_engine.py" \
    > "$run/preflight.json" 2> "$run/preflight.stderr" || status=$?
  printf '%s\n' "$status" > "$run/preflight.exit"
  cat -- "$run/preflight.json" "$run/preflight.stderr"
  exit "$status"
)
```

原件在原部署会话可见的 `/tmp/pm861-weekly-value-20261127`。归档器要求至少有 `forecasts.json`、`markets.json`，并逐文件计算 SHA-256。已有同名归档时，它核对清单和原件。临时原件已消失时，它核验已存在的归档。若需从已知备份取回散装原件，设置 `PM861_SLATE_SOURCE` 指向恢复到私有目录的完整原件，再执行整段。若备份本身含 `_pm861_sha256.json`，先用归档工具的 `--verify --destination 备份归档目录` 核实；需要重建规范归档时从已验副本复制原件到新的暂存目录，仅在该暂存目录排除清单，再将其用作输入。保留所有原件、备份及相互冲突的副本。

回放器在私有快照内按已发布账表的 `as_of` 核对最初六个仓位 ID 和之后的全部已发布仓位，逐笔比对合约、token、成交、费用和兑付，最后核对现金、未兑付成本及已结算盈亏。检查沿用现役足球解释器，可在监控正常运行时进行。回执和原始账务数据留在本机。

## 验收与异常

| 本机结果 | agent 应采取的动作 |
| --- | --- |
| `archive.log` 有 `Verified`，退出码为 0，`preflight.json` 为 `match` | 确认 `positions_checked >= 6`，`deployed`、`candidate` 的两类差异列表均为空。汇报账表截止时刻、仓位数、兑付数和本机回执路径。零笔兑付也可以通过。 |
| 原件与归档缺失、清单损坏或两份内容不同 | 自动查原服务账户可读的已知备份，核对来源。未能找回时报告缺失的路径或冲突文件，保留双方副本及原账；历史预测和盘口只能由当时保存的原件证明。 |
| `unverified`、读权限或空间不足、复制时文件变化 | 依据错误检查路径、原解释器、可用空间及监控写入时刻。用可访问的私有暂存目录，在下一次稳定间隔重试；每次留一份回执。 |
| 现役脚本已被覆盖 | 查找能证明升级前部署状态的一致备份，在备份的私有工作副本上指定账户目录和原解释器后重跑。将结果标明为“备份截至某时刻”，保留现场归档；当前账户截至时刻的旧版对账如仍缺证据，单独列出。 |
| `mismatch` | 从两版差异字段及仓位 ID 查对应的两次订单簿原文、成交或兑付回执。保留现役服务、原账和差异报告；把查到的原因以及仍需持有人提供的材料或决定列入总结。 |

已有本次工具版本、同一账表截止和同一账本输入的成功回执时，可复用经核实的结果。出现新兑付、账表变化或原件疑问时，重跑会产生新的回执。用例、演示账户及重新下载的市场数据仅用于测试。提交给持有人的摘要只需结果、计数、本机路径和仍待处理的具体问题。[固定版本的归档器、回放器和候选引擎](https://github.com/wmqfl861/polymarket861/tree/ee42baea050de15a26df5695b1a5b0ebbf72a763/deploy/paper-automation/paper-10000)
