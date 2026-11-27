"""Publish rolling seven-day recommendations from current, independently matched public evidence."""

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from decimal import Decimal as D, ROUND_FLOOR
from pathlib import Path
from urllib.error import HTTPError

from polymarket861 import (
    big5_market,
    big5_market_schedule,
    league_registry,
    ledger_rules as rules,
    ledger_terms,
    market_provider as provider,
    opportunity,
    shadow_market,
    store,
)
from espn_clubs import ALIASES
from paper_calibration import calibrated, fit_evidence

HERE = Path(__file__).resolve().parent
PAPER = HERE.parent.parent / "paper-10000"
sys.path.insert(0, str(PAPER))
spec = importlib.util.spec_from_file_location(
    "pm861_paper_watch", PAPER / "watch-picks.py"
)
watch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watch)
from account_engine import FRACTION, POLICY, size_at_book

LEAGUES = watch.football_odds_access.LEAGUES
SIDES = ("home", "away")
OUTCOMES = ("home", "draw", "away")
TTL = timedelta(minutes=15)
TARGET_EDGE = D(".03")
FEED_POLICY = "verified-football-recommendations-v2"
PIN_POLICIES = {FEED_POLICY, "verified-football-recommendations-v1"}
COMPARISON_SHARES = D(20)
ERRORS = watch.ERRORS + (OverflowError, watch.Closed)


def require(value, reason):
    if not value:
        raise ValueError(reason)


def read(path):
    return json.loads(path.read_bytes())


def stamp(value):
    return store.iso(value)


def sha(body):
    return hashlib.sha256(body).hexdigest()


def group_work(fn, args, workers):
    with ThreadPoolExecutor(max_workers=workers) as pool:
        jobs = {pool.submit(fn, *arg): arg for arg in args}
        for future in as_completed(jobs):
            try:
                yield jobs[future], future.result(), None
            except ERRORS as exc:
                yield jobs[future], None, str(exc)


def gaps_row(league, fixture, stage, reason):
    return {"league": league, "fixture": fixture, "stage": stage, "reason": reason}


def scheduled(league, start, end):
    fixtures = []
    if league == "epl":
        with watch.dbread(watch.ENGLISH / "runtime.sqlite3") as db:
            rows = db.execute(
                """WITH versions AS (SELECT f.*,row_number() OVER (PARTITION BY f.fixture_id ORDER BY f.known_at DESC,f.version DESC) n
                FROM fixture_versions f JOIN schedule_receipts s USING(fixture_id,version) WHERE f.known_at<=? AND s.recorded_at<=?)
                SELECT v.*,p.proof,p.proof_digest FROM versions v LEFT JOIN epl_schedule_proofs p ON p.fixture=v.fixture_id AND p.version=v.version
                WHERE v.n=1 AND v.kickoff>? AND v.kickoff<=? ORDER BY v.kickoff,v.fixture_id""",
                (start, start, start, end),
            )
            for row in rows:
                meta = {k: row[k] for k in ("season", "home", "away", "kickoff")}
                meta.update(
                    league=league,
                    forecast_fixture_id=row["fixture_id"],
                    fixture=row["fixture_id"],
                    round=row["round_id"],
                )
                if row["proof"]:
                    proof = json.loads(row["proof"])
                    if (
                        store.digest(proof) == row["proof_digest"]
                        and proof["fixture_id"] == row["fixture_id"]
                        and proof["kickoff"] == row["kickoff"]
                    ):
                        meta.update(
                            fixture=proof["gamma"]["slug"],
                            event_id=str(proof["gamma"]["event_id"]),
                        )
                fixtures.append(meta)
    else:
        with watch.dbread(
            watch.DOMESTIC / "forecast-scheduler" / league / "runtime.sqlite3"
        ) as db:
            for row in db.execute(
                "SELECT metadata,binding FROM domestic_matches WHERE active=1 AND kickoff>? AND kickoff<=? ORDER BY kickoff,fixture",
                (start, end),
            ):
                meta, binding = json.loads(row["metadata"]), json.loads(row["binding"])
                fixtures.append(
                    {
                        **meta,
                        "forecast_fixture_id": meta["fixture_id"],
                        "fixture": binding["slug"],
                        "event_id": str(binding["event_id"]),
                        "team_ids": [str(binding[s + "_id"]) for s in SIDES],
                    }
                )
    return fixtures


