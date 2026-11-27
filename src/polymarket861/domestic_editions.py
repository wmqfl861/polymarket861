"""Evidence transitions and match-weighted evaluation for prospective domestic forecasts."""

import hashlib
import json
from bisect import bisect_right

from . import football, league_player_status, news_model, store


SCHEMA = """
CREATE TABLE IF NOT EXISTS domestic_change_cursor(
 fixture TEXT NOT NULL, kickoff TEXT NOT NULL, observed TEXT NOT NULL, seal_digest TEXT NOT NULL,
 training_sha TEXT NOT NULL, inputs_sha TEXT, evidence_sha TEXT, sources TEXT NOT NULL,
 PRIMARY KEY(fixture,kickoff));
CREATE TABLE IF NOT EXISTS domestic_evidence_changes(
 id INTEGER PRIMARY KEY, fixture TEXT NOT NULL, kickoff TEXT NOT NULL, observed TEXT NOT NULL,
 before_sha TEXT, after_sha TEXT, sources TEXT NOT NULL, changed_sources TEXT NOT NULL,
 seal_digest TEXT NOT NULL, changed_outputs TEXT NOT NULL, disposition TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS domestic_evidence_fixture ON domestic_evidence_changes(fixture,kickoff,id);
CREATE INDEX IF NOT EXISTS domestic_latest_seal ON domestic_jobs(fixture,kickoff,sealed DESC)
 WHERE sealed IS NOT NULL;
CREATE TABLE IF NOT EXISTS domestic_result_heads(
 fixture TEXT PRIMARY KEY, kickoff TEXT NOT NULL, result_at TEXT NOT NULL, record TEXT NOT NULL,
 complete INTEGER NOT NULL DEFAULT 0);
"""


def training_key(engine, fixture, at):
    """Identity of the available result editions and season sizes that B0/B1 can use."""
    season = fixture.season
    times, events = engine._index(season)
    count = bisect_right(times, at)
    if not hasattr(engine, "_domestic_training_keys"):
        engine._domestic_training_keys = {}
    cache = engine._domestic_training_keys
    if (season, count) not in cache:
        year = football.season_year(season)
        sizes = {s: len(fixtures) for s, fixtures in engine.seasons.items()
                 if year - 3 <= football.season_year(s) <= year}
        versions = hashlib.sha256()
        for i in range(count):
            versions.update(events[i][4])
        cache[season, count] = store.digest([football.MODEL_VERSION, sizes, versions.hexdigest()])
    return cache[season, count]


def input_key(training, snapshot, at):
    # Press links and rosters without a match absence cannot change either arm.
    # Reassess time-sensitive inputs even without a new receipt (expiry, new training exposure).
    status = snapshot.get("player_status", {})
    injuries = status.get("injuries", {})
    has_absences = injuries.get("state") == "observed" and any(
        team.get("players") for team in injuries.get("teams", {}).values())
    sensitive = bool(has_absences or snapshot.get("facts"))
    return store.digest(["domestic-editions-v1", news_model.POLICY_SHA256,
                         league_player_status.SCENARIO_POLICY_SHA256, training,
                         snapshot.get("evidence_sha256") if sensitive else None,
                         store.iso(at) if sensitive else None])


def _probability_digest(value):
    # Discard floating-point fitting noise for the change trigger only. The seal
    # and all evaluation retain the exact model values.
    def rounded(item):
        if isinstance(item, dict):
            return {k: rounded(v) for k, v in item.items()}
        return round(item, 12)
    return store.digest(rounded(value))


def signatures(pair, scenario):
    # Include the match and both team totals. Separate a material sensitivity
    # arm from B1 even when a provider subsequently loses coverage.
    baseline = _probability_digest(pair["no_news"])
    news = _probability_digest(pair["with_news"]) if pair["changed"] else baseline
    sensitivity = baseline
    if scenario.get("changed"):
        sensitivity = _probability_digest({
            "probabilities": scenario["probabilities"], "totals": scenario.get("totals", pair["no_news"]["totals"])
        })
    return {"baseline": baseline, "paired_news": news if news != baseline else None,
            "player_scenario": sensitivity if sensitivity != baseline else None}


def latest(db, key):
    return db.execute(
        "SELECT digest,sealed FROM domestic_jobs WHERE fixture=? AND kickoff=? AND sealed IS NOT NULL "
        "ORDER BY sealed DESC,rowid DESC LIMIT 1", key).fetchone()


def read_forecast(db, key, digest):
    row = db.execute("SELECT payload FROM domestic_jobs WHERE fixture=? AND kickoff=? AND digest=?",
                     (*key, digest)).fetchone()
    if row is None or store.digest(payload := json.loads(row[0])) != digest:
        raise ValueError("forecast digest mismatch")
    return payload


def _cursor(db, key):
    return db.execute("SELECT * FROM domestic_change_cursor WHERE fixture=? AND kickoff=?", key).fetchone()


