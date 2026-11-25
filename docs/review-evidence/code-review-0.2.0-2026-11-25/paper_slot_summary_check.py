"""查看比赛日调度进程在一次“成功的” T−60 纸面决策之后，给这个时段留下了什么记录。

调度进程的 season._paper 依次调用正式账户（opportunity.process）和影子账户（shadow.run），
再把两份结果各压成一行摘要写进 season_slots.detail。本脚本复用 decision_window_benchmark.py
的合成夹具（一场比赛 42 枚方向代币），在决策窗口内真实调用 season._paper，
把它返回的摘要和影子订单表里的实际结果放在一起对比。
不修改任何仓库文件，数据写在临时目录。

用法：PYTHONDONTWRITEBYTECODE=1 python3 paper_slot_summary_check.py [仓库根目录]
"""

import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
BENCH = ROOT / "docs/review-evidence/code-review-2026-11-25/decision_window_benchmark.py"
with patch.object(sys, "argv", [str(BENCH), "21", "0"]), redirect_stdout(io.StringIO()):
    bench = runpy.run_path(str(BENCH))
T = bench["T"]
from polymarket861 import ledger, season, shadow  # noqa: E402

case = T.Opportunities()
case.setUp()
try:
    seal = case.seal()
    bench["capture_first_books"](case, T.CUT - timedelta(seconds=7), seal)
    with T.at(T.CUT - timedelta(seconds=6)):
        ledger.open_account(case.root, "main", "formal", role="main")
    with T.at(T.CUT - timedelta(seconds=5)):
        summary = season._paper(case.root, T.SLUG)
    with shadow.db_for(case.root) as db:
        shadow_orders = dict(db.execute("SELECT status,count(*) FROM research_shadow_orders GROUP BY status").fetchall())
    with ledger.connection(case.root) as db:
        formal_intents = dict(db.execute("SELECT status,count(*) FROM paper_intents GROUP BY status").fetchall())
    print(json.dumps({
        "slot_detail_written_by_coordinator": summary,
        "actual_formal_intents": formal_intents,
        "actual_shadow_orders": shadow_orders,
    }, ensure_ascii=False, indent=1))
finally:
    case.doCleanups()