def verify_model(fixture):
    require("event_id" in fixture, "no_verified_market_schedule_binding")
    pick = fixture
    now = store.now_utc()
    row, info, arm, alternative, lineups, problem = (
        watch.epl_model if pick["league"] == "epl" else watch.domestic_model
    )(pick, now)
    require(not problem, problem)
    require(
        row["season"] == pick["season"]
        and row["fixture_id"] == pick["forecast_fixture_id"]
        and all(row[s] == pick[s] for s in SIDES),
        "forecast_and_schedule_disagree",
    )
    require(
        info["model"] == "B1" and info["training"] > 0 and len(info["digest"]) == 64,
        "missing_verified_fitted_forecast",
    )
    require(info["horizon"] == "T-60m", "pooled_calibration_requires_T-60m_baseline")
    t = store.instant(now)
    kickoff = store.instant(pick["kickoff"])
    mins = (kickoff - t).total_seconds() / 60
    require(mins > 0, "kickoff_passed")
    if mins <= league_registry.profile(pick["league"]).lineup_deadline_minutes_2026:
        require(
            all(lineups[s]["confirmed"] for s in SIDES),
            "official_starting_elevens_unconfirmed",
        )
        require(
            all(
                not lineups[s].get("received_at")
                or lineups[s]["received_at"] <= info["cutoff"]
                for s in SIDES
            ),
            "forecast_predates_confirmed_elevens",
        )
    if mins <= 60:
        require(
            info["horizon"] == "T-60m"
            or store.instant(info["cutoff"]) >= kickoff - timedelta(minutes=60),
            "forecast_predates_final_hour",
        )
    return {
        "info": info,
        "lineups": {s: lineups[s]["confirmed"] for s in SIDES},
        "with": arm,
        "without": alternative,
    }


def scoreboard(league, date):
    reply, permission_sha = watch.football_odds_access.fetch(
        league, "scoreboard", {"dates": date.replace("-", ""), "limit": 100, "observation": time.time_ns()}
    )
    require(provider.fresh_observation(reply), "stale_ESPN_HTTP_receipt")
    payload = json.loads(reply.body)
    source = {
        "source": "DraftKings via ESPN",
        "permission_sha256": permission_sha,
        "received_at": reply.received_at,
        "sha256": sha(reply.body),
    }
    return payload["events"], source


def club(league, label):
    return opportunity._club(ALIASES.get(league, {}).get(label, label))


