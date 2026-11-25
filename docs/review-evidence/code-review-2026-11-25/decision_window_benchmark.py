"""测量正式 T−60 冻结循环（opportunity.process）处理一场比赛全部方向代币的实际耗时，
以及主账户已有意图数增长后的变化。

复用仓库自带的合成端到端夹具 tests/test_opportunity.py（虚构球队、零费旧版大小球条款、打补丁的时钟），
只把一场比赛扩成多条进球线，使方向代币数接近真实挂牌（2026/27 目录平均每场 21.2 个已映射合约，约 42 枚代币）。
预置的旧意图全部是“未入场”记录，这是最便宜的一种（不占用预留资金，不触发收盘复核），所以结果是下限。
不修改任何仓库文件，数据写在临时目录。

用法：PYTHONDONTWRITEBYTECODE=1 python3 decision_window_benchmark.py [进球线条数=21] [预置意图数=0,1000,2000,4000,8000,16000]
"""

import json
import sys
import time
from datetime import timedelta
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import test_opportunity as T  # noqa: E402
from polymarket861 import ledger, opportunity, store  # noqa: E402
from polymarket861 import market_archive as archive  # noqa: E402
from polymarket861 import market_provider as provider  # noqa: E402

LINES = int(sys.argv[1]) if len(sys.argv) > 1 else 21
PRELOADS = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [0, 1000, 2000, 4000, 8000, 16000]
LEVELS = 10
single_market_event = T.event


def event(slug=T.SLUG):
    base = single_market_event(slug)
    if slug != T.SLUG:
        return base
    template = base["markets"][0]
    markets = []
    for i in range(LINES):
        market = dict(template)
        market.update(
            slug=f"{T.SLUG}-total-{i}pt5",
            conditionId="0x" + format(i + 1, "x").rjust(64, "3"),
            clobTokenIds=json.dumps([f"9{i:02d}1", f"9{i:02d}2"]),
            line=i + 0.5,
            groupItemTitle=f"O/U {i + 0.5}",
            description=template["description"]
            .replace("3 or more goals", f"{i + 1} or more goals")
            .replace("less than 3", f"less than {i + 1}"),
        )
        markets.append(market)
    base["markets"] = markets
    return base


T.event = event


def capture_first_books(case, when, seal):
    count = 0
    for contract in archive.contracts(case.root, T.SLUG, as_of=store.iso(when)):
        over = sum(
            Decimal(str(p))
            for h, row in enumerate(seal["matrix"])
            for a, p in enumerate(row)
            if h + a > float(contract["line"])
        )
        for token in contract["tokens"]:
            fair = over if token["outcome"].casefold() == "over" else 1 - over
            price = min(Decimal("0.90"), max(Decimal("0.12"), (fair - Decimal("0.04")).quantize(Decimal(".01"), rounding=ROUND_FLOOR)))
            native = {
                "asset_id": token["token_id"],
                "market": contract["condition_id"],
                "hash": store.digest([token["token_id"], store.iso(when)]),
                "tick_size": "0.01",
                "min_order_size": "1",
                "timestamp": int(when.timestamp() * 1000),
                "bids": [{"price": str(price - Decimal("0.01") * (k + 1)), "size": "40"} for k in range(LEVELS)],
                "asks": [{"price": str(price + Decimal("0.01") * k), "size": "40"} for k in range(LEVELS)],
            }

            def http(url, params=None, native=native):
                return T.reply(native, when, url + "?" + urlencode(params))

            with T.at(when), patch.object(provider, "get", side_effect=http):
                archive.snapshot_book(case.root, contract, token["token_id"])
            count += 1
    return count


def preload(root, count, template):
    with ledger.connection(root, True) as db:
        for i in range(count):
            result = dict(
                template["result"],
                intent_id=f"season-{i}",
                match_id=f"epl/2025-26/earlier/{i // 42}",
                condition_id=f"0xold{i}",
                token_id=f"old{i}",
                status="skipped",
                reason="insufficient edge, depth, budget or minimum size",
            )
            db.execute(
                "INSERT INTO paper_intents VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("main", f"season-{i}", result["match_id"], result["condition_id"], result["token_id"],
                 "2025-10-01T14:00:00+00:00", 0, "skipped", "0" * 64, template["original"], json.dumps(result)),
            )


def measure(existing, template=None):
    case = T.Opportunities()
    case.setUp()
    try:
        seal = case.seal()
        tokens = capture_first_books(case, T.CUT - timedelta(seconds=7), seal)
        with T.at(T.CUT - timedelta(seconds=6)):
            ledger.open_account(case.root, "main", "formal", role="main")
        if existing:
            preload(case.root, existing, template)
        with T.at(T.CUT - timedelta(seconds=5)):
            started = time.perf_counter()
            report = opportunity.process(case.root, T.SLUG)
            seconds = time.perf_counter() - started
        states = {}
        for decision in report["decisions"]:
            states[decision["state"]] = states.get(decision["state"], 0) + 1
        with ledger.connection(case.root) as db:
            row = db.execute("SELECT original,result FROM paper_intents WHERE account='main' AND id LIKE 'opp-%' LIMIT 1").fetchone()
            size = db.execute("SELECT length(payload) FROM reports WHERE kind='ledger' AND key='main'").fetchone()[0]
        print(json.dumps({
            "existing_intents": existing, "tokens": tokens, "states": states,
            "seconds": round(seconds, 2), "ms_per_token": round(1000 * seconds / tokens, 1),
            "ledger_report_bytes": size,
        }), flush=True)
        return {"result": json.loads(row["result"]), "original": row["original"]}
    finally:
        case.doCleanups()


template = measure(0)
for existing in PRELOADS:
    if existing:
        measure(existing, template)
