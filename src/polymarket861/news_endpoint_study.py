"""Prospective EPL news price-gap study; opt-in registration never retrofits existing seals."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import timedelta
from pathlib import Path

from . import experiment_statistics, news_accumulation, news_evaluation, news_market, news_study_plan, store

ENDPOINTS = {"home_win": ("home_win", "result"), "full_over_2_5": ("total_over_2_5", "total")}
CLOCKS = ("market_close", "scheduled_market_close")
HORIZONS = {"T-24h": timedelta(hours=24), "T-60m": timedelta(minutes=60)}
SEASONS = ("2026-27", "2027-28")
END = "2028-07-01T00:00:00.000000Z"
FAMILY = len(HORIZONS) * (len(CLOCKS) * len(ENDPOINTS) + 1)
PLAN = {
    "version": "epl-news-home-and-full-over-v1", "eligible_seasons": SEASONS, "ends_at_exclusive": END,
    "enrollment": "One immutable registration per runtime, one prospective mode. Start is at or after actual local registration. Attach only on insertion of a qualifying new native main seal; never backfill.",
    "population": "EPL primary seals using the named native declaration and timely study-qualified news for both clubs. Include zero news changes. Only the first bound probability era is eligible, selected by capture order without reference to news movement, outcomes, coverage or available books. Capture starts at the real local clock immediately following enrollment.",
    "native_pair_declaration_sha256": news_study_plan.PLAN_SHA256,
    "horizons": tuple(HORIZONS), "endpoints": tuple(ENDPOINTS), "closing_clocks": CLOCKS,
    "admission": "Reverify native B1/news probabilities and contemporaneous source originals. For each endpoint require the same contracts, rules and matched decision and close books. Actual first-whistle and scheduled explicitly pregame closes have separate strata.",
    "estimand": "Through-origin slope of (close minus B1) on (with-news minus B1) per endpoint, horizon and clock. One fixture per cell, compared within the initial bound probability era. No pooled eras.",
    "reference": "Normalized three-way home-win midpoint; Over midpoint for the regulation full-match 2.5-goal contract after both sides pass the native checks.",
    "interval": "Two-sided Bonferroni round-cluster CR1/Student intervals with G-1 degrees of freedom. Only rounds with movement above 1e-12 contribute to slope information. Missing independent rounds or residual variance leaves the interval undefined.",
    "score_guardrail": "At each horizon the verified regulation four-component score delta (with-news minus B1) has a match-weighted round-cluster interval; upper simultaneous bound below zero flags deterioration.",
    "family_comparisons": FAMILY, "family_confidence": 0.95, "zero_move_tolerance": 1e-12,
    "reporting": "Before the fixed end, publish only collection coverage and reasons. Reveal the ten registered estimates together after the fixed end using evidence received before the end. Descriptive research; no automatic promotion or trading.",
}
PLAN_SHA = store.digest(PLAN)

_DECLARATIONS = """CREATE TABLE IF NOT EXISTS news_endpoint_declarations(
 version TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, manifest TEXT NOT NULL,
 starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, mode TEXT NOT NULL)"""
_BINDINGS = """CREATE TABLE IF NOT EXISTS news_endpoint_bindings(
 capture_order INTEGER PRIMARY KEY, seal TEXT NOT NULL UNIQUE REFERENCES forecast_seals(digest),
 declaration TEXT NOT NULL REFERENCES news_endpoint_declarations(digest), bound_at TEXT NOT NULL,
 probability_era TEXT NOT NULL)"""
# A separate table preserves the exact legacy seal. The trigger runs synchronously in the
# forecast transaction, even for concurrent writers, and cannot attach a pre-existing seal.
# Native seal/proof validation is repeated at analysis time; a SQL pointer alone is insufficient.
_CLOCK_SQL = "(strftime('%Y-%m-%dT%H:%M:%f','now') || '000Z')"
_TRIGGER = f"""CREATE TRIGGER news_endpoint_bind_on_insert AFTER INSERT ON forecast_seals
WHEN NEW.protocol='{store.PROTOCOL}' AND NEW.horizon IN ('T-24h','T-60m') AND json_valid(NEW.data_json)
BEGIN
 INSERT INTO news_endpoint_bindings(seal,declaration,bound_at,probability_era)
 SELECT NEW.digest,d.digest,{_CLOCK_SQL},json_extract(NEW.data_json,'$.news_study.era_sha256')
 FROM news_endpoint_declarations d
 WHERE d.version='{PLAN["version"]}' AND d.mode=NEW.mode
 AND NEW.fixture_id GLOB ('epl/' || json_extract(NEW.data_json,'$.season') || '/*')
 AND json_extract(NEW.data_json,'$.season') IN ('2026-27','2027-28')
 AND json_extract(NEW.data_json,'$.purpose')='main'
 AND json_extract(NEW.data_json,'$.news_study.plan_sha256')='{news_study_plan.PLAN_SHA256}'
 AND length(json_extract(NEW.data_json,'$.news_study.era_sha256'))=64
 AND json_extract(NEW.data_json,'$.news_study.era_sha256') NOT GLOB '*[^a-f0-9]*'
 AND d.starts_at<=json_extract(NEW.data_json,'$.input_cutoff')
 AND json_extract(NEW.data_json,'$.input_cutoff')<=NEW.sealed_at AND NEW.sealed_at<=NEW.cutoff
 AND julianday(NEW.sealed_at)<=julianday('now') AND {_CLOCK_SQL}<=NEW.cutoff
 AND NEW.cutoff<NEW.kickoff AND NEW.kickoff<d.ends_at;
