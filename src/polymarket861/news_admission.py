"""Local preregistration and numerical review of a source field or confirmed-absence blind set."""

import hashlib
import json
import math
import re
from collections import defaultdict
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from statistics import NormalDist

from . import league_registry, store


LANGUAGES = {"epl": "en", "lal": "es", "bun": "de", "sea": "it", "fl1": "fr"}
SCHEMA = """CREATE TABLE IF NOT EXISTS news_admission_plans(
    id TEXT PRIMARY KEY, recorded_at TEXT NOT NULL, document TEXT NOT NULL)"""


def _sha(value):
    if not isinstance(value, str) or not re.fullmatch("[0-9a-f]{64}", value):
        raise ValueError("expected a SHA-256 evidence identifier")
    return value


def _label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise ValueError("missing or invalid case identifier")
    return value


def _map(rows):
    if not isinstance(rows, list) or len(rows) > 2000:
        raise ValueError("a bounded list of cases is required")
    indexed = {}
    for row in rows:
        if not isinstance(row, dict) or (key := _label(row.get("id"))) in indexed:
            raise ValueError("cases must have unique identifiers")
        indexed[key] = row
    return indexed


def _cohort(plan):
    league, season = plan["league"], plan["season"]
    league_registry.profile(league)
    league_registry.season_start_year(season)
    opening = store.iso(plan["opens_at"])
    samples = _map(plan["samples"])
    if plan["kind"] == "confirmed_absence":
        if (plan["language"] != LANGUAGES[league] or len(samples) != 84
                or {_label(v) for v in plan["training_clusters"]} & samples.keys()):
            raise ValueError("blind cohort requires exactly 84 distinct held-out confirmation clusters in one language")
        _sha(plan["model_sha256"])
        raters = plan["reviewers"]
        if not isinstance(raters, list) or len(raters) != 3 or len({_label(r) for r in raters}) != 3:
            raise ValueError("two independent annotators and one adjudicator required")
        for sample in samples.values():
            _sha(sample["confirmed_prediction_sha256"])
    elif plan["kind"] == "source_field":
        _label(plan["source"])
        _label(plan["field"])
        _label(plan["independent_reviewer"])
        if type(plan.get("horizon_minutes")) is not int or plan["horizon_minutes"] not in (60,1440):
            raise ValueError("source opportunity requires a registered 60 or 1440 minute horizon")
        count = league_registry.team_count(league, season)
        teams = {league_registry.club_key(league, season, n) for n in league_registry.members(league, season)}
        by_round = defaultdict(set)
        for fid, sample in samples.items():
            parts = fid.split("/")
            rnd = sample["round"]
            if (type(rnd) is not int or not 1 <= rnd <= league_registry.round_count(league, season)
                    or len(parts) != 4 or parts[:2] != [league, season] or parts[2] == parts[3]
                    or not set(parts[2:]) <= teams or by_round[rnd].intersection(parts[2:])
                    or store.iso(sample["deadline"]) < opening):
                raise ValueError("fixture identity, round or deadline conflicts with the registered population")
            by_round[rnd].update(parts[2:])
        if not samples or any(len(clubs) != count for clubs in by_round.values()):
            raise ValueError("submit all fixtures in each registered regular-season round")
    else:
        raise ValueError("unknown news admission protocol")
    return samples


def _bytes(root, directory, ident):
    ident = _sha(ident)
    try:
        if directory is None:
            raw = store.read_blob(root, ident)
        else:
            folder = Path(directory)
            candidates = [folder / ident, *sorted(folder.glob(ident + ".*"))]
            path = next((p for p in candidates if p.is_file()), None)
            if path is None:
                raise FileNotFoundError(ident)
            raw = path.read_bytes()
    except (ValueError, OSError) as exc:
        raise ValueError(f"missing news admission evidence {ident}") from exc
    if hashlib.sha256(raw).hexdigest() != ident:
        raise ValueError("news admission evidence SHA-256 mismatch")
    return raw


def _proof(root, directory, ident, expected):
    raw = _bytes(root, directory, ident)
    record = json.loads(raw)
    if not isinstance(record, dict) or any(record.get(k) != v for k, v in expected.items()):
        raise ValueError("news admission evidence has the wrong case, field or competition")
    return record


BLIND_LABELS = """CREATE TABLE IF NOT EXISTS news_admission_blind_labels(
    registration TEXT NOT NULL, cluster TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
    PRIMARY KEY(registration, cluster))"""


def _freeze_labels(root, registration, pointers):
    with closing(store.connect(root)) as db, db:
        db.execute(BLIND_LABELS)
        db.execute("BEGIN IMMEDIATE")
        known = dict(db.execute("SELECT cluster,evidence_sha256 FROM news_admission_blind_labels WHERE registration=?",
                                (registration,)))
        for cluster, item in pointers.items():
            ident = item["evidence_sha256"]
            if cluster in known and ident != known[cluster]:
                raise ValueError("independent blind decision already sealed under a different receipt")
            db.execute("INSERT OR IGNORE INTO news_admission_blind_labels VALUES(?,?,?)", (registration,cluster,ident))