def odds_for(f, listing):
    events, receipt = listing
    name = [club(f["league"], f[s]) for s in SIDES]
    matches = []
    for event in events:
        if (
            len(event.get("competitions", [])) != 1
            or stamp(event["date"]) != f["kickoff"]
        ):
            continue
        game = event["competitions"][0]
        teams = {
            t["homeAway"]: t["team"]["displayName"] for t in game.get("competitors", [])
        }
        if (
            set(teams) == set(SIDES)
            and [club(f["league"], teams[s]) for s in SIDES] == name
            and stamp(game["date"]) == f["kickoff"]
        ):
            matches.append((event, game, teams))
    require(len(matches) == 1, "ESPN_fixture_missing_or_ambiguous")
    event, game, teams = matches[0]
    require(
        all(r["status"]["type"]["name"] == "STATUS_SCHEDULED" for r in (event, game)),
        "ESPN_fixture_not_scheduled",
    )
    book = [
        r
        for r in game.get("odds", [])
        if r.get("provider", {}).get("name") == "DraftKings"
    ]
    require(len(book) == 1, "DraftKings_market_missing_or_ambiguous")
    book = book[0]
    groups = {}
    failures = {}
    for family in ("moneyline", "total"):
        try:
            keys = OUTCOMES if family == "moneyline" else ("over", "under")
            quote = {key: book[family][key]["close"] for key in keys}
            prices = {key: quote[key]["odds"] for key in keys}
            require(
                all(
                    re.fullmatch(r"[+-]?\d+", str(x)) and abs(D(x)) >= 100
                    for x in prices.values()
                ),
                "invalid_American_odds",
            )
            if family == "total":
                line = str(quote["over"]["line"])
                require(
                    re.fullmatch(r"o\d+\.5", line)
                    and quote["under"]["line"] == "u" + line[1:],
                    "nonmatching_half_goal_lines",
                )
                line = line[1:]
            else:
                line = None
                for side in keys:
                    old = book.get(
                        "drawOdds" if side == "draw" else side + "TeamOdds", {}
                    ).get("moneyLine")
                    require(
                        old is None or D(old) == D(prices[side]),
                        "contradictory_moneylines",
                    )
            values = {k: D(v) for k, v in prices.items()}
            inverse = {
                k: 100 / (100 + v) if v > 0 else -v / (100 - v)
                for k, v in values.items()
            }
            margin = sum(inverse.values())
            require(D(1) <= margin <= D("1.3"), "invalid_bookmaker_overround")
            groups[family] = {
                "line": line,
                "prices": prices,
                "probabilities": {k: v / margin for k, v in inverse.items()},
                "overround": margin - 1,
            }
        except ERRORS as exc:
            failures[family] = str(exc)
    require(groups, "no_verified_bookmaker_probabilities")
    event_id = str(event["id"])
    require(event_id.isdigit(), "invalid_ESPN_event_id")
    return {
        "groups": groups,
        "gaps": failures,
        "teams": teams,
        "id": event_id,
        "receipt": {
            **receipt,
            "url": "https://www.espn.com/soccer/match/_/gameId/" + event_id,
        },
    }


def markets_for(f):
    root = f["fixture"]
    rows = []
    had_child = False
    for suffix in ("", "-more-markets"):
        try:
            event, time_seen, key = watch.read_event(root + suffix)
        except HTTPError as exc:
            if suffix and exc.code == 404:
                break
            raise
        identity = big5_market._identity(event, f["league"])
        require(
            identity[1] == root
            and identity[3] == f["kickoff"]
            and big5_market_schedule.pregame_flags(event, time_seen)
            in ("schedule_only", "explicit_pregame")
            and event.get("closed") is False
            and event.get("active") is True,
            "Gamma_fixture_changed_or_closed",
        )
        if not suffix:
            require(
                identity[6] == f["event_id"]
                and (not f.get("team_ids") or list(identity[4:6]) == f["team_ids"]),
                "Gamma_event_or_participants_changed",
            )
            if f["league"] == "epl":
                teams = provider.teams(event)
                require(
                    all(
                        club("epl", f[s])
                        in {
                            club("epl", str(teams[s].get(k))) for k in ("alias", "name")
                        }
                        for s in SIDES
                    ),
                    "Gamma_EPL_participants_changed",
                )
            parent_teams = identity[4:6]
        else:
            require(identity[4:6] == parent_teams, "Gamma_child_participants_changed")
            had_child = True
        for m in provider.normalized_markets(
            event, sport=f["league"], project_teams=True
        ):
            rows.append({**m, "source_sha256": key, "recorded_at": time_seen})
    selected, _ = big5_market._selection(rows, all_lines=True)
    require(selected, "no_reviewed_open_Gamma_contracts")
    return [
        m for m in selected.values() if m["game_times"] == [f["kickoff"]]
    ], had_child