END"""


def _trigger_ok(db):
    row = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='news_endpoint_bind_on_insert'").fetchone()
    return bool(row and row[0] == _TRIGGER)


def _read_declaration(db):
    row = db.execute("SELECT * FROM news_endpoint_declarations WHERE version=?", (PLAN["version"],)).fetchone()
    if not row:
        return None
    manifest = json.loads(row["manifest"])
    if (store.digest(manifest) != row["digest"] or manifest.get("plan_sha256") != PLAN_SHA
            or manifest.get("version") != PLAN["version"] or manifest.get("ends_at") != END
            or any(manifest.get(k) != row[k] for k in ("mode", "starts_at", "ends_at"))
            or manifest["mode"] not in {"technical", "formal"}
            or not manifest["registered_at"] <= manifest["starts_at"] < END):
        raise ValueError("Invalid stored EPL endpoint study declaration")
    if not _trigger_ok(db):
        raise ValueError("EPL endpoint study capture trigger missing or changed")
    return {**manifest, "registration_sha256": row["digest"]}


def enroll(data_dir, *, starts_at=None, mode="technical"):
    if mode not in ("technical", "formal"):
        raise ValueError("Choose one prospective technical or formal mode")
    start = store.iso(starts_at) if starts_at else None
    with closing(store.connect(data_dir)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(_DECLARATIONS)
        db.execute(_BINDINGS)
        existing = _read_declaration(db)
        if existing:
            if existing["mode"] == mode and (start is None or existing["starts_at"] == start):
                return existing
            raise ValueError("This study already has an immutable local start and mode")
        now = store.now_utc()
        if start is None:
            start = now
        if not now <= start < END:
            raise ValueError("Start must be at or after registration and before 2028-07-01 UTC")
        manifest = {"version": PLAN["version"], "plan_sha256": PLAN_SHA, "mode": mode,
                    "registered_at": now, "starts_at": start, "ends_at": END}
        db.execute(_TRIGGER)
        digest = store.digest(manifest)
        db.execute("INSERT INTO news_endpoint_declarations VALUES(?,?,?,?,?,?)",
                   (PLAN["version"], digest, store.canonical(manifest), start, END, mode))
        return {**manifest, "registration_sha256": digest}


def _cell(rows, field, endpoint, reveal):
    native_key, family = ENDPOINTS[endpoint]
    points = []
    missing = Counter()
    for row in rows:
        observation = row.get(field) or {}
        value = observation.get("endpoints", {}).get(native_key)
        if value:
            base = value["no_news"]
            points.append(((row["season"], row["round_id"]), row["implementation"],
                           value["with_news"] - base, value["close"] - base))
        else:
            reasons = (observation.get("missing") or {}).get(family)
            if isinstance(reasons, dict) and reasons:
                reason = ",".join(f"{k}={v}" for k, v in sorted(reasons.items()))
            else:
                reason = observation.get("reason", "no_matched_endpoint_receipts")
            missing[reason] += 1
    result = {"eligible_news_pairs": len(rows), "matched_books": len(points), "missing": dict(missing)}
    if reveal or not points:
        fit = news_market.price_gap_slope(points, family=FAMILY, no_pairs_status="no_matched_endpoint_pairs")
        return {**result, **fit}
    return {**result, "status": "sealed_until_fixed_end", "slope": None, "interval": None}


def _guardrail(rows, reveal):
    groups = defaultdict(list)
    for row in rows:
        if row.get("scored"):
            groups[row["season"], row["round_id"]].append(
                [row["scores"]["with_news"]["score"] - row["scores"]["no_news"]["score"]])
    n = sum(map(len, groups.values()))
    if not n or not reveal:
        return {"pairs": n, "status": "sealed_until_fixed_end" if n else "no_verified_scored_pairs", "interval": None}
    ci = experiment_statistics.clustered_t(groups.values(), FAMILY)
    interval = None if ci is None else {k: v for k, v in ci.items() if k != "intervals"} | ci["intervals"][0]
    return {"pairs": n, "interval": interval, **news_accumulation.guardrail({"interval": interval})}


def _summary(registration, rows, due, reasons, *, reveal, era=None, bound=0):
    estimates = {}
    for horizon in HORIZONS:
        cohort = rows.get(horizon, [])
        estimates[horizon] = {"past_scheduled_windows": due.get(horizon, 0), "qualified_news_pairs": len(cohort),
                             "exclusions": dict(reasons.get(horizon, {})), "clocks": {
                                 field: {ep: _cell(cohort, field, ep, reveal) for ep in ENDPOINTS}
                                 for field in CLOCKS}, "score_guardrail": _guardrail(cohort, reveal)}
    return {"version": PLAN["version"], "plan_sha256": PLAN_SHA, "registration": registration,
            "state": "not_registered" if not registration else ("window_ended" if reveal else "prospective_collection"),
            "bound_seals": bound, "initial_bound_probability_era": era, "family_comparisons": FAMILY,
            "horizons": estimates, "automatic_promotion": False}


def _receipts_before_end(db, row):
    # The price receipts were already verified before kickoff (< END); an
    # independent first-whistle proof or result admission can arrive later.
    record = dict(row)
    actual = record.get("market_close") or {}
    if actual.get("endpoints"):
        timely = db.execute("SELECT 1 FROM football_kickoff_receipts WHERE proof_id=? AND fixture_id=? AND received_at<?",
                            (actual.get("kickoff_proof"), record["fixture_id"], END)).fetchone()
        if not timely:
            record["market_close"] = {"reason": "actual_kickoff_proof_missing_by_study_end"}
    if record.get("scored") and (not record.get("result_confirmed_at") or record["result_confirmed_at"] >= END):
        record["scored"] = False
    return record


def _scope_fixture_history(db, as_of):
    """Scope the normal schedule/result queries (including nested validators) to this read connection.

    Filtering versions before the native ranking keeps a backdated later arrival
    from hiding the version actually on hand. TEMP objects never touch the runtime DB.
    """
    db.execute("CREATE TEMP TABLE endpoint_study_clock(cut TEXT NOT NULL)")
    db.execute("INSERT INTO endpoint_study_clock VALUES(?)", (as_of,))
    db.execute("""CREATE TEMP VIEW fixture_versions AS SELECT f.* FROM main.fixture_versions f
        JOIN main.schedule_receipts s USING(fixture_id,version), endpoint_study_clock w
        WHERE f.known_at<=w.cut AND s.recorded_at<=w.cut""")
    db.execute("""CREATE TEMP VIEW result_versions AS SELECT r.* FROM main.result_versions r, endpoint_study_clock w
        WHERE r.recorded_at<=w.cut AND r.received_at<=w.cut AND r.published_at<=w.cut AND r.finalized_at<=w.cut""")
    db.execute("""CREATE TEMP VIEW result_dispositions AS SELECT d.* FROM main.result_dispositions d, endpoint_study_clock w
        WHERE d.at<=w.cut""")


def report(data_dir):
    path = Path(data_dir).expanduser() / "runtime.sqlite3"
    if not path.is_file():
        return _summary(None, {}, {}, {}, reveal=False)
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='news_endpoint_declarations'").fetchone():
            return _summary(None, {}, {}, {}, reveal=False)
        registration = _read_declaration(db)
        if registration is None:
            return _summary(None, {}, {}, {}, reveal=False)
        now = store.now_utc()
        reveal = now >= END
        as_of = min(now, store.iso(store.instant(END) - timedelta(microseconds=1)))
        _scope_fixture_history(db, as_of)
        binding_rows = db.execute("""SELECT b.seal,b.bound_at,b.probability_era,s.fixture_id,s.horizon,s.kickoff
                    FROM news_endpoint_bindings b JOIN forecast_seals s ON s.digest=b.seal
                    WHERE b.declaration=? AND b.bound_at<=? ORDER BY b.capture_order""",
                                  (registration["registration_sha256"], as_of)).fetchall()
        bindings = {r["seal"]: r for r in binding_rows}
        era = binding_rows[0]["probability_era"] if binding_rows else None
        bound_keys = {(r["fixture_id"], r["horizon"], r["kickoff"]) for r in binding_rows}
        due, accepted = Counter(), defaultdict(list)
        reasons = defaultdict(Counter)
        for season in SEASONS:
            fixtures = [f for f in store.eligible_fixture_rows(db, season=season)
                        if f["fixture_id"].startswith(f"epl/{season}/")]
            for horizon, lead in HORIZONS.items():
                scheduled = [f for f in fixtures if registration["starts_at"] <= store.iso(store.instant(f["kickoff"]) - lead)
                             <= as_of and f["kickoff"] < END]
                due[horizon] += len(scheduled)
                candidates = [f for f in scheduled if (f["fixture_id"], horizon, f["kickoff"]) in bound_keys]
                reasons[horizon]["no_prospective_binding"] += len(scheduled) - len(candidates)
                for row in news_evaluation._records(db, candidates, horizon, registration["mode"], as_of=as_of,
                                                    reopen_news=True, eligible_seals=bindings):
                    binding = bindings.get(row.get("seal"))
                    if not binding or row.get("study_status") != "declared" or row.get("study_plan_sha256") != news_study_plan.PLAN_SHA256:
                        reason = row.get("reason") or "native_seal_failed_reverification"
                    elif (binding["probability_era"] != row.get("implementation") or row["implementation"] != era):
                        reason = "different_probability_era"
                    elif not row.get("timely_news_coverage"):
                        reason = "missing_timely_qualified_two_team_news"
                    elif not registration["starts_at"] <= row["input_cutoff"] <= binding["bound_at"] < row["kickoff"]:
                        reason = "invalid_binding_time"
                    else:
                        accepted[horizon].append(_receipts_before_end(db, row) if reveal else row)
                        continue
                    reasons[horizon][reason] += 1
        return _summary(registration, accepted, due, reasons, reveal=reveal, era=era, bound=len(bindings))


def register(commands):
    command = commands.add_parser("news-endpoints", help="登记英超主胜与全场大 2.5 的前瞻新闻收盘研究")
    sub = command.add_subparsers(dest="news_endpoint_action", required=True)
    plan = sub.add_parser("plan", help="查看预定研究族、两档收盘和揭晓日期")
    plan.set_defaults(func=lambda a: {"plan": PLAN, "plan_sha256": PLAN_SHA})
    registration = sub.add_parser("register", help="登记本机前瞻开始时间，之后自动绑定新封存")
    registration.add_argument("--starts-at", help="带 UTC 偏移的未来时刻，默认从登记时起")
    registration.add_argument("--mode", choices=("technical", "formal"), default="technical")
    registration.set_defaults(func=lambda a: enroll(a.data_dir, starts_at=a.starts_at, mode=a.mode))
    output = sub.add_parser("report", help="查看覆盖、缺件及到期后的十项同时推断")
    output.set_defaults(func=lambda a: report(a.data_dir))
