"""Refresh the local paper-account watchlist from publicly observable pregame evidence."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path

from account_engine import FRACTION, FUND
from polymarket861 import (
    big5_market_schedule,
    evidence,
    execution_books,
    league_live_inputs,
    league_player_status,
    league_registry,
    ledger_rules,
    ledger_terms,
    market_provider,
    news_events,
    pipeline,
    schedule_consensus,
    shadow_market,
    store,
)

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
ENGLISH = REPO / ".pm861"
DOMESTIC = REPO / ".local-deploy/five-automation/state"
MARKET_STATE = REPO / ".local-deploy/five-league-state"
HISTORY = HERE / "watch-history.jsonl"
STATUS = HERE / "watch-status.json"
LOCK = HERE / ".watch-picks.lock"
SIDES = ("home", "away")
ESPN_LEAGUES = {
    "epl": "eng.1",
    "sea": "ita.1",
    "fl1": "fra.1",
    "lal": "esp.1",
    "bun": "ger.1",
}
ERRORS = (
    OSError,
    ValueError,
    RuntimeError,
    AssertionError,
    TypeError,
    KeyError,
    IndexError,
    ArithmeticError,
    sqlite3.Error,
)


def require(test, reason):
    if not test:
        raise ValueError(reason)


def part(data, fields):
    return {k: data[k] for k in fields if k in data}


def kelly_budget(capital, probability, cost):
    if probability is None or not D(0) < cost < D(1):
        return D(0)
    return capital * FRACTION * max(D(0), (probability - cost) / (1 - cost))


def reported_news(snapshot, pair):
    applied = {f.get("id") for f in pair["applied"] if isinstance(f, dict)}
    fields = (
        "id",
        "team",
        "player_name",
        "event",
        "availability",
        "status",
        "authority",
        "conflict",
        "received_at",
        "source",
        "original_sha256",
        "url",
    )
    return {
        "facts_sha256": pair["facts_sha256"],
        "facts": [
            part(f, fields)
            | {"used_in_model": f.get("id") is not None and f.get("id") in applied}
            for f in snapshot["facts"]
        ],
        "state": snapshot.get("state"),
        "coverage": part(
            snapshot.get("coverage", {}),
            ("both_teams_observed", "both_teams_study_qualified"),
        ),
    }


@contextmanager
def dbread(path):
    with closing(
        sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=8)
    ) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db


def newest_cycle(cycle, due_field, at):
    if not cycle:
        return "collector_has_no_review_record"
    if cycle.get("last_error") or cycle.get("error"):
        return "collector_reported_error"
    if not cycle.get(due_field) or store.instant(cycle[due_field]) < store.instant(at):
        return "collector_review_overdue"
    return None


def epl_model(pick, at):
    fid, kick = pick["forecast_fixture_id"], store.iso(pick["kickoff"])
    with dbread(ENGLISH / "runtime.sqlite3") as db:
        s = db.execute(
            "SELECT f.*,s.recorded_at,s.precision FROM fixture_versions f JOIN schedule_receipts s "
            "USING(fixture_id,version) WHERE f.fixture_id=? AND f.known_at<=? AND s.recorded_at<=? "
            "ORDER BY f.known_at DESC LIMIT 1",
            (fid, at, at),
        ).fetchone()
        require(s and s["kickoff"] == kick, "registered_kickoff_changed_or_missing")
        proof = schedule_consensus.attest(ENGLISH, db, s, at)
        require(
            proof["gamma"]["slug"] == pick["fixture"], "local_market_binding_changed"
        )
        fixture = dict(s)
        fixture["league"] = pick["league"]
        entry = db.execute(
            "SELECT * FROM forecast_seals WHERE fixture_id=? AND kickoff=? AND sealed_at<=? "
            "AND mode IN ('formal','technical') ORDER BY sealed_at DESC LIMIT 1",
            (fid, kick, at),
        ).fetchone()
        require(entry, "no_current_match_seal")
        base = pipeline._verified_seal(entry)
        require(
            all(base[k] == fixture[k] for k in SIDES), "seal_team_identity_disagrees"
        )
        update = db.execute(
            "SELECT * FROM news_event_updates WHERE fixture_id=? AND kickoff=? AND "
            "json_extract(document,'$.created_at')<=? ORDER BY revision DESC LIMIT 1",
            (fid, kick, at),
        ).fetchone()
        doc = news_events._verified(update) if update else None
        cycle = db.execute(
            "SELECT review_at,retry_at,last_error FROM news_event_work WHERE fixture_id=? AND kickoff=?",
            (fid, kick),
        ).fetchone()
        cycle = dict(cycle) if cycle else {}
        info = {
            "kind": "match_seal",
            "digest": base["digest"],
            "cutoff": base["input_cutoff"],
            "published": base["sealed_at"],
            "horizon": base["horizon"],
            "model": base["model"],
            "training": base["evidence"]["training_matches"],
            "news_applied": None,
            "main_digest": base["digest"],
            "news_cycle": cycle,
            "event_digest": doc["digest"] if doc else None,
        }
        primary = base
        alternative = base
        if doc and doc["created_at"] > base["sealed_at"]:
            pair = doc["news_pair"]
            require(
                doc["schedule_version"] == s["version"]
                and doc["cutoff"] <= doc["created_at"] < kick
                and all(doc[k] == s[k] for k in SIDES),
                "news_edition_schedule_changed",
            )
            require(
                store.digest(doc["snapshot"]["facts"]) == pair["facts_sha256"]
                and doc["training_sha256"] == pair["base_training_sha256"]
                and store.digest(doc["state"]) == doc["state_sha256"],
                "news_edition_evidence_does_not_match",
            )
            primary, alternative = pair["with_news"], pair["no_news"]
            info.update(
                kind="event_news_research",
                digest=doc["digest"],
                published=doc["created_at"],
                cutoff=doc["cutoff"],
                horizon="news_update",
                model=pair["base_model"],
                training=doc["training_evidence"]["training_matches"],
                news_applied=len(pair["applied"]),
                news_status=pair["status"],
                news_changed=pair["changed"],
                reported_news=reported_news(doc["snapshot"], pair),
            )
        lineups = {}
        for side in SIDES:
            facts, _, conflicts = evidence._collect(
                db,
                fid,
                evidence.utc(at),
                "prediction",
                teams=(evidence.name_key(s[side]),),
                field="lineup",
                prospective=True,
            )
            selected, ambiguity = evidence._selected_lineup(facts)
            confirmed = bool(
                selected
                and selected["value"]["confirmed"]
                and not ambiguity
                and not conflicts
            )
            lineups[side] = {
                "confirmed": confirmed,
                "source": selected.get("source_id") if confirmed else None,
                "received_at": selected["received_at"] if confirmed else None,
                "players": selected["value"]["starters"] if confirmed else [],
                "state": "conflict"
                if ambiguity or conflicts
                else "confirmed"
                if confirmed
                else "unobserved",
            }
        return (
            fixture,
            info,
            primary,
            alternative,
            lineups,
            newest_cycle(cycle, "review_at", at),
        )


def domestic_model(pick, at):
    fid, kick = pick["forecast_fixture_id"], store.iso(pick["kickoff"])
    folder = DOMESTIC / "forecast-scheduler" / pick["league"]
    with dbread(folder / "runtime.sqlite3") as db:
        row = db.execute(
            "SELECT * FROM domestic_matches WHERE fixture=? AND kickoff=? AND active=1",
            (fid, kick),
        ).fetchone()
        require(row, "registered_kickoff_changed_or_inactive")
        fixture, binding = json.loads(row["metadata"]), json.loads(row["binding"])
        require(
            fixture["fixture_id"] == fid
            and fixture["league"] == pick["league"]
            and fixture["kickoff"] == kick
            and binding["slug"] == pick["fixture"]
            and league_live_inputs.still_pregame(MARKET_STATE, binding, at),
            "local_active_schedule_or_market_not_pregame",
        )
        e = db.execute(
            "SELECT * FROM domestic_jobs WHERE fixture=? AND kickoff=? AND state='sealed' AND sealed<=? "
            "ORDER BY sealed DESC,digest LIMIT 1",
            (fid, kick, at),
        ).fetchone()
        require(e, "no_current_domestic_seal")
        d = json.loads(e["payload"])
        require(
            store.digest(d) == e["digest"]
            and d["fixture"] == fixture
            and d["horizon"] == e["horizon"]
            and d["input_cutoff"] <= e["sealed"] < d["deadline"] <= kick,
            "domestic_seal_integrity_or_clocks",
        )
        require(
            all(
                d["schedule_evidence"][k] == binding[k]
                for k in ("slug", "home_id", "away_id", "event_id", "kickoff")
            ),
            "forecast_market_binding_changed",
        )
        n, b, pair = d["news"], d["baseline"], d["pair"]
        require(
            store.digest(n["facts"]) == pair["facts_sha256"]
            and b["evidence"]["input_sha256"] == pair["base_training_sha256"],
            "news_evidence_does_not_match_seal",
        )
        cycle = db.execute(
            "SELECT due,state,attempted,error FROM domestic_news_polls WHERE fixture=? AND kickoff=?",
            (fid, kick),
        ).fetchone()
        cycle = dict(cycle) if cycle else {}
    player = league_player_status.as_of(DOMESTIC, fixture, at)
    lineups = {}
    for side in SIDES:
        team = player["lineup"]["teams"][side]
        yes = team["state"] == "published" and len(team["players"]) == 11
        lineups[side] = {
            "confirmed": yes,
            "source": player["source"],
            "state": team["state"] if yes else player["lineup"]["state"],
            "received_at": player["lineup"].get("received_at"),
            "players": team["players"] if yes else [],
        }
    info = {
        "kind": "domestic_seal",
        "digest": e["digest"],
        "published": e["sealed"],
        "cutoff": d["input_cutoff"],
        "horizon": d["horizon"],
        "model": b["model"],
        "training": b["evidence"]["training_matches"],
        "news_changed": pair["changed"],
        "news_status": pair["status"],
        "news_applied": len(pair["applied"]),
        "reported_news": reported_news(n, pair),
        "press_index": {
            "state": n["press"].get("state"),
            "latest_edition": n["press"].get("latest_index_edition"),
            "scope": n["press"].get("scope"),
            "teams": {
                s: {"state": v.get("state"), "links_observed": len(v.get("items", []))}
                for s, v in n["press"].get("teams", {}).items()
            },
        },
        "scenario_status": d["player_scenario"]["status"],
        "scenario_changed": d["player_scenario"]["changed"],
        "injuries": {side: player["injuries"]["teams"][side] for side in SIDES},
        "injury_state": player["injuries"]["state"],
        "news_cycle": cycle,
    }
    return (
        fixture,
        info,
        pair["with_news"],
        pair["no_news"],
        lineups,
        newest_cycle(cycle, "due", at),
    )


def read_event(slug):
    # Gamma's CDN can cache schedule/fee flags for several minutes even on an anonymous GET.
    reply = market_provider.get(
        market_provider.GAMMA + "/events/slug/" + slug,
        {"t": str(time.time_ns())},
        timeout=12,
    )
    require(market_provider.fresh_observation(reply), "stale_Gamma_HTTP_receipt")
    raw = json.loads(reply.body)
    require(raw.get("slug") == slug, "Gamma_returned_another_event")
    return raw, reply.received_at, hashlib.sha256(reply.body).hexdigest()


class Closed(Exception):
    pass


def comparison_book(pick, terms):
    """Read the same provider and full-time market used to size the initial paper position."""
    league = ESPN_LEAGUES[pick["league"]]
    event = str(pick["espn_id"])
    require(event.isdigit(), "invalid_ESPN_fixture_identifier")
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/" + league + "/summary"
    reply = market_provider.get(
        url, {"event": event, "observation": time.time_ns()}, timeout=12
    )
    require(market_provider.fresh_observation(reply), "stale_ESPN_HTTP_receipt")
    data = json.loads(reply.body)
    header = data["header"]
    require(
        str(header["id"]) == event and len(header["competitions"]) == 1,
        "ESPN_event_binding_changed",
    )
    match = header["competitions"][0]
    teams = {t["homeAway"]: t["team"] for t in match["competitors"]}
    require(
        store.iso(match["date"]) == store.iso(pick["kickoff"])
        and [teams[s]["displayName"] for s in SIDES] == pick["espn_teams"],
        "ESPN_schedule_or_participants_changed",
    )
    status = match["status"]["type"]
    if status.get("state") in ("in", "post") or status.get("completed"):
        raise Closed("ESPN_reports_match_started_or_ended")
    require(
        status.get("name") == "STATUS_SCHEDULED", "ESPN_game_not_confirmed_scheduled"
    )
    receipt = {
        "source": "ESPN",
        "match_url": "https://www.espn.com/soccer/match/_/gameId/" + event,
        "received_at": reply.received_at,
        "source_sha256": hashlib.sha256(reply.body).hexdigest(),
    }
    public_xi = {}
    for team in data.get("rosters", []):
        side = team.get("homeAway")
        if side not in SIDES or str(team.get("team", {}).get("id")) != str(
            teams[side]["id"]
        ):
            continue
        players = [
            athlete["athlete"]
            for athlete in team.get("roster", [])
            if athlete.get("starter") is True and athlete.get("athlete")
        ]
        if len(players) == len({p.get("id") for p in players}) == 11 and all(
            p.get("id") and p.get("displayName") for p in players
        ):
            public_xi[side] = {
                "formation": team.get("formation"),
                "players": [p["displayName"] for p in players],
            }
    lineups = {**receipt, "teams": public_xi}
    try:
        odds = [
            o
            for o in data.get("odds", [])
            if o.get("provider", {}).get("name") == "DraftKings"
        ]
        require(len(odds) == 1, "DraftKings_odds_missing_or_ambiguous")
        if terms["family"] == "total":
            line = format(D(terms["line"]).normalize(), "f")
            offers = {d: odds[0]["total"][d]["close"] for d in ("under", "over")}
            require(
                all(o["line"] == d[0] + line for d, o in offers.items()),
                "DraftKings_total_line_changed",
            )
            prices = {d: o["odds"] for d, o in offers.items()}
            selection = terms["side"]
        else:
            require(terms["family"] == "result", "DraftKings_unsupported_market")
            prices = {
                s: odds[0]["moneyline"][s]["close"]["odds"]
                for s in ("home", "draw", "away")
            }
            selection = terms["selection"]
        require(
            all(
                re.fullmatch(r"[+-]?\d+", str(x)) and abs(D(x)) >= 100
                for x in prices.values()
            ),
            "invalid_DraftKings_American_odds",
        )
        vals = {k: D(v) for k, v in prices.items()}
        implied = {
            k: 100 / (100 + v) if v > 0 else -v / (100 - v) for k, v in vals.items()
        }
        probability = implied[selection] / sum(implied.values())
        if terms["family"] == "result" and terms["side"] == "no":
            probability = 1 - probability
        fair = ledger_rules.fair_values(probability, terms)[0]
        return (
            lineups,
            {
                **receipt,
                "provider": "DraftKings via ESPN",
                "prices": prices,
                "probability": probability,
                "fair": fair,
            },
            None,
        )
    except ERRORS as exc:
        return (
            lineups,
            {**receipt, "provider": "DraftKings via ESPN"},
            "external_odds_unavailable: " + str(exc),
        )


def clob_settings(market, fee):
    reply = market_provider.get(
        market_provider.CLOB + "/clob-markets/" + market["condition_id"],
        {"observation": time.time_ns()},
        timeout=12,
    )
    require(market_provider.fresh_observation(reply), "stale_CLOB_market_HTTP_receipt")
    data = json.loads(reply.body)
    require(
        data["c"] == market["condition_id"]
        and {(t["t"], t["o"]) for t in data["t"]}
        == {(t["token_id"], t["outcome"]) for t in market["tokens"]},
        "CLOB_contract_or_outcome_changed",
    )
    if data.get("ao") is False:
        raise Closed("CLOB_not_accepting_orders")
    require(data.get("ao") is True, "CLOB_order_status_unknown")
    require(
        data.get("sd") == market["seconds_delay"],
        "CLOB_and_Gamma_execution_delays_differ",
    )
    params = data.get("fd") or {}
    if fee["enabled"]:
        require(
            D(str(params["r"])) == D(fee["rate"])
            and params["e"] == fee["exponent"]
            and params["to"] is True,
            "CLOB_and_Gamma_taker_fees_differ",
        )
    else:
        require(
            D(str(params.get("r", 0))) == 0,
            "CLOB_charges_fees_on_fee_disabled_contract",
        )
    return {
        "received_at": reply.received_at,
        "source_sha256": hashlib.sha256(reply.body).hexdigest(),
        "taker_fee": params,
        "minimum_order_size": data.get("mos"),
        "tick": data.get("mts"),
    }


def market_for(pick):
    main, seen, sha = read_event(pick["fixture"])
    ids = pick["fixture_id"].split("/")
    require(
        len(ids) == 4
        and ids[0] == pick["league"]
        and all(t.isdigit() for t in ids[-2:]),
        "invalid_frozen_fixture_id",
    )
    native = market_provider.teams(main)
    require(
        [str(native[s]["id"]) for s in SIDES] == ids[-2:], "Gamma_participants_changed"
    )
    require(
        store.iso(main["startTime"]) == store.iso(pick["kickoff"]),
        "Gamma_kickoff_changed",
    )

    def check_event(event, stamp):
        if big5_market_schedule.stopped(event, stamp) or event.get("closed") is True:
            raise Closed("native_venue_closed_or_game_started")
        require(
            big5_market_schedule.pregame_flags(event, stamp)
            in ("explicit_pregame", "schedule_only"),
            "Gamma_pregame_state_unclassified",
        )

    check_event(main, seen)
    for idx in range(2):
        rows = [
            m
            for m in market_provider.normalized_markets(
                main, sport=pick["league"], project_teams=True
            )
            if m["slug"] == pick["market_slug"]
        ]
        if rows:
            break
        if idx == 0:
            main, seen, sha = read_event(pick["fixture"] + "-more-markets")
            check_event(main, seen)
            require(
                [str(market_provider.teams(main)[s]["id"]) for s in SIDES] == ids[-2:],
                "child_event_team_identity_changed",
            )
    require(len(rows) == 1, "market_not_returned_by_Gamma")
    m = rows[0]
    require(
        m["condition_id"] == pick["condition_id"]
        and m["fixture"] == pick["fixture"]
        and not m["mapping_error"]
        and m["game_times"] == [store.iso(pick["kickoff"])],
        "condition_or_regulation_mapping_changed",
    )
    if m["market_open"] is False:
        raise Closed("native_contract_closed_or_not_accepting_orders")
    require(m["market_open"] is True, "market_open_unknown")
    token = str(pick["token_id"])
    require(
        {t["token_id"] for t in m["tokens"]} == {token, str(pick["opposite_token_id"])}
        and any(
            t["token_id"] == token
            and t["outcome"].casefold() == pick["outcome"].casefold()
            for t in m["tokens"]
        ),
        "frozen_outcome_or_tokens_changed",
    )
    m.update(recorded_at=seen, source_sha256=sha)
    fee = shadow_market._fee(None, m, seen)
    terms = ledger_terms._terms_with_fee(None, m, token, seen, None, fee)
    frozen = pick.get("frozen_contract")
    if frozen is not None:
        require(
            all(
                m[k] == frozen[k]
                for k in (
                    "rule_sha256",
                    "protocol",
                    "seconds_delay",
                    "fees_enabled",
                    "fee_schedule",
                )
            ),
            "frozen_contract_terms_changed",
        )
    m["clob_settings"] = clob_settings(m, fee)
    return m, fee, terms


def normal_p(arm, terms):
    if terms["family"] == "result":
        team = terms["selection"]
        key = "draw" if team == "draw" else team + "_win"
        level = ledger_rules.unit(arm["probabilities"][key])
        return level if terms["side"] == "yes" else 1 - level
    scope = "total" if terms["family"] == "total" else terms["team"]
    line = format(D(terms["line"]).normalize(), "f")
    return ledger_rules.unit(arm["totals"][scope][line][terms["side"]])


def sample_books(markets):
    tokens = {t["token_id"]: m for m in markets for t in m["tokens"]}
    if not tokens:
        return {}, {}

    def request(group):
        reply = market_provider.get(
            market_provider.CLOB + "/books", {"token_ids": group}, timeout=12
        )
        require(market_provider.fresh_observation(reply), "stale_CLOB_HTTP_receipt")
        by_token = execution_books.native_rows(json.loads(reply.body), set(group))
        sha = hashlib.sha256(reply.body).hexdigest()
        records = {}
        for token in group:
            if token not in by_token:
                continue
            record = market_provider.normalize_book(
                by_token[token], reply, tokens[token], token, sha
            )
            require(
                record.get("exchange_at")
                and store.instant(record["exchange_at"])
                <= store.instant(reply.received_at),
                "book_exchange_clock_invalid",
            )
            records[token] = record
        return records

    try:
        books = request(list(tokens))
        errors = {
            token: "token_omitted_by_CLOB" for token in tokens if token not in books
        }
        return books, errors
    except ERRORS:
        # A closed or retired token can cause a batch rejection. Preserve independent contracts.
        books, errors = {}, {}
        for market in markets:
            pair = [t["token_id"] for t in market["tokens"]]
            try:
                got = request(pair)
                books.update(got)
                errors.update(
                    {t: "token_omitted_by_CLOB" for t in pair if t not in got}
                )
            except ERRORS as err:
                errors.update({t: str(err) for t in pair})
        return books, errors


def inspect_book(book):
    bids = ledger_rules.series(book, "bids")
    asks = ledger_rules.series(book, "asks")
    step = ledger_rules.number(book["tick_size"])
    minimum = ledger_rules.number(book["min_order_size"])
    require(
        step > 0 and minimum > 0 and all(p % step == 0 for p, _ in bids + asks),
        "book_tick_or_minimum_invalid",
    )
    require(0 < bids[0][0] < asks[0][0] < 1, "crossed_or_empty_book")
    return {
        "bid": bids[0][0],
        "ask": asks[0][0],
        "bid_size": bids[0][1],
        "ask_size": asks[0][1],
        "mid": (bids[0][0] + asks[0][0]) / 2,
        "spread": asks[0][0] - bids[0][0],
        "min_size": minimum,
        "tick": step,
        **part(
            book,
            (
                "received_at",
                "requested_at",
                "exchange_at",
                "source_sha256",
                "book_hash",
            ),
        ),
    }, asks


def monitor(picks):
    items = []
    for p in picks:
        k = store.iso(p["kickoff"])
        row = {
            "id": p["id"],
            "fixture": p["fixture"],
            "display": p["display"],
            "kickoff": k,
            "token_id": str(p["token_id"]),
            "forecast_fixture_id": p["forecast_fixture_id"],
            "frozen_limit": str(p["limit_price"]),
            "frozen_stake_budget": str(p["max_stake"]),
            "sizing_bankroll": str(p.get("sizing_bankroll", FUND)),
            "frozen_fair": str(p["fair"]),
            "frozen_blended_probability": p.get("blended_probability"),
            "initial_model_sha256": p["model_sha256"],
            "state": "insufficient_evidence",
            "reasons": [],
        }
        if store.now_utc() >= k:
            row.update(state="closed", reasons=["scheduled_kickoff_passed"])
        items.append((p, row))
    markets = {}

    def setup(pair):
        p, row = pair
        if row["state"] == "closed":
            return
        mk = p["id"]
        try:
            m, fee, terms = market_for(p)
            at = store.now_utc()
            markets[mk] = (m, fee, terms)
            row["market"] = {
                **part(
                    m,
                    (
                        "slug",
                        "event_slug",
                        "condition_id",
                        "seconds_delay",
                        "recorded_at",
                        "source_sha256",
                        "rule_sha256",
                        "game_times",
                        "clob_settings",
                    ),
                ),
                "fee": fee,
            }
            try:
                public_lineups, external, gap = comparison_book(p, terms)
                row["public_lineups"], row["bookmaker"] = public_lineups, external
                if gap:
                    row["reasons"].append(gap)
            except ERRORS as exc:
                row["reasons"].append("ESPN_comparison_unavailable: " + str(exc))
        except Closed as exc:
            markets.pop(mk, None)
            row.update(state="closed", reasons=[str(exc)])
            return
        except ERRORS as exc:
            row["reasons"].append(str(exc))
            at = store.now_utc()
        try:
            fixture, info, model, alt, lineups, problem = (
                epl_model if p["league"] == "epl" else domestic_model
            )(p, at)
            require(
                fixture["fixture_id"] == p["forecast_fixture_id"]
                and fixture["season"] == p["fixture_id"].split("/")[1],
                "model_fixture_season_differs",
            )
            row["model"], row["lineups"] = info, lineups
            if problem:
                row["reasons"].append(problem)
            minutes = (
                store.instant(row["kickoff"]) - store.instant(at)
            ).total_seconds() / 60
            deadline = league_registry.profile(p["league"]).lineup_deadline_minutes_2026
            row["lineup_review"] = {
                "publication_minutes_before_kickoff": deadline,
                "inside_publication_window": minutes <= deadline,
                "both_confirmed": all(lineups[s]["confirmed"] for s in SIDES),
                "published_on_ESPN": sorted(
                    row.get("public_lineups", {}).get("teams", {})
                ),
            }
            if minutes <= deadline and not row["lineup_review"]["both_confirmed"]:
                row["reasons"].append("official_starting_elevens_unconfirmed")
            if (
                minutes <= 60
                and info["horizon"] != "T-60m"
                and store.instant(info["cutoff"])
                < store.instant(row["kickoff"]) - timedelta(minutes=60)
            ):
                row["reasons"].append("forecast_predates_final_hour")
            newest_lineup = max(
                (
                    v["received_at"]
                    for v in lineups.values()
                    if v["confirmed"] and v.get("received_at")
                ),
                default=None,
            )
            if newest_lineup and newest_lineup > info["cutoff"] and minutes <= deadline:
                row["reasons"].append("confirmed_lineup_newer_than_model")
            if mk in markets:
                price = normal_p(model, terms)
                other = normal_p(alt, terms)
                fair = ledger_rules.fair_values(price, terms)[0]
                robust = min(fair, ledger_rules.fair_values(other, terms)[0])
                row.update(
                    model_probability=price,
                    no_news_probability=other,
                    fair=fair,
                    conservative_fair=robust,
                    model_probability_change=price
                    - ledger_rules.unit(p["model_probability"]),
                    conservative_fair_change=robust - ledger_rules.unit(p["fair"]),
                    model_revised=info["digest"] != p["model_sha256"],
                )
                if "fair" in row.get("bookmaker", {}):
                    row["blended_probability"] = (robust + row["bookmaker"]["fair"]) / 2
                    row["bookmaker"]["probability_change"] = row["bookmaker"][
                        "probability"
                    ] - ledger_rules.unit(p["bookmaker"]["probability"])
        except ERRORS as exc:
            row["reasons"].append(str(exc))

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(setup, items))
    ms = [v[0] for v in markets.values()]
    first, err1 = sample_books(ms)
    if ms:
        time.sleep(max(5, max(m["seconds_delay"] for m in ms)))
    second, err2 = sample_books(ms)
    for p, row in items:
        if p["id"] not in markets:
            continue
        m, fee, terms = markets[p["id"]]
        token = str(p["token_id"])
        try:
            require(
                token in first and token in second,
                err1.get(token) or err2.get(token) or "pair_of_quotes_unavailable",
            )
            views = [inspect_book(book[token]) for book in (first, second)]
            quotes = [v[0] for v in views]
            row["selected_quotes"] = quotes
            opposite = str(p["opposite_token_id"])
            if opposite in first and opposite in second:
                row["opposite_quotes"] = [
                    inspect_book(book[opposite])[0] for book in (first, second)
                ]
            else:
                row["reasons"].append(
                    err1.get(opposite)
                    or err2.get(opposite)
                    or "opposite_outcome_quotes_unavailable"
                )
            require(
                all(store.iso(q["received_at"]) < row["kickoff"] for q in quotes),
                "quote_at_or_after_kickoff",
            )
            require(
                (
                    store.instant(quotes[1]["requested_at"])
                    - store.instant(quotes[0]["received_at"])
                ).total_seconds()
                >= max(5, m["seconds_delay"]),
                "quote_confirmation_delay_not_met",
            )
            cap = ledger_rules.unit(p["limit_price"])
            common = ledger_rules.pair_depth(views[0][1], views[1][1], cap)
            available = sum((lev[1] for lev in common), D(0))
            minimum = max(q["min_size"] for q in quotes)
            row.update(
                worst_best_ask=max(q["ask"] for q in quotes),
                common_depth_shares_at_limit=available,
            )
            full_depth = ledger_rules.pair_depth(views[0][1], views[1][1], D(1))
            min_fill = ledger_rules.price_fill(full_depth, minimum, fee)
            if min_fill["gross"] == minimum and min_fill["net"] > 0:
                row["minimum_order_at_market"] = {
                    **part(
                        min_fill, ("gross", "spent", "net", "fees", "cost", "levels")
                    ),
                    "edge_vs_model_fair": row["conservative_fair"] - min_fill["cost"]
                    if "conservative_fair" in row
                    else None,
                    "edge_vs_blended": row["blended_probability"] - min_fill["cost"]
                    if "blended_probability" in row
                    else None,
                }
            settings = m["clob_settings"]
            if any(
                q["min_size"] != ledger_rules.number(settings["minimum_order_size"])
                or q["tick"] != ledger_rules.number(settings["tick"])
                for q in quotes
            ):
                row["reasons"].append("CLOB_settings_changed_between_quotes")
            if row["worst_best_ask"] > cap:
                row["reasons"].append("ask_above_frozen_limit")
            if available < minimum:
                row["reasons"].append("insufficient_depth_at_frozen_limit")
            if any(q["spread"] > ledger_rules.MAX_SPREAD for q in quotes):
                row["reasons"].append("spread_exceeds_policy")
            if "conservative_fair" in row:
                fair = row["conservative_fair"]
                blend = row.get("blended_probability")
                capital = ledger_rules.number(row["sizing_bankroll"])
                if any(
                    abs(q["mid"] - fair) > ledger_rules.MAX_GAP
                    or abs(q["mid"] - row["fair"]) > ledger_rules.MAX_GAP
                    for q in quotes
                ):
                    row["reasons"].append("midpoint_exceeds_fair_gap_policy")
                if available >= minimum:
                    budget = ledger_rules.number(p["max_stake"])
                    fill = ledger_rules.fit(
                        common,
                        available,
                        fee,
                        budget,
                        fair,
                        risk_ok=lambda sample: (
                            sample["spent"]
                            <= kelly_budget(capital, blend, sample["cost"])
                        ),
                    )
                    row["budget_depth"] = {
                        **part(
                            fill, ("gross", "spent", "net", "fees", "cost", "levels")
                        ),
                        "remaining_edge": fair - fill["cost"] if fill["net"] else None,
                        "edge_vs_blended": blend - fill["cost"]
                        if fill["net"] and blend is not None
                        else None,
                        "current_kelly_budget_at_unit_cost": kelly_budget(
                            capital, blend, fill["cost"]
                        )
                        if fill["net"]
                        else None,
                    }
                    if fill["gross"] < minimum:
                        row["reasons"].append(
                            "fee_adjusted_edge_or_budget_below_minimum"
                        )
            if store.now_utc() >= row["kickoff"]:
                row.update(state="closed", reasons=["scheduled_kickoff_passed"])
            elif "ask_above_frozen_limit" in row["reasons"]:
                row["state"] = "price_above_limit"
            elif not row["reasons"]:
                row["state"] = "retain"
        except ERRORS as exc:
            row["reasons"].append(str(exc))
    stopped_at = store.now_utc()
    for _, row in items:
        if stopped_at >= row["kickoff"]:
            row.update(state="closed", reasons=["scheduled_kickoff_passed"])
    return [row for _, row in items]


def write_report(result):
    value = json.dumps(
        result, ensure_ascii=False, separators=(",", ":"), default=str, allow_nan=False
    )
    with HISTORY.open("a+b") as f:
        # Repair only an interrupted final append, then durably append this observation before publishing the current status.
        end = f.seek(0, os.SEEK_END)
        if end:
            f.seek(end - 1)
            if f.read(1) != b"\n":
                pos = end
                while pos:
                    step = min(pos, 8192)
                    pos -= step
                    f.seek(pos)
                    tail = f.read(step)
                    last = tail.rfind(b"\n")
                    if last >= 0:
                        pos += last + 1
                        break
                f.truncate(pos)
        f.seek(0, os.SEEK_END)
        f.write(value.encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    with tempfile.NamedTemporaryFile(
        dir=HERE, prefix=".watch-status-", delete=False
    ) as f:
        temp = Path(f.name)
        f.write(value.encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())
    try:
        os.replace(temp, STATUS)
    finally:
        temp.unlink(missing_ok=True)


def main():
    with LOCK.open("a") as mutex:
        try:
            fcntl.flock(mutex, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        picks = json.loads((HERE / "picks.json").read_bytes())
        if isinstance(picks, dict):
            picks = picks["picks"]
        require(
            isinstance(picks, list)
            and picks
            and len({p["id"] for p in picks}) == len(picks),
            "invalid or empty picks.json",
        )
        original = {s["id"]: s for s in json.loads((HERE / "slate.json").read_bytes())}
        account = json.loads((HERE / "account-input.json").read_bytes())
        intents = {i["id"]: i for i in account["intents"]}
        for p in picks:
            s = original[p["id"]]
            require(
                all(
                    s[k] == p[k]
                    for k in (
                        "fixture",
                        "condition_id",
                        "token_id",
                        "opposite_token_id",
                        "kickoff",
                        "limit_price",
                    )
                ),
                "edited_frozen_contract_identity_or_limit",
            )
            p["frozen_contract"] = s["market"]
            intent = intents.get(p["id"])
            if intent:
                require(
                    intent["token_id"] == p["token_id"]
                    and intent["condition_id"] == p["condition_id"],
                    "paper_intent_differs_from_frozen_contract",
                )
                p["sizing_bankroll"] = ledger_rules.number(
                    intent["sizing"]["sizing_bankroll"]
                )
                p["max_stake"] = ledger_rules.number(intent["sizing"]["budget"])
                p["blended_probability"] = str(
                    ledger_rules.unit(intent["sizing"]["probability"])
                )
        started = store.now_utc()
        rows = monitor(picks)
        states = dict(Counter(r["state"] for r in rows))
        result = {
            "scope": "read_only_paper_candidate_monitor",
            "started_at": started,
            "checked_at": store.now_utc(),
            "picks": rows,
            "counts": states,
            "midpoint_rule": {
                "maximum_spread": str(ledger_rules.MAX_SPREAD),
                "maximum_gap_to_fair": str(ledger_rules.MAX_GAP),
                "minimum_fee_adjusted_edge": str(ledger_rules.MIN_EDGE),
                "allocation_probability": "Mean of the conservative model fair and the current de-vigged same-market bookmaker fair after the contract adjustment.",
            },
            "budget_basis": "Each intent's frozen fractional-Kelly allocation from shared capital.",
        }
        write_report(result)
        print(
            json.dumps(
                {"checked_at": result["checked_at"], "counts": states},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