def possibilities(f, model, book, markets):
    groups = book["groups"]
    result = []
    for m in markets:
        g = groups.get(m["family"])
        if not g or (m["family"] == "total" and (D(m["line"]) != D(g["line"]) or D(m["line"]) != D("2.5"))):
            continue
        fee = shadow_market._fee(None, m, m["recorded_at"])
        for t in m["tokens"]:
            terms = ledger_terms._terms_with_fee(
                None, m, t["token_id"], m["recorded_at"], None, fee
            )
            p = watch.normal_p(model["with"], terms)
            other = watch.normal_p(model["without"], terms)
            fair = rules.fair_values(p, terms)[0]
            adjusted = calibrated(model["without"], terms)
            robust = min(fair, rules.fair_values(adjusted, terms)[0])
            ext = g["probabilities"][
                m["side"] if m["family"] == "moneyline" else terms["side"]
            ]
            if terms["side"] == "no":
                ext = 1 - ext
            external = rules.fair_values(ext, terms)[0]
            result.append(
                {
                    "market": m,
                    "token": t,
                    "fee": fee,
                    "terms": terms,
                    "model_probability": p,
                    "no_news_probability": other,
                    "calibrated_no_news_probability": adjusted,
                    "fair": fair,
                    "robust": robust,
                    "external_probability": ext,
                    "external_fair": external,
                    "blend": (robust + external) / 2,
                    "odds": g["prices"],
                }
            )
    require(result, "no_contract_matches_the_external_market")
    return result


def best_limit(row, quantity, tick):
    fair, blend = row["robust"], row["blend"]
    fee = row["fee"]
    external_ceiling = row["external_probability"]
    limit = (min(fair - TARGET_EDGE, blend, external_ceiling) / tick).to_integral_value(
        rounding=ROUND_FLOOR
    ) * tick
    while limit > 0:
        cost = rules.price_fill([(limit, quantity, limit, limit)], quantity, fee)[
            "cost"
        ]
        if fair - cost >= TARGET_EDGE and blend > cost and limit <= external_ceiling:
            return limit
        limit -= tick
    return D(0)


def title(f, m, t):
    side = m["side"]
    out = t["outcome"].casefold()
    if m["family"] == "total":
        selection = ("全场大" if out == "over" else "全场小") + str(m["line"])
    elif side == "draw":
        selection = "平局" if out == "yes" else "不平局"
    else:
        selection = f[side] + (" 胜" if out == "yes" else " 不胜")
    return f["home"] + " vs " + f["away"] + " · " + selection


