"""测量研究影子账户在 T−60 决策窗口里的实际耗时和持有写锁的时间。

0.2.0 里真正会积累样本的是影子账户（正式账户的门禁在真实数据上仍未通过）。本脚本复用
decision_window_benchmark.py 的合成夹具（一场比赛 21 个合约、42 枚方向代币），
调用 shadow.run，并把时钟换成随真实耗时前进的时钟，使 shadow._freeze 里
“在写事务中按实际时刻重新核验条款和首簿”的分支与真实运行一致。
预置的影子订单是“已持有”状态，plan 和 result 取自本场景第一次运行的真实记录。
不修改任何仓库文件，数据写在临时目录。

用法：PYTHONDONTWRITEBYTECODE=1 python3 shadow_window_benchmark.py [仓库根目录] [预置已持有订单数=0,2000,8000]
"""

import io
import json
import runpy
import sys
import time
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
PRELOADS = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [0, 2000, 8000]
BENCH = ROOT / "docs/review-evidence/code-review-2026-11-25/decision_window_benchmark.py"

with patch.object(sys, "argv", [str(BENCH), "21", "0"]), redirect_stdout(io.StringIO()):
    bench = runpy.run_path(str(BENCH))
T = bench["T"]
from polymarket861 import opportunity, shadow, store  # noqa: E402

MODULES = (store, T.pipeline, T.datasets, T.input_manifest, T.evidence, T.evidence_sources, T.archive)


@contextmanager
def realtime(start):
    """模拟时钟 = 起点 + 真实流逝时间。"""
    origin = time.perf_counter()

    def now():
        return store.iso(start + timedelta(seconds=time.perf_counter() - origin))

    with ExitStack() as stack:
        for module in MODULES:
            stack.enter_context(patch.object(module, "now_utc", side_effect=now))
        yield


def preload(root, count, template):
    with shadow.db_for(root) as db:
        db.execute("INSERT OR IGNORE INTO research_shadow_accounts VALUES(?,?,?)", ("research-shadow", "10000", "10000"))
        for i in range(count):
            db.execute(
                "INSERT INTO research_shadow_orders VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("research-shadow", f"old-{i}", f"epl/2025-26/earlier/{i // 6}", f"epl-old-{i // 6}",
                 "2025-10-01T14:00:00+00:00", f"old{i}", f"0xold{i}", "held", "0" * 64,
                 template["plan"], template["result"]),
            )


def measure(existing, template=None, clock="real"):
    case = T.Opportunities()
    case.setUp()
    try:
        seal = case.seal()
        tokens = bench["capture_first_books"](case, T.CUT - timedelta(seconds=7), seal)
        if existing:
            preload(case.root, existing, template)
        spans = {"first": None, "last": None, "freeze_calls": 0}
        freeze = shadow._freeze

        def traced(*args, **kwargs):
            if spans["first"] is None:
                spans["first"] = time.perf_counter()
            spans["freeze_calls"] += 1
            try:
                return freeze(*args, **kwargs)
            finally:
                spans["last"] = time.perf_counter()

        manager = T.at(T.CUT - timedelta(seconds=5)) if clock == "fixed" else realtime(T.CUT - timedelta(seconds=5))
        with manager, patch.object(shadow, "_freeze", traced):
            started = time.perf_counter()
            formal = opportunity.process(case.root, T.SLUG) if clock == "real+formal" else None
            mid = time.perf_counter()
            out = shadow.run(case.root, T.SLUG)
            ended = time.perf_counter()
        states = {}
        for action in out["actions"]:
            states[action["state"]] = states.get(action["state"], 0) + 1
        reasons = {}
        for gap in out["gaps"]:
            reasons[gap["reason"]] = reasons.get(gap["reason"], 0) + 1
        with shadow.db_for(case.root) as db:
            row = db.execute(
                "SELECT plan,result FROM research_shadow_orders WHERE account='research-shadow' AND status='frozen' AND id NOT LIKE 'old-%' LIMIT 1"
            ).fetchone()
        print(json.dumps({
            "clock": clock, "existing_held_orders": existing, "tokens": tokens, "states": states, "gaps": reasons,
            "shadow_run_seconds": round(ended - mid, 3),
            "write_lock_seconds": round(spans["last"] - spans["first"], 3) if spans["first"] else None,
            "formal_seconds": round(mid - started, 3) if formal else None,
        }, ensure_ascii=False), flush=True)
        return {"plan": row["plan"], "result": json.dumps(json.loads(row["result"]) | {"status": "held", "spent": "20", "net_shares": "40"})} if row else template
    finally:
        case.doCleanups()


if __name__ == "__main__":
    template = measure(0, clock="fixed")
    for existing in PRELOADS:
        measure(existing, template, clock="real")
        measure(existing, template, clock="real+formal")