def register(root, plan, directory=None):
    _cohort(plan)
    ident = store.digest(plan)
    with closing(store.connect(root)) as db, db:
        db.execute(SCHEMA)
        db.execute(BLIND_LABELS)
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT recorded_at FROM news_admission_plans WHERE id=?", (ident,)).fetchone()
        now = store.iso(store.now_utc())
        if not old:
            if now >= store.iso(plan["opens_at"]):
                raise ValueError("local preregistration must precede the cohort opening")
            if plan["kind"] == "source_field":
                for sample in plan["samples"]:
                    fx = db.execute("""SELECT f.* FROM fixture_versions f JOIN schedule_receipts r
                        USING(fixture_id,version) WHERE f.fixture_id=? AND f.season=?
                        AND f.known_at<=? AND r.recorded_at<=? AND r.precision!='date'
                        ORDER BY f.version DESC LIMIT 1""", (sample["id"],plan["season"],now,now)).fetchone()
                    if (not fx or fx["round_id"] != sample["round"] or
                            sample["id"].split("/")[2:] != [league_registry.club_key(plan["league"], plan["season"], fx[k])
                                                             for k in ("home","away")] or
                            store.instant(fx["kickoff"]) - timedelta(minutes=plan["horizon_minutes"]) !=
                            store.instant(sample["deadline"])):
                        raise ValueError("registered source opportunity disagrees with locally received fixture or cutoff")
            if plan["kind"] == "confirmed_absence":
                disclosed = {r[0] for r in db.execute("SELECT DISTINCT cluster FROM news_admission_blind_labels")}
                if disclosed & {s["id"] for s in plan["samples"]}:
                    raise ValueError("previously unblinded clusters require a fresh independent holdout")
                for sample in plan["samples"]:
                    proof = _proof(root, directory, sample["confirmed_prediction_sha256"],
                                   {"kind": "system_confirmed_absence", "id": sample["id"], "language": plan["language"],
                                    "model_sha256": plan["model_sha256"], "league": plan["league"], "season": plan["season"]})
                    if store.iso(proof["predicted_at"]) > now:
                        raise ValueError("frozen system confirmation is dated after enrollment")
            db.execute("INSERT INTO news_admission_plans VALUES(?,?,?)", (ident, now, store.canonical(plan)))
        return {"registration": ident, "recorded_at": old[0] if old else now, "cases": len(plan["samples"])}


def _wilson_lower(success, n):
    z = NormalDist().inv_cdf(0.975)
    p = success / n
    return (p + z*z/(2*n) - z*math.sqrt(p*(1-p)/n + z*z/(4*n*n))) / (1 + z*z/n)