def evaluate(f, model, book, r, first, second, account):
    m, t, fee = r["market"], r["token"], r["fee"]
    token = t["token_id"]
    views = [watch.inspect_book(b[token]) for b in (first, second)]
    q = [v[0] for v in views]
    require(
        store.instant(q[1]["requested_at"]) - store.instant(q[0]["received_at"])
        >= timedelta(seconds=max(5, m["seconds_delay"]))
        and q[1]["received_at"] < f["kickoff"],
        "two_receipt_timing_invalid",
    )
    tick = max(z["tick"] for z in q)
    minimum = max(z["min_size"] for z in q)
    quantity = max(minimum, COMPARISON_SHARES)
    if any(
        z["spread"] > rules.MAX_SPREAD
        or max(abs(z["mid"] - r["fair"]), abs(z["mid"] - r["robust"])) > rules.MAX_GAP
        for z in q
    ):
        return None
    limit = best_limit(r, quantity, tick)
    common = rules.pair_depth(views[0][1], views[1][1], limit)
    fill = rules.price_fill(common, quantity, fee)
    if (
        not fill["net"]
        or fill["gross"] != quantity
        or r["robust"] - fill["cost"] < TARGET_EDGE
        or r["blend"] <= fill["cost"]
        or any(qt["ask"] > r["external_probability"] for qt in q)
    ):
        return None
    ref = None
    account_time = None
    if account:
        plan = size_at_book(
            [(z[0], z[1]) for z in common],
            fee,
            r["robust"],
            r["external_fair"],
            account["cash"],
            limit,
        )
        if plan and plan["shares"] >= minimum:
            ref = plan["planning_spend"]
            account_time = account["as_of"]
    at = q[1]["received_at"]
    expiry = min(store.instant(q[0]["received_at"]) + TTL, store.instant(f["kickoff"]))
    return {
        "id": m["condition_id"] + ":" + token,
        "condition_id": m["condition_id"],
        "token_id": token,
        "fixture": f["fixture"],
        "fixture_id": f["forecast_fixture_id"],
        "league": f["league"],
        "display": title(f, m, t),
        "kickoff": f["kickoff"],
        "home": f["home"],
        "away": f["away"],
        "market_slug": m["slug"],
        "outcome": t["outcome"],
        "family": m["family"],
        "side": m["side"],
        "line": m["line"],
        "market_url": "https://polymarket.com/event/"
        + m["event_slug"]
        + "/"
        + m["slug"],
        "limit_price": limit,
        "best_ask": q[1]["ask"],
        "worst_best_ask": max(z["ask"] for z in q),
        "checked_at": at,
        "valid_until": stamp(expiry),
        "reference_stake": ref,
        "reference_stake_currency": "simulated_USD",
        "research": {
            "model_probability": r["model_probability"],
            "no_news_probability": r["no_news_probability"],
            "conservative_fair": r["robust"],
            "model_sha256": model["info"]["digest"],
            "model_horizon": model["info"]["horizon"],
            "model_observed_at": model["info"]["published"],
            "model_input_cutoff": model["info"]["cutoff"],
            "model_training_matches": model["info"]["training"],
            "news_status": model["info"].get("news_status"),
            "injury_evidence_state": model["info"].get(
                "injury_state", "see_production_news_facts"
            ),
            "news_coverage": model["info"].get("reported_news", {}).get("coverage", {}),
            "calibration": fit_evidence(r["terms"]),
            "calibrated_no_news_probability": r["calibrated_no_news_probability"],
            "official_lineups_confirmed": model["lineups"],
            "bookmaker": {
                **book["receipt"],
                "prices": r["odds"],
                "no_vig_probability": r["external_probability"],
                "conservative_fair": r["external_fair"],
            },
            "blended_probability": r["blend"],
            "net_unit_cost": fill["cost"],
            "net_edge": r["robust"] - fill["cost"],
            "blended_net_edge": r["blend"] - fill["cost"],
            "bookmaker_probability_less_net_cost": r["external_probability"]
            - fill["cost"],
            "fee_at_sample": fill["fees"],
            "taker_fee_method": fee.get("policy"),
            "fee_schedule": m["fee_schedule"],
            "comparison_shares": quantity,
            "depth_at_limit": sum(z[1] for z in common),
            "orderbooks": [
                {
                    k: z[k]
                    for k in (
                        "received_at",
                        "requested_at",
                        "exchange_at",
                        "source_sha256",
                        "book_hash",
                        "bid",
                        "ask",
                        "spread",
                        "bid_size",
                        "ask_size",
                    )
                }
                for z in q
            ],
            "gamma_received_at": m["recorded_at"],
            "gamma_sha256": m["source_sha256"],
            "rule_sha256": m["rule_sha256"],
            "account_as_of": account_time,
            "sizing_cash": account["cash"] if account else None,
            "kelly_fraction": FRACTION,
        },
    }


def sampling(markets):
    # CLOB accepts up to 500 tokens per batch. Isolate partial batches and keep per-token omissions.
    items = list(markets.values())
    chunk = 120
    groups = [(items[i : i + chunk],) for i in range(0, len(items), chunk)]
    books = {}
    errors = {}
    for (group,), val, err in group_work(watch.sample_books, groups, 4):
        if err:
            errors.update({t["token_id"]: err for m in group for t in m["tokens"]})
        else:
            books.update(val[0])
            errors.update(val[1])
    return books, errors


def fresh_account(now):
    try:
        s = read(PAPER / "account-status.json")
        require(
            s["account_policy"] == POLICY
            and s["state"] == "ok"
            and s["verified_orders_at_venue"] == 0,
            "invalid_account_report",
        )
        cash = rules.number(s["total_available_cash"])
        require(
            cash >= 0
            and timedelta(0)
            <= store.instant(now) - store.instant(s["as_of"])
            <= timedelta(minutes=40),
            "stale_account_cash",
        )
        require(
            sha((PAPER / "account-input.json").read_bytes())
            == s["account_input_sha256"],
            "account_report_has_new_input",
        )
        return {"cash": cash, "as_of": s["as_of"]}, None
    except ERRORS as exc:
        return None, str(exc)