def _cancel_updates(db, key, keep=""):
    db.execute("UPDATE domestic_jobs SET state='superseded' WHERE fixture=? AND kickoff=? AND "
               "horizon IN ('news_update','forecast_update') AND revision<>? AND "
               "state IN ('pending','retry') AND sealed IS NULL", (*key, keep))


def model_source_failed(snapshot):
    return bool({"rosters", "player_status"}.intersection(snapshot.get("source_errors", ())))


def changes(db, engine, fixture, snapshot, at, calculate):
    """Compare to the actual last seal outside the write lock. Record each observed transition once."""
    if model_source_failed(snapshot):
        # An unreadable input has not withdrawn the last known player observation.
        # Fixed jobs can still seal B1 with the explicitly missing source recorded.
        return
    key = (fixture.fixture_id, store.iso(fixture.kickoff))
    stamp = store.iso(at)
    prior = latest(db, key)
    if not prior or prior["sealed"] > stamp or stamp >= key[1]:
        return
    cursor = _cursor(db, key)
    if cursor and cursor["observed"] > stamp:
        return
    training = training_key(engine, fixture, at)
    inputs = input_key(training, snapshot, at)
    same_seal = cursor and cursor["seal_digest"] == prior["digest"]
    original = None
    seal_training = cursor["training_sha"] if same_seal else ""
    changed = []
    outputs = {}
    if not (same_seal and cursor["inputs_sha"] == inputs):
        original = read_forecast(db, key, prior["digest"])
        original_outputs = signatures(original["pair"], original.get("player_scenario", {}))
        # A previously verified unchanged training cohort permits a cheap assessment of
        # time/availability alone. Every actual seal still calls the real forecast engine.
        cached_base = original["baseline"] if seal_training == training else None
        base, pair, scenario = calculate(engine, fixture, snapshot, at, cached_base)
        # Equal market odds alone do not prove the previous training cohort or
        # fitted rates can be reused by a later availability calculation.
        if cached_base is None:
            seal_training = training if dict(base, cutoff=original["baseline"]["cutoff"]) == original["baseline"] else ""
        outputs = signatures(pair, scenario)
        changed = [name for name in outputs if outputs[name] != original_outputs[name]]
    evidence = snapshot.get("evidence_sha256")
    sources = snapshot.get("source_digests", {})
    previous_evidence = cursor["evidence_sha"] if cursor else original["news"].get("evidence_sha256")
    previous_sources = json.loads(cursor["sources"]) if cursor else original["news"].get("source_digests", {})
    if "press" in snapshot.get("source_errors", ()):
        # Press does not affect either model. Do not invent a link withdrawal when
        # the source temporarily cannot be read, or delay a genuine football update.
        sources = {**sources, "press": previous_sources.get("press")}
        evidence = store.digest(sources) if any(sources.values()) else None
    with db:
        db.execute("BEGIN IMMEDIATE")
        # A newer scan, observation or concurrent seal wins. A failed comparison is
        # retried against the new state on the following tick.
        active = db.execute("SELECT active,checked FROM domestic_matches WHERE fixture=? AND kickoff=?", key).fetchone()
        if not active or not active["active"] or active["checked"] > stamp:
            return
        if latest(db, key) != prior or _cursor(db, key) != cursor:
            return
        if previous_evidence != evidence:
            fields = sorted(k for k in sources.keys() | previous_sources.keys()
                            if sources.get(k) != previous_sources.get(k))
            db.execute("INSERT INTO domestic_evidence_changes "
                       "(fixture,kickoff,observed,before_sha,after_sha,sources,changed_sources,seal_digest,"
                       "changed_outputs,disposition) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (*key, stamp, previous_evidence, evidence, store.canonical(sources),
                        store.canonical(fields or ["evidence"]), prior["digest"], store.canonical(changed),
                        "forecast_change_detected" if changed else "evidence_only"))
        revision = ""
        if changed:
            revision = store.digest([prior["digest"], outputs])
            db.execute("INSERT INTO domestic_jobs(fixture,kickoff,horizon,revision,opens,deadline,state) "
                       "VALUES(?,?,'forecast_update',?,?,?,'pending') ON CONFLICT(fixture,kickoff,horizon,revision) "
                       "DO UPDATE SET state='pending', opens=excluded.opens WHERE domestic_jobs.state='superseded' "
                       "AND domestic_jobs.sealed IS NULL", (*key, revision, stamp, key[1]))
        _cancel_updates(db, key, revision)
        # Cache only a comparison that agreed with the currently sealed forecast. A
        # failed/pending update must be retried and can also be legitimately withdrawn.
        db.execute("INSERT OR REPLACE INTO domestic_change_cursor VALUES(?,?,?,?,?,?,?,?)",
                   (*key, stamp, prior["digest"], seal_training,
                    inputs if not changed else None, evidence, store.canonical(sources)))