def audit(root, registration, review, directory=None):
    """Review operator-supplied adjudications; the returned status cannot enable production."""
    with closing(store.connect(root)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_admission_plans'").fetchone():
            raise ValueError("unknown local news admission registration")
        row = db.execute("SELECT * FROM news_admission_plans WHERE id=?", (_sha(registration),)).fetchone()
    if not row or review.get("registration") != registration:
        raise ValueError("review does not match a local preregistration")
    plan = json.loads(row["document"])
    if store.digest(plan) != registration:
        raise ValueError("registered plan SHA-256 mismatch")
    samples, pointers = _cohort(plan), _map(review["decisions"])
    scope = {"registration": registration, "league": plan["league"], "season": plan["season"]}
    if plan["kind"] == "source_field":
        scope.update(source=plan["source"], field=plan["field"])
    else:
        scope.update(language=plan["language"], model_sha256=plan["model_sha256"])
    decisions = {key: _proof(root, directory, item["evidence_sha256"], scope | {"id": key, "kind": plan["kind"]})
                 for key, item in pointers.items()}
    now, opening = store.iso(store.now_utc()), store.iso(plan["opens_at"])
    if decisions.keys() - samples.keys() or not row["recorded_at"] < opening:
        raise ValueError("unregistered case or invalid local registration clock")
    for decision in decisions.values():
        if not opening <= store.iso(decision["reviewed_at"]) <= now:
            raise ValueError("review outside its observation period")
    result = {"kind": plan["kind"], "league": plan["league"], "season": plan["season"],
              "registration": registration, "registered_at": row["recorded_at"], "as_of": now,
              "review_sha256": store.digest(review), "registered_cases": len(samples),
              "reviewed_cases": len(decisions), "production_enabled": False,
              "evidence_basis": "Local prospective plan and operator-supplied independent adjudications."}
    if plan["kind"] == "confirmed_absence":
        correct = 0
        for decision in decisions.values():
            labels = decision["labels"]
            if (not isinstance(labels, dict) or set(labels) != set(plan["reviewers"][:2])
                    or any(type(v) is not bool for v in labels.values())):
                raise ValueError("two registered independent Boolean labels required")
            a, b = (labels[k] for k in plan["reviewers"][:2])
            if a != b:
                if decision.get("adjudicator") != plan["reviewers"][2] or type(decision.get("adjudicated")) is not bool:
                    raise ValueError("disagreement needs a third registered adjudicator")
                a = decision["adjudicated"]
            correct += a
        _freeze_labels(root, registration, pointers)
        n = len(decisions)
        if n < 84:
            return result | {"language":plan["language"], "status":"awaiting_fixed_84"}
        lower = _wilson_lower(correct, n)
        gate = "precision_met" if correct >= 81 and lower >= 0.90 else "precision_failed"
        return result | {"language": plan["language"], "correct": correct, "errors": n-correct,
                         "wilson_lower_95": lower, "status": gate}

    matured = sorted((s for s in samples.values() if store.iso(s["deadline"]) <= now),
                     key=lambda s: (store.iso(s["deadline"]), s["id"]))
    if any(store.iso(samples[key]["deadline"]) > now for key in decisions):
        raise ValueError("source field review precedes its registered decision deadline")
    timely = {}
    for sample in matured:
        decision = decisions.get(sample["id"])
        timely[sample["id"]] = False
        if decision:
            if (type(decision.get("valid")) is not bool or decision.get("reviewer") != plan["independent_reviewer"]):
                raise ValueError("source field needs a verdict by its registered independent reviewer")
            if decision.get("receipt_sha256") is not None:
                native = _proof(root, directory, decision["receipt_sha256"],
                                {k: v for k,v in scope.items() if k != "registration"} |
                                {"id":sample["id"], "kind":"source_receipt"})
                if not _bytes(root, directory, native.get("response_sha256")):
                    raise ValueError("a credited source receipt requires a nonempty upstream response")
                if not isinstance(native.get("received_at"), str):
                    raise ValueError("a source receipt requires its actual received_at time")
                received = store.iso(native["received_at"])
                if not opening <= received <= store.iso(decision["reviewed_at"]):
                    raise ValueError("invalid source receipt or review clock")
                timely[sample["id"]] = decision["valid"] and received <= store.iso(sample["deadline"])
    finished = {s["round"] for s in matured} - {s["round"] for s in samples.values() if s["id"] not in timely}
    window = matured[-100:]
    n, good = len(window), sum(timely[s["id"]] for s in window)
    check = (_proof(root, directory, review["semantic_check"], scope | {"kind":"source_field_semantics"})
             if review.get("semantic_check") else None)
    if check and (not isinstance(check.get("cases"), list) or not isinstance(check.get("reviewer"), str)
                  or check["reviewer"] != plan["independent_reviewer"]):
        raise ValueError("an identified independent semantic reviewer and a sample are required")
    sampled = set(check["cases"]) if check else set()
    if check:
        if (not sampled or not sampled <= decisions.keys() or type(check.get("passed")) is not bool
                or not opening <= store.iso(check["reviewed_at"]) <= now):
            raise ValueError("invalid independently sampled semantic check")
    semantic = bool(check and check["passed"] and sampled <= {s["id"] for s in window})
    if len(finished) < 2 or n < 20:
        gate = "awaiting_rounds_or_cases"
    elif n < 100:
        gate = "provisional_shadow" if good/n >= 0.95 else "shadow_only"
    else:
        gate = "confirmation_criteria_met" if good >= 91 and semantic else "shadow_only"
    return result | {"source": plan["source"], "field": plan["field"], "complete_rounds": sorted(finished),
                     "matured": len(matured), "window_cases": n, "on_time_and_valid": good,
                     "missing_reviews": len(matured)-len(decisions), "semantic_check_in_window": semantic, "status": gate}


def add_commands(commands):
    enroll = commands.add_parser("admission-register", help="在来源观察或盲标开始前冻结样本")
    enroll.add_argument("--plan", type=Path, required=True)
    enroll.add_argument("--evidence-dir", type=Path, help="原件按 SHA-256.json 命名，默认为本机归档")
    enroll.set_defaults(func=lambda a: register(a.data_dir, json.loads(a.plan.read_bytes()), a.evidence_dir))
    check = commands.add_parser("admission-audit", help="复算来源及时率或固定 84 簇确认缺阵精度")
    check.add_argument("--registration", required=True)
    check.add_argument("--reviews", type=Path, required=True)
    check.add_argument("--evidence-dir", type=Path, help="原件按 SHA-256.json 命名，默认为本机归档")
    check.set_defaults(func=lambda a: audit(a.data_dir, a.registration, json.loads(a.reviews.read_bytes()), a.evidence_dir))