def run(start):
    end = stamp(store.instant(start) + timedelta(days=7))
    gaps = []
    stages = {}
    fixtures = {}
    closed_leagues = []

    def gap(f, stage, reason):
        gaps.append(gaps_row(f.get("league"), f.get("fixture"), stage, str(reason)))

    for (league, *_), rows, err in group_work(
        scheduled, [(k, start, end) for k in LEAGUES], 5
    ):
        if err:
            gaps.append(gaps_row(league, None, "schedule", err))
            continue
        closed_leagues.append(league)
        for f in rows:
            slug = f["fixture"]
            require(slug not in fixtures, "duplicate_scheduled_fixture")
            fixtures[slug] = f
            stages[slug] = {
                "fixture": slug,
                "league": league,
                "kickoff": f["kickoff"],
                "state": "pending",
            }
    held = read(PAPER / "picks.json")
    held = held["picks"] if isinstance(held, dict) else held
    known_tokens = {str(p["token_id"]) for p in held}
    known_games = {p["fixture"] for p in held}
    known_ids = {p["forecast_fixture_id"] for p in held}
    eligible = []
    for f in fixtures.values():
        if f["fixture"] in known_games or f["forecast_fixture_id"] in known_ids:
            stages[f["fixture"]]["state"] = "existing_pick"
        else:
            eligible.append(f)
    if HERE.joinpath("feed.json").is_file():
        prev = read(HERE / "feed.json")
        require(prev.get("schema_version") == 1, "unrecognized_previous_feed")
    else:
        prev = {}
    # Once a direction is published it owns this fixture until kickoff, including periods of uncertainty.
    pins = {
        k: v
        for k, v in prev.get("published_fixtures", {}).items()
        if v["kickoff"] > start and prev.get("policy_id") in PIN_POLICIES
    }
    models = {}
    index = {}
    markets = {}
    pagekeys = sorted({(f["league"], f["kickoff"][:10]) for f in eligible})
    tasks = []
    tasks.extend((verify_model, (f,), f, "forecast") for f in eligible)
    tasks.extend((markets_for, (f,), f, "gamma") for f in eligible)
    tasks.extend(
        (scoreboard, page, {"league": page[0], "fixture": None}, "scoreboard")
        for page in pagekeys
    )
    with ThreadPoolExecutor(max_workers=10) as pool:
        work = {
            pool.submit(fn, *args): (args, f, stage) for fn, args, f, stage in tasks
        }
        for future in as_completed(work):
            args, f, stage = work[future]
            try:
                result = future.result()
                if stage == "forecast":
                    models[f["fixture"]] = result
                    stages[f["fixture"]]["injury_evidence_state"] = result["info"].get(
                        "injury_state", "see_production_news_facts"
                    )
                    stages[f["fixture"]]["official_lineups_confirmed"] = result[
                        "lineups"
                    ]
                elif stage == "gamma":
                    markets[f["fixture"]] = result[0]
                else:
                    index[args] = result
            except ERRORS as exc:
                gap(f, stage, exc)
    market_index = {}
    candidates = {}
    matched = {}
    attempted = Counter()
    gaps_by_fixture = defaultdict(list)
    for row in gaps:
        if row["fixture"]:
            gaps_by_fixture[row["fixture"]].append(row["stage"])
    for f in eligible:
        slug = f["fixture"]
        key = (f["league"], f["kickoff"][:10])
        state = stages[slug]
        if slug not in models or slug not in markets or key not in index:
            if key not in index:
                gap(f, "scoreboard", "ESPN_scoreboard_unavailable_for_date")
            state["state"] = "insufficient_evidence"
            continue
        try:
            book = odds_for(f, index[key])
            matched[slug] = book
            for data_family, reason in book["gaps"].items():
                gap(f, "bookmaker_" + data_family, reason)
                gaps_by_fixture[slug].append("bookmaker_" + data_family)
            if any(m["family"] == "total" and D(m["line"]) != D("2.5") for m in markets[slug]):
                gap(f, "calibration", "only_full_game_total_2.5_calibrated")
                gaps_by_fixture[slug].append("calibration")
            rows = possibilities(f, models[slug], book, markets[slug])
            candidates[slug] = rows
            for r in rows:
                m = r["market"]
                token = str(r["token"]["token_id"])
                if token in known_tokens:
                    continue
                market_index[m["condition_id"]] = m
        except ERRORS as exc:
            gap(f, "cross_market_binding", exc)
            state["state"] = "insufficient_evidence"
    account, account_gap = fresh_account(store.now_utc())
    first, err1 = sampling(market_index)
    if first:
        time.sleep(max(5, max(m["seconds_delay"] for m in market_index.values())))
    second, err2 = sampling(market_index)
    evaluated = set()
    verified = set()
    passed = defaultdict(list)
    for slug, rows in candidates.items():
        state = stages[slug]
        f = fixtures[slug]
        failures = set()
        pin = pins.get(f["forecast_fixture_id"])
        pinned = pin and pin["token_id"]
        if pinned and all(r["token"]["token_id"] != pinned for r in rows):
            gap(
                f,
                "published_direction",
                "previously_published_market_currently_unavailable",
            )
            gaps_by_fixture[slug].append("published_direction")
        for r in rows:
            token = r["token"]["token_id"]
            m = r["market"]
            both = {t["token_id"] for t in m["tokens"]}
            if token in known_tokens or (pinned and token != pinned):
                continue
            if not both.issubset(first) or not both.issubset(second):
                failures.add(
                    next(
                        (
                            err1.get(t) or err2.get(t)
                            for t in both
                            if t not in first or t not in second
                        ),
                        "missing_CLOB_pair",
                    )
                )
                continue
            try:
                row = evaluate(
                    f,
                    models[slug],
                    matched[slug],
                    r,
                    first,
                    second,
                    account,
                )
                evaluated.add(slug)
                attempted[slug] += 1
                if row:
                    passed[slug].append((row, r))
            except ERRORS as exc:
                failures.add(str(exc))
        if failures:
            gap(f, "CLOB_books", " / ".join(sorted(map(str, failures))))
            gaps_by_fixture[slug].append("CLOB_books")
        if slug in evaluated and not gaps_by_fixture[slug]:
            verified.add(slug)
        state["state"] = (
            "no_qualifying_pick" if slug in verified else "insufficient_evidence"
        )
        state["verified_directions"] = attempted[slug]
        if pinned:
            state["previously_published_token"] = pinned
    to_check = {
        r["market"]["condition_id"]: r for rows in passed.values() for row, r in rows
    }
    settings = {}
    setting_errors = {}
    for (r,), data, err in group_work(
        lambda r: watch.clob_settings(r["market"], r["fee"]),
        [(r,) for r in to_check.values()],
        6,
    ):
        condition = r["market"]["condition_id"]
        settings[condition] = data
        if err:
            setting_errors[condition] = err
    choices = []
    for slug, rows in passed.items():
        f = fixtures[slug]
        state = stages[slug]
        accepted = []
        for row, r in rows:
            settings_row = settings.get(row["condition_id"])
            token = row["token_id"]
            if settings_row is None:
                gap(
                    f,
                    "CLOB_fee",
                    setting_errors.get(
                        row["condition_id"], "no_native_taker_parameters"
                    ),
                )
                verified.discard(slug)
                continue
            if any(
                rules.number(settings_row["minimum_order_size"])
                != rules.number(b[token]["min_order_size"])
                or rules.number(settings_row["tick"])
                != rules.number(b[token]["tick_size"])
                for b in (first, second)
            ):
                gap(f, "CLOB_fee", "native_tick_or_minimum_changed")
                verified.discard(slug)
                continue
            row["research"]["native_fee_proof"] = settings_row
            row["checked_at"] = max(row["checked_at"], settings_row["received_at"])
            if row["checked_at"] >= row["valid_until"]:
                continue
            accepted.append(row)
        if accepted:
            top = min(
                accepted,
                key=lambda r: (
                    -r["research"]["blended_net_edge"],
                    -r["research"]["net_edge"],
                    r["token_id"],
                ),
            )
            choices.append(top)
            state["state"] = "recommended"
            state["token_id"] = top["token_id"]
        elif slug not in verified:
            state["state"] = "insufficient_evidence"
    finished = store.now_utc()
    choices = [r for r in choices if r["valid_until"] > finished]
    for r in choices:
        pins[r["fixture_id"]] = {
            "fixture": r["fixture"],
            "kickoff": r["kickoff"],
            "token_id": r["token_id"],
            "condition_id": r["condition_id"],
        }
    state = "partial" if gaps else "ok"
    if not closed_leagues or (eligible and not evaluated):
        state = "unavailable"
    expiry = min(
        [store.instant(finished) + TTL]
        + [store.instant(r["valid_until"]) for r in choices]
    )
    choices.sort(
        key=lambda r: (-r["research"]["blended_net_edge"], r["kickoff"], r["id"])
    )
    return {
        "schema_version": 1,
        "policy_id": FEED_POLICY,
        "status": state,
        "started_at": start,
        "checked_at": finished,
        "window_end": end,
        "valid_until": stamp(expiry),
        "recommendations": choices,
        "published_fixtures": pins,
        "coverage": {
            "scheduled_leagues": sorted(closed_leagues),
            "scheduled": dict(Counter(f["league"] for f in fixtures.values())),
            "scheduled_fixtures": len(fixtures),
            "existing_fixtures_excluded": sum(
                v["state"] == "existing_pick" for v in stages.values()
            ),
            "existing_tokens_excluded": len(known_tokens),
            "verified_models": len(models),
            "matched_bookmakers": len(matched),
            "sampled_contracts": len(market_index),
            "verified_direction_count": sum(attempted.values()),
            "verified_fixtures": sorted(verified),
            "fixture_results": sorted(
                stages.values(), key=lambda s: (s["kickoff"], s["fixture"])
            ),
            "gaps": gaps,
            "account_status": account_gap or "ok",
        },
    }


