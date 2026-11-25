"""检查比赛日调度进程在“纸面决策整段失败”时会不会告警。

构造一个到期的 T−60 纸面决策时段，让正式账户和影子账户的处理函数都抛出异常，
然后走调度进程自己的收尾函数 Coordinator._finish，查看该时段最终的状态和告警表。
对照组是同一时段的盘口采集失败（采集函数抛出异常）。
不修改任何仓库文件，数据写在临时目录。

用法：PYTHONDONTWRITEBYTECODE=1 python3 paper_failure_alert_check.py [仓库根目录]
"""

import contextlib
import io
import json
import sys
import tempfile
from concurrent.futures import Future
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))
from polymarket861 import opportunity, season, shadow, store  # noqa: E402

FIXTURE = "epl-aaa-bbb-2026-11-28"


def run(stage, role, job):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        now = store.instant(store.now_utc())
        kickoff = store.iso(now + timedelta(hours=1, seconds=10))
        agent = season.Coordinator(root, workers=2)
        try:
            with closing(season._db(root)) as db, db:
                key = season._add(db, FIXTURE, kickoff, now, now + timedelta(seconds=60), role, stage)
                db.execute("UPDATE season_slots SET state='running',owner=?,until=? WHERE key=?",
                           (agent.identity, store.iso(now + timedelta(seconds=90)), key))
                row = dict(db.execute("SELECT * FROM season_slots WHERE key=?", (key,)).fetchone())
            future = Future()
            try:
                future.set_result(job(root))
            except Exception as exc:  # 与线程池里的行为一致：异常留在 future 里
                future.set_exception(exc)
            with contextlib.redirect_stderr(io.StringIO()) as printed:
                agent._finish(future, stage, row)
            with closing(season._db(root)) as db:
                slot = db.execute("SELECT state,detail FROM season_slots WHERE key=?", (key,)).fetchone()
                alerts = db.execute("SELECT count(*) FROM season_alerts").fetchone()[0]
            return {"stage": stage, "role": role, "slot_state": slot["state"], "alerts_recorded": alerts,
                    "alert_printed": bool(printed.getvalue().strip()), "detail": json.loads(slot["detail"])}
        finally:
            agent.close()


def broken(message):
    def raiser(*args, **kwargs):
        raise RuntimeError(message)

    return raiser


paper_broken = broken("simulated failure inside the paper decision")
with patch.object(opportunity, "process", paper_broken), patch.object(shadow, "run", paper_broken):
    paper = run("paper", "decision", lambda root: season._paper(root, FIXTURE))
with patch.object(season.exchange, "market_state", broken("simulated failure inside the book capture")):
    book = run("book", "decision_book", lambda root: season._slot(root, {"fixture": FIXTURE, "stage": "book", "deadline": store.now_utc()}))
print(json.dumps({"paper_decision_failed": paper, "book_capture_failed": book}, ensure_ascii=False, indent=1))
