"""Native Gamma and CLOB shapes for testing the production collectors without HTTP."""

import hashlib
import json
from contextlib import closing
from datetime import timedelta
from email.utils import format_datetime
from unittest.mock import patch
from urllib.parse import urlencode

from polymarket861 import ledger_terms, market_archive, market_provider as provider
from polymarket861.ledger_rules import iso


def exchange_op(root, op, kickoff, home, away, *, quotes=None):
    """Capture the native event, both executable samples and the closing sample."""
    c = op["intent"]["contract"]
    fixture, condition, token = c["match_id"], c["condition_id"], c["token_id"]
    decision = kickoff - timedelta(hours=1)
    receipt = decision - timedelta(seconds=500)
    date = kickoff.strftime("%B %-d")
    prose = (
        f"In the upcoming Premier League game between {home} and {away}, scheduled for {date} at 11:30 AM ET:\n\n"
        f'This market will resolve to "Over" if {home} and {away} combine to score 3 or more goals in this game.\n\n'
        'If the combined total is less than 3, this market will resolve to "Under".\n\n'
        "If the game is postponed, this market will remain open until the game has been completed. "
        "If the game is canceled entirely, with no make-up game, this market will resolve 50–50.\n\n"
        "If the game is started but not completed, this market will resolve according to the official final score "
        "published on premierleague.com. This market refers only to the outcome within the first 90 minutes "
        "of regular play plus stoppage time."
    )
    match = provider.FIXTURE.fullmatch(fixture)
    native_market = {
        "slug": fixture + "-total-2pt5",
        "conditionId": condition,
        "version": "v1",
        "sportsMarketType": "totals",
        "outcomes": '["Over","Under"]',
        "clobTokenIds": json.dumps([token, str(int(token) + 100000)]),
        "question": f"{home} vs. {away}: O/U 2.5",
        "groupItemTitle": "O/U 2.5",
        "description": prose,
        "line": 2.5,
        "gameStartTime": iso(kickoff),
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "feesEnabled": False,
        "feeSchedule": None,
        "secondsDelay": 3,
        "orderMinSize": 5,
        "orderPriceMinTickSize": 0.01,
    }
    raw_event = {
        "id": str(int(token) + 32),
        "slug": fixture,
        "title": f"{home} vs. {away}",
        "live": False,
        "ended": False,
        "startTime": iso(kickoff),
        "teams": [
            {"name": home, "abbreviation": match[2], "ordering": "home"},
            {"name": away, "abbreviation": match[3], "ordering": "away"},
        ],
        "markets": [native_market],
    }

    def serve(body, time):
        def respond(url, params=None):
            link = url + ("?" + urlencode(params) if params else "")
            return provider.Reply(
                json.dumps(body).encode(),
                iso(time),
                iso(time),
                {"date": format_datetime(time, usegmt=True), "age": "0"},
                link,
            )

        return respond

    with patch.object(provider, "get", side_effect=serve(raw_event, receipt)):
        event, reply, sha = market_archive._fetch(
            root, "event", fixture, provider.GAMMA + "/events/slug/" + fixture
        )
    with closing(market_archive.database(root)) as db, db:
        market_archive._ingest(db, event, reply, sha)
    (market,) = market_archive.contracts(root, fixture)
    op["intent"]["contract"] = ledger_terms.contract_at(
        root, condition, token, decision
    )
    captures = quotes or (
        ("first_book", -1, ".39", ".41"),
        ("second_book", 5, ".40", ".42"),
        ("close_book", 3595, ".38", ".40"),
    )
    for key, seconds, bid, ask in captures:
        time = decision + timedelta(seconds=seconds)
        raw = {
            "asset_id": token,
            "market": condition,
            "timestamp": str(int(time.timestamp() * 1000)),
            "hash": hashlib.sha256((condition + key).encode()).hexdigest(),
            "bids": [{"price": bid, "size": "100"}],
            "asks": [{"price": ask, "size": "100"}],
            "tick_size": "0.01",
            "min_order_size": "5",
        }
        with patch.object(provider, "get", side_effect=serve(raw, time)):
            op[key] = market_archive.snapshot_book(root, market, token)
    return op


def forecast_example(root, op):
    """Record a fixed test model's score distribution with a genuinely observed input manifest."""
    from pathlib import Path

    from polymarket861 import datasets, football, input_manifest, ledger, store

    stamp = "2023-09-01T12:00:00.000000Z"
    with closing(input_manifest._db(root)) as db:
        known = db.execute(
            "SELECT digest FROM football_input_manifests LIMIT 1"
        ).fetchone()
    if known:
        manifest = known["digest"]
    else:
        source = Path(root) / "test-observations"
        season = "2023-24"
        f = source / season / "en.1.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(
            json.dumps(
                {
                    "matches": [
                        {
                            "round": "Matchday 1",
                            "date": "2023-08-20",
                            "time": "15:00",
                            "team1": "Training H",
                            "team2": "Training A",
                            "score": {"ft": [1, 0]},
                        }
                    ]
                }
            )
        )
        with (
            patch.object(datasets, "now_utc", return_value=stamp),
            patch.object(input_manifest, "now_utc", return_value=stamp),
            patch.object(store, "now_utc", return_value=stamp),
        ):
            manifest = datasets.observe(
                root,
                source,
                revision="a" * 40,
                expected={season: football.blob_sha(f.read_bytes())},
            )["manifest_digest"]
    i = op["intent"]
    i["model_version"] = "B1"
    p = ledger.number(i["normal_fv"])
    payload = {
        "input_manifest_sha256": manifest,
        "model_version": i["model_version"],
        "fixture_id": i["match_id"],
        "kickoff": i["kickoff"],
        "cutoff": i["decision_at"],
        "lineups": i["lineups"],
        "matrix": [[str(1 - p)], [0], [0], [str(p)]],
    }
    i["forecast_digest"] = store.digest(payload)
    with closing(store.connect(root)) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO trial_activations VALUES (?,?,?,?)",
            (store.PROTOCOL, stamp, stamp, "{}"),
        )
        db.execute(
            "INSERT INTO forecast_seals VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                i["match_id"],
                i["kickoff"],
                i["decision_at"],
                "T-60m",
                "B1",
                store.PROTOCOL,
                "formal",
                i["prediction_at"],
                i["forecast_digest"],
                store.canonical(payload),
            ),
        )
    return i


def formal_example(root, op, account="fixture-formal"):
    from polymarket861 import ledger, store

    i = forecast_example(root, op)
    with patch.object(store, "now_utc", return_value=i["decision_at"]):
        ledger.open_account(root, account, "formal")
        result = ledger.freeze(root, account, i, op["first_book"])
    if result["status"] != "frozen":
        raise AssertionError(result)
    with patch.object(store, "now_utc", return_value=op["second_book"]["received_at"]):
        executed = ledger.evaluate(root, account, i["intent_id"], op["second_book"])
    if executed["status"] != "filled":
        raise AssertionError(executed)
    op.update(
        paper_account=account,
        intent_id=i["intent_id"],
        decision_sealed_at=result["sealed_at"],
        decision_sha256=result["decision_sha256"],
        forecast_digest=i["forecast_digest"],
    )
    return op