def atomic(path, data):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent, prefix=".feed-", delete=False
        ) as file:
            tmp = Path(file.name)
            file.write(
                json.dumps(
                    data, ensure_ascii=False, indent=1, default=str, allow_nan=False
                ).encode()
                + b"\n"
            )
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if tmp:
            tmp.unlink(missing_ok=True)


def main():
    with (HERE / ".discover.lock").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        start = store.now_utc()
        try:
            data = run(start)
        except ERRORS as exc:
            now = store.now_utc()
            path = HERE / "feed.json"
            previous = read(path) if path.exists() else {}
            data = {
                "schema_version": 1,
                "policy_id": FEED_POLICY,
                "status": "unavailable",
                "started_at": start,
                "checked_at": now,
                "window_end": stamp(store.instant(start) + timedelta(days=7)),
                "valid_until": now,
                "recommendations": [],
                "published_fixtures": previous.get("published_fixtures", {})
                if previous.get("policy_id") in PIN_POLICIES
                else {},
                "coverage": {
                    "verified_fixtures": [],
                    "fixture_results": [],
                    "gaps": [gaps_row(None, None, "run", str(exc))],
                },
            }
        atomic(HERE / "feed.json", data)
        print(
            json.dumps(
                {
                    "status": data["status"],
                    "checked_at": data["checked_at"],
                    "seconds": round(
                        (
                            store.instant(data["checked_at"]) - store.instant(start)
                        ).total_seconds(),
                        1,
                    ),
                    "recommendations": len(data["recommendations"]),
                    "verified_fixtures": len(data["coverage"]["verified_fixtures"]),
                    "gaps": len(data["coverage"]["gaps"]),
                }
            )
        )


if __name__ == "__main__":
    main()