def edition_jobs(db, fixture_id=None):
    # A dynamic update has a single prospective endpoint: the last real change
    # before kickoff. Historical news-only seals remain available for audit.
    where = " AND fixture=?" if fixture_id else ""
    return db.execute(
        "SELECT fixture,kickoff,horizon,revision,sealed,digest FROM (SELECT j.fixture,j.kickoff,j.horizon,"
        "j.revision,j.sealed,j.digest,ROW_NUMBER() OVER (PARTITION BY fixture,kickoff,horizon "
        "ORDER BY sealed DESC,rowid DESC) AS rank FROM domestic_jobs j WHERE sealed IS NOT NULL AND "
        "horizon IN ('preview','T-24h','T-60m','forecast_update')" + where + ") WHERE rank=1",
        (fixture_id,) if fixture_id else ())


def evaluation(db, fixture_id=None):
    """One current result per fixture/horizon. Works read-only on pre-upgrade databases."""
    tables = {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE name IN ('domestic_evidence_changes','domestic_result_heads')")}
    heads = "domestic_result_heads" in tables
    where = " AND s.fixture=?" if fixture_id else ""
    # Result heads exclude withdrawn, corrected and moved kickoffs, including when no
    # valid seal exists for the replacement. Old score journals have no heads yet.
    head_filter = (
        " LEFT JOIN domestic_result_heads h ON h.fixture=s.fixture WHERE (h.fixture IS NULL OR "
        "(h.complete=1 AND s.result_at=h.result_at AND s.kickoff=h.kickoff AND json_extract(s.payload,'$.score')="
        "json_extract(h.record,'$.score') AND json_extract(s.payload,'$.source')=json_extract(h.record,'$.source') "
        "AND json_extract(s.payload,'$.grade')=json_extract(h.record,'$.grade'))) "
        if heads else " WHERE 1 ")
    cte = (
        "WITH current AS (SELECT s.fixture,s.kickoff,s.horizon,s.revision,s.result_at,s.payload,"
        "ROW_NUMBER() OVER(PARTITION BY s.fixture,s.horizon ORDER BY s.result_at DESC,j.sealed DESC,"
        "s.rowid DESC) AS rank FROM domestic_scores s JOIN domestic_jobs j "
        "USING(fixture,kickoff,horizon,revision)" + head_filter +
        "AND s.horizon IN ('preview','T-24h','T-60m','forecast_update')" + where + ") ")
    args = (fixture_id,) if fixture_id else ()
    report = {"scored_samples_by_horizon": {}, "match_weighted_scores": {},
              "evidence_changes": 0, "evidence_only_changes": 0}
    aggregate = db.execute(
        cte + "SELECT horizon,COUNT(*) AS matches,AVG(json_extract(payload,'$.scores.no_news.score')) AS b1,"
        "AVG(json_extract(payload,'$.scores.with_news.score')) AS candidate,"
        "COUNT(json_extract(payload,'$.scores.uncalibrated_player_scenario.score')) AS scenarios,"
        "AVG(json_extract(payload,'$.scores.uncalibrated_player_scenario.score')) AS scenario_mean,"
        "AVG(CASE WHEN json_type(payload,'$.scores.uncalibrated_player_scenario.score') IS NOT NULL THEN "
        "json_extract(payload,'$.scores.no_news.score') END) AS comparable_base "
        "FROM current WHERE rank=1 AND json_type(payload,'$.score')='array' GROUP BY horizon", args)
    for row in aggregate:
        report["scored_samples_by_horizon"][row["horizon"]] = row["matches"]
        report["match_weighted_scores"][row["horizon"]] = {
            "fixtures": row["matches"], "mean_no_news": row["b1"], "mean_with_news": row["candidate"],
            "scenario_fixtures": row["scenarios"], "mean_uncalibrated_scenario": row["scenario_mean"],
            "mean_no_news_on_scenario_fixtures": row["comparable_base"]}
    if fixture_id:
        report["fixture_evaluation"] = [
            {"fixture": row["fixture"], "kickoff": row["kickoff"], "horizon": row["horizon"],
             "revision": row["revision"], **json.loads(row["payload"])}
            for row in db.execute(cte + "SELECT * FROM current WHERE rank=1 AND json_type(payload,'$.score')='array' "
                                   "ORDER BY horizon", args)]
        report["fixture_evidence_changes"] = []
    if "domestic_evidence_changes" in tables:
        where_fixture = " WHERE fixture=?" if fixture_id else ""
        counts = db.execute("SELECT COUNT(*),COUNT(CASE WHEN disposition='evidence_only' THEN 1 END) "
                            "FROM domestic_evidence_changes" + where_fixture, args).fetchone()
        report.update(evidence_changes=counts[0], evidence_only_changes=counts[1])
        if fixture_id:
            for row in db.execute("SELECT * FROM domestic_evidence_changes WHERE fixture=? ORDER BY id", args):
                item = dict(row)
                for field in ("sources", "changed_sources", "changed_outputs"):
                    item[field] = json.loads(item[field])
                report["fixture_evidence_changes"].append(item)
    return report
