"""查看影子账户在一场 42 枚代币的比赛里实际冻结了哪几笔、其余为什么被跳过。

复用 decision_window_benchmark.py 的合成夹具（21 个合约、42 枚方向代币，正式账户在同一场景冻结 6 笔）。
对每一笔影子记录打印处理顺序、合约、模型公平价与盘口中点之差、冻结时拿到的预算和计划数量。
不修改任何仓库文件，数据写在临时目录。

用法：PYTHONDONTWRITEBYTECODE=1 python3 shadow_budget_order.py [仓库根目录]
"""

import io
import json
import runpy
import sys
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
BENCH = ROOT / "docs/review-evidence/code-review-2026-11-25/decision_window_benchmark.py"
with patch.object(sys, "argv", [str(BENCH), "21", "0"]), redirect_stdout(io.StringIO()):
    bench = runpy.run_path(str(BENCH))
T = bench["T"]
from polymarket861 import ledger, opportunity, shadow  # noqa: E402

case = T.Opportunities()
case.setUp()
try:
    seal = case.seal()
    bench["capture_first_books"](case, T.CUT - timedelta(seconds=7), seal)
    with T.at(T.CUT - timedelta(seconds=6)):
        ledger.open_account(case.root, "main", "formal", role="main")
    with T.at(T.CUT - timedelta(seconds=5)):
        formal = opportunity.process(case.root, T.SLUG)
        out = shadow.run(case.root, T.SLUG)
    print("正式账户：", {s: sum(d["state"] == s for d in formal["decisions"]) for s in ("frozen", "skipped")})
    with shadow.db_for(case.root) as db:
        rows = db.execute("SELECT rowid,* FROM research_shadow_orders ORDER BY rowid").fetchall()
    print("影子账户：", {s: sum(r["status"] == s for r in rows) for s in ("frozen", "skipped")})
    print("顺序  状态      合约            方向   公平价−中点   预算    计划数量  跳过原因")
    for n, r in enumerate(rows, 1):
        plan, res = json.loads(r["plan"]), json.loads(r["result"])
        c = plan["intent"]["contract"]
        book = plan["first_book"]
        mid = (Decimal(book["bids"][0]["price"]) + Decimal(book["asks"][0]["price"])) / 2
        edge = Decimal(plan["fair_low"]) - mid
        if r["status"] == "frozen" or edge > 0:
            print(f"{n:>3}   {r['status']:<8}  {c['family']}{' ' + str(c.get('line')) if c.get('line') else '':<10} {c['side']:<6} {edge:+.4f}      {plan['budget']:>6}  {plan['quantity']:>8}  {res.get('reason') or ''}")
finally:
    case.doCleanups()
