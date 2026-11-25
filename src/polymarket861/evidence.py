"""Point-in-time match evidence, independent source comparisons and root-cause repair."""

import json
import math
from pathlib import Path

from .evidence_sources import (
    FIELDS, METHODS, USES, coverage_gaps, db, digest, discover_sources, dump, granted, list_sources, name_key, publish_source,
    observe_source, register_source, required, source_at, source_report, utc,
)
from .store import now_utc, save_report


def _lineup_key(player):
    if isinstance(player, dict):
        player = player.get("player_id") or player.get("id") or player.get("name")
    return name_key(required(player, "starter identifier"))


def _number(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def _joint_estimate(estimate):
    if not isinstance(estimate, dict) or not _number(estimate.get("probability")) or not 0 <= estimate["probability"] <= 1:
        raise ValueError("match estimate needs a finite scenario probability in [0,1]")
    result = {"probability": estimate["probability"]}
    for side in ("home", "away"):
        row = estimate.get(side)
        if not isinstance(row, dict):
            raise ValueError("match estimate needs both team sides")
        factors = {}
        for key in ("attack_multiplier", "defense_multiplier"):
            if not _number(row.get(key)) or row[key] <= 0:
                raise ValueError("match estimate needs both positive finite team multipliers")
            factors[key] = row[key]
        result[side] = factors
    return result


def _check_value(field, value, rule, published):
    if not isinstance(value, dict) or not value:
        raise ValueError("an active evidence record needs a nonempty value object")
    if field == "lineup":
        if not isinstance(value.get("confirmed"), bool):
            raise ValueError("lineup.confirmed must explicitly be true or false")
        starters = value.get("starters", [])
        if not isinstance(starters, list):
            raise ValueError("lineup.starters must be a list")
        players = {_lineup_key(p) for p in starters}
        if len(players) != len(starters) or (value["confirmed"] and len(players) != 11):
            raise ValueError("a confirmed lineup requires 11 distinct starters")
    if field == "suspension":
        required(value.get("competition"), "suspension.competition")
    if field == "card":
        for key in ("yellow", "red"):
            if key in value and (type(value[key]) is not int or value[key] < 0):
                raise ValueError(f"card.{key} must be a nonnegative count")
    if field == "weather":
        run, target = utc(value.get("run_at")), utc(value.get("forecast_for"))
        if run > published or target < run:
            raise ValueError("weather forecast precedes its model run")
    if field == "impact_validation":
        if value.get("applies_to") not in FIELDS - {"impact_validation"}:
            raise ValueError("impact validation must name the evidence field it covers")
        if type(value.get("holdout_games")) is not int or value["holdout_games"] < 1:
            raise ValueError("supply the positive number of out-of-sample fixtures in the validation")
        required(value.get("evaluation_locator"), "out-of-sample evaluation locator")
        _joint_estimate(value.get("estimate"))
    if "match_impact" in value:
        estimate = value["match_impact"]
        if not rule.get("allow_match_impacts") or field == "impact_validation" or "impact" in value:
            raise ValueError("field has no applicable permission for a shared match estimate")
        _joint_estimate(estimate)
        for key in ("basis", "uncertainty", "validation_evidence_id", "scenario_id"):
            required(estimate.get(key), "match_impact." + key)
    if "impact" in value:
        impact = value["impact"]
        if not rule.get("allow_impacts") or not isinstance(impact, dict):
            raise ValueError("field has no permission for impact estimates")
        for key in ("attack_multiplier", "defense_multiplier"):
            if not _number(impact.get(key)) or impact[key] <= 0:
                raise ValueError(f"impact.{key} must be a positive finite estimate")
        if not _number(impact.get("probability")) or not 0 <= impact["probability"] <= 1:
            raise ValueError("impact.probability must be in [0,1]")
        if (value.get("scenario_confirmed") is True or field == "lineup" and value["confirmed"]) and impact["probability"] != 1:
            raise ValueError("a confirmed personnel scenario must have probability 1")
        required(impact.get("basis"), "impact.basis (estimation method or source)")
        required(impact.get("uncertainty"), "impact.uncertainty")


def _guard_rule(raw):
    if not isinstance(raw, dict):
        raise ValueError("guard must be a source field validation rule")
    rule = {k: required(raw.get(k), k) for k in ("source_id", "field", "path")}
    if rule["field"] not in FIELDS or not all(p.isidentifier() for p in rule["path"].split(".")):
        raise ValueError("guard must address a supported field and a path within value")
    if "allowed" in raw:
        if not isinstance(raw["allowed"], list) or not raw["allowed"] or "min" in raw or "max" in raw:
            raise ValueError("guard.allowed needs a nonempty list and no range")
        rule["allowed"] = raw["allowed"]
    else:
        for limit in ("min", "max"):
            if limit in raw:
                if not _number(raw[limit]):
                    raise ValueError("guard range must be finite")
                rule[limit] = raw[limit]
        if not ({"min", "max"} & rule.keys()) or rule.get("min", -math.inf) > rule.get("max", math.inf):
            raise ValueError("guard needs ordered numerical limits or allowed values")
    return rule


def _passes_guard(value, guard):
    for key in guard["path"].split("."):
        if not isinstance(value, dict) or key not in value:
            return False
        value = value[key]
    if "allowed" in guard:
        return any(dump(value) == dump(item) for item in guard["allowed"])
    return _number(value) and guard.get("min", -math.inf) <= value <= guard.get("max", math.inf)


def _ingest(conn, raw):
    source_id = required(raw.get("source_id"), "source_id")
    first_recorded = utc(now_utc())
    source = source_at(conn, source_id, first_recorded)
    if not source:
        raise ValueError(f"no registered source currently in force: {source_id}")
    field = required(raw.get("field"), "field")
    method = required(raw.get("method"), "collection method")
    if field not in FIELDS or method not in METHODS:
        raise ValueError("unrecognised field or collection method")
    published, received = utc(raw.get("published_at")), utc(raw.get("received_at", now_utc()))
    uses = granted(source, field, method, conn=conn, at=first_recorded)
    if source["status"] == "candidate":
        uses &= granted(source, field, method, conn=conn, at=received)
    if raw.get("trial_only") is True:
        uses &= {"trial"}
    if not uses:
        raise ValueError(f"{source_id} has no field permission for {field}/{method}")
    fixture = required(raw.get("fixture_id"), "fixture_id")
    team = raw.get("team", "")
    if not isinstance(team, str) or (field == "lineup" and not team.strip()):
        raise ValueError("provide a team name for lineup evidence")
    subject = raw.get("subject", "")
    if not isinstance(subject, str):
        raise ValueError("subject must be a stable string")
    if field in {"injury", "availability", "suspension", "card", "form", "minutes", "player_stats"}:
        required(subject, "player or group subject")
    origin = required(raw.get("origin_id", source["upstream"]), "origin_id")
    claim = required(raw.get("claim_key") if field == "news" else raw.get("claim_key", subject or field),
                     "claim_key identifying the original story or upstream assertion")
    revision = raw.get("revision", 1)
    if type(revision) is not int or revision < 1:
        raise ValueError("revision must be a positive integer")
    state = raw.get("state", "active")
    if state not in {"active", "withdrawn"}:
        raise ValueError("state must be active or withdrawn")
    valid_from = utc(raw.get("valid_from", published))
    valid_until = utc(raw["valid_until"]) if raw.get("valid_until") else None
    if valid_until and valid_until <= valid_from:
        raise ValueError("valid_until must follow valid_from")
    value = raw.get("value", {})
    if state == "active":
        _check_value(field, value, source["fields"][field], published)
        if "match_impact" in value:
            if team.strip():
                raise ValueError("shared match estimates belong to fixture-level evidence without a team")
            witness = conn.execute("SELECT field FROM ev_facts WHERE evidence_id=?",
                                   (value["match_impact"]["validation_evidence_id"],)).fetchone()
            if not witness or witness[0] != "impact_validation":
                raise ValueError("match estimate needs a registered validation evidence record")
        if "impact" in value and not team.strip():
            raise ValueError("a one-team impact must identify its team")
        for guard in conn.execute("SELECT incident_id,document FROM ev_guards WHERE source_id=? AND field=?",
                                  (source_id, field)):
            if not _passes_guard(value, json.loads(guard["document"])):
                raise ValueError(f"evidence failed regression guard from incident {guard['incident_id']}")
    elif value:
        raise ValueError("a withdrawal must not carry a replacement value")
    locator = required(raw.get("locator"), "locator")
    identity = (source_id, fixture, name_key(team), field, subject, origin, claim, revision)
    evidence_id = required(raw.get("evidence_id", "ev-" + digest(identity)), "evidence_id")
    fact = dict(evidence_id=evidence_id, source_id=source_id, fixture_id=fixture, team=team.strip(),
                field=field, subject=subject, origin_id=origin, claim_key=claim, revision=revision,
                state=state, published_at=published, received_at=received, first_recorded_at=first_recorded,
                valid_from=valid_from,
                valid_until=valid_until, method=method, locator=locator, value=value,
                source_version=source["effective_at"], capture_uses=sorted(uses),
                upstream=source["upstream"])
    fact["content_hash"] = digest({key:fact[key] for key in (
        "source_id", "fixture_id", "field", "subject", "origin_id", "claim_key", "team",
        "revision", "state", "published_at", "valid_from", "valid_until", "locator", "value", "method",
    )})
    prev = conn.execute(
        """SELECT document FROM ev_facts WHERE evidence_id=? OR
           (source_id,fixture_id,team_key,field,subject,origin_id,claim_key,revision)=(?,?,?,?,?,?,?,?)""",
        (evidence_id, *identity),
    ).fetchall()
    if prev:
        old = json.loads(prev[0][0])
        if len(prev) == 1 and old["evidence_id"] == evidence_id and old["content_hash"] == fact["content_hash"]:
            return old
        raise ValueError("conflicting immutable evidence id/revision; publish a new revision")
    conn.execute("INSERT INTO ev_facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (evidence_id, *identity, published, received, valid_from, valid_until, dump(fact)))
    return fact


def import_evidence(data_dir, records):
    """Atomically import a batch. Manual records require the same field grants as adapters."""
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list) or not records:
        raise ValueError("evidence import expects a record or a nonempty list")
    with db(data_dir, write=True) as conn:
        facts = [_ingest(conn, row) for row in records]
    for source in sorted({row["source_id"] for row in facts}):
        publish_source(data_dir, source)
    return {"evidence_ids":[r["evidence_id"] for r in facts], "count":len(facts)}


def get_evidence(data_dir, evidence_id):
    with db(data_dir) as conn:
        row = conn.execute("SELECT document FROM ev_facts WHERE evidence_id=?", (evidence_id,)).fetchone()
        if not row:
            raise ValueError(f"unknown evidence: {evidence_id}")
        return json.loads(row[0])


def _collect(conn, fixture, cutoff, purpose, *, source_id=None, field=None, teams=None,
             subject=None, prospective=False):
    if purpose not in USES:
        raise ValueError("purpose must be trial, prediction or training")
    query = """SELECT f.*, EXISTS(SELECT 1 FROM ev_quarantine q WHERE q.evidence_id=f.evidence_id
               AND q.since<=? AND (?=0 OR EXISTS(SELECT 1 FROM ev_incident_versions iv
               WHERE iv.incident_id=q.incident_id AND iv.updated_at=q.since AND
               json_extract(iv.document,'$.quarantine_recorded_at')<=?))) AS quarantined
               FROM ev_facts f WHERE fixture_id=?
               AND received_at<=? AND published_at<=? AND valid_from<=?"""
    params = [cutoff, int(prospective), cutoff, fixture, cutoff, cutoff, cutoff]
    for column, val in (("source_id", source_id), ("field", field), ("subject", subject)):
        if val is not None:
            query += f" AND {column}=?"
            params.append(val)
    if teams:
        query += " AND team_key IN (" + ",".join("?" for _ in teams) + ")"
        params.extend(teams)
    versions = {}
    manifests = {}
    current_time = utc(now_utc())
    for row in conn.execute(query, params):
        fact = json.loads(row["document"])
        if prospective and (not fact.get("first_recorded_at") or fact["first_recorded_at"] > cutoff):
            continue
        source = fact["source_id"]
        if source not in manifests:
            manifests[source] = (source_at(conn, source, cutoff, recorded_by=cutoff if prospective else None),
                                 source_at(conn, source, current_time))
        at_cutoff, current = manifests[source]
        if purpose not in fact["capture_uses"] or purpose not in granted(at_cutoff, fact["field"], fact["method"], conn=conn, at=cutoff):
            continue
        if purpose == "training" and purpose not in granted(current, fact["field"], fact["method"], conn=conn, at=current_time):
            continue
        fact["impact_permitted"] = at_cutoff["fields"][fact["field"]].get("allow_impacts", False)
        fact["match_impact_permitted"] = at_cutoff["fields"][fact["field"]].get("allow_match_impacts", False)
        if purpose == "training":
            fact["impact_permitted"] &= current["fields"][fact["field"]].get("allow_impacts", False)
            fact["match_impact_permitted"] &= current["fields"][fact["field"]].get("allow_match_impacts", False)
        fact["quarantined"] = bool(row["quarantined"])
        key = (row["team_key"], fact["field"], fact["subject"], fact["origin_id"], fact["claim_key"])
        versions.setdefault(key, []).append(fact)
    active, changes, conflicts = [], [], []
    for key, group in sorted(versions.items()):
        revision = max(r["revision"] for r in group)
        latest = sorted((r for r in group if r["revision"] == revision),
                        key=lambda r:(r["received_at"], r["evidence_id"]))
        ids = sorted(r["evidence_id"] for r in latest)
        prior_ids = sorted(r["evidence_id"] for r in group if r["revision"] < revision)
        feed_observation = any(r["value"].get("observation_hash") for r in latest)
        if prior_ids or (revision > 1 and not feed_observation) or any(r["state"] == "withdrawn" or r["quarantined"] for r in latest):
            changes.append({"evidence_ids":ids, "revision":revision, "claim_key":key[-1],
                            "states":sorted({"quarantined" if r["quarantined"] else r["state"] for r in latest}),
                            "superseded_ids":prior_ids})
        # A retraction of the originating assertion invalidates its syndicated copies as well.
        if any(r["state"] == "withdrawn" or r["quarantined"] for r in latest):
            continue
        rows = [r for r in latest if not r["valid_until"] or cutoff < r["valid_until"]]
        if not rows:
            continue
        same_index_link = key[1] == "news" and all(
            r["value"].get("evidence_class") == "news_index_lead" for r in rows
        ) and len({r["value"].get("url") for r in rows}) == 1
        values = []
        for fact in rows:
            value = fact["value"]
            if key[1] == "lineup":
                value = dict(value, starters=sorted(_lineup_key(p) for p in value.get("starters", [])))
            values.append(dump(value))
        if not same_index_link and len(set(values)) > 1:
            conflicts.append({"reason":"same_origin_conflict", "evidence_ids":ids, "claim_key":key[-1]})
            continue
        active.append(dict(rows[0], corroborating_ids=[r["evidence_id"] for r in rows[1:]]))
    return active, changes, conflicts


def _selected_lineup(facts, primary=None):
    rows = [r for r in facts if r["field"] == "lineup"]
    confirmed = [r for r in rows if r["value"]["confirmed"]]
    if len({tuple(sorted(_lineup_key(p) for p in r["value"]["starters"])) for r in confirmed}) > 1:
        return None, {"reason":"conflicting_confirmed_lineups",
                      "evidence_ids":sorted(r["evidence_id"] for r in confirmed)}
    if not rows:
        return None, None
    candidates = confirmed or rows
    preferred = [r for r in candidates if r["source_id"] == primary]
    return max(preferred or candidates, key=lambda r:(r["published_at"],r["received_at"],r["evidence_id"])), None


def _prefer_sources(data_dir, facts, cutoff):
    """Choose the promoted field source only for competing versions of one fact."""
    from .jobs import active_version

    priorities = {}
    for field in sorted({f["field"] for f in facts}):
        primary = active_version(data_dir, "source", field, at=cutoff)
        if primary is not None:
            priorities[field] = primary
    if not priorities:
        return facts, priorities, []
    grouped = {}
    for fact in facts:
        slot = (name_key(fact["team"]), fact["field"], fact["subject"], fact["claim_key"])
        grouped.setdefault(slot, []).append(fact)
    selected, alternatives = [], []
    for (_, field, _, _), group in grouped.items():
        preferred = [f for f in group if f["source_id"] == priorities.get(field)]
        # Independent news stories remain visible and conflicting official team sheets
        # must still be detected even when a field primary has been designated.
        if preferred and field not in {"lineup", "news"}:
            selected.extend(preferred)
            alternatives.extend(f["evidence_id"] for f in group if f not in preferred)
        else:
            selected.extend(group)
    return selected, priorities, sorted(alternatives)


def _evidence_ids(facts):
    return sorted({ident for fact in facts for ident in [fact["evidence_id"], *fact["corroborating_ids"]]})


def _match_impacts(data_dir, facts, cutoff, purpose, prospective):
    candidates = [f for f in facts if not f["team"] and f["match_impact_permitted"] and "match_impact" in f["value"]]
    selected, excluded, scenarios = [], [], {}
    if not candidates:
        return selected, excluded
    with db(data_dir) as conn:
        for fact in candidates:
            estimate = fact["value"]["match_impact"]
            ident = estimate["validation_evidence_id"]
            raw = conn.execute("SELECT document FROM ev_facts WHERE evidence_id=?", (ident,)).fetchone()
            witness = json.loads(raw[0]) if raw else None
            verified = None
            if witness and witness["origin_id"] != fact["origin_id"] and witness["upstream"] != fact["upstream"]:
                rows, _, conflicts = _collect(conn, witness["fixture_id"], cutoff, purpose,
                    source_id=witness["source_id"], field="impact_validation", teams=(name_key(witness["team"]),),
                    subject=witness["subject"], prospective=prospective)
                if not conflicts:
                    verified = next((r for r in rows if r["evidence_id"] == ident or ident in r["corroborating_ids"]), None)
            if (not verified or verified["value"]["applies_to"] != fact["field"] or
                    _joint_estimate(verified["value"]["estimate"]) != _joint_estimate(estimate)):
                excluded.append({"reason":"independent_match_estimate_validation_unavailable",
                                 "evidence_ids":[fact["evidence_id"], ident]})
                continue
            item = dict(estimate, evidence_id=fact["evidence_id"], experimental=True,
                        validation_locator=verified["value"]["evaluation_locator"])
            scenarios.setdefault(estimate["scenario_id"], []).append(item)
    for scenario, items in scenarios.items():
        if len(items) == 1:
            selected.extend(items)
        else:
            excluded.append({"reason":"overlapping_match_impact_estimates","scenario_id":scenario,
                             "evidence_ids":sorted(r["evidence_id"] for r in items)})
    return selected, excluded


def as_of_context(data_dir, fixture_id, home, away, cutoff, *, purpose="prediction", prospective=False):
    cutoff = utc(cutoff)
    if not name_key(home) or not name_key(away) or name_key(home) == name_key(away):
        raise ValueError("home and away must identify distinct teams")
    with db(data_dir) as conn:
        facts, changes, conflicts = _collect(conn, fixture_id, cutoff, purpose,
                                             teams=("", name_key(home), name_key(away)),
                                             prospective=prospective)
    priorities, alternatives = {}, []
    if purpose != "trial":
        facts, priorities, alternatives = _prefer_sources(data_dir, facts, cutoff)
    matches, exclusions = _match_impacts(data_dir, facts, cutoff, purpose, prospective)
    teams = {}
    for side, name in (("home", home),("away", away)):
        team_facts = [f for f in facts if name_key(f["team"]) == name_key(name)]
        lineup, issue = _selected_lineup(team_facts, priorities.get("lineup"))
        if issue:
            conflicts.append(issue)
        impact_candidates = {}
        for f in team_facts:
            if f["field"] == "lineup" and (lineup is None or f["evidence_id"] != lineup["evidence_id"]):
                continue
            impact = f["value"].get("impact")
            if f["impact_permitted"] and impact:
                scenario = impact.get("scenario_id") or f["subject"] or f["claim_key"]
                impact_candidates.setdefault(scenario, []).append((f, impact))
        impacts = []
        for scenario, estimates in sorted(impact_candidates.items()):
            if len(estimates) > 1:
                conflicts.append({"reason":"overlapping_impact_estimates", "scenario_id":scenario,
                                  "evidence_ids":sorted(f["evidence_id"] for f,_ in estimates)})
                continue
            f, impact = estimates[0]
            impacts.append(dict(impact, evidence_id=f["evidence_id"], experimental=True))
        teams[side] = {"name":name, "confirmed":bool(lineup and lineup["value"]["confirmed"]),
                       "lineup":dict(lineup["value"], evidence_id=lineup["evidence_id"]) if lineup else None,
                       "evidence_ids":_evidence_ids(team_facts), "impacts":impacts}
    return {"fixture_id":fixture_id, "cutoff":cutoff, "purpose":purpose, "teams":teams,
            "confirmed":{side:v["confirmed"] for side,v in teams.items()},
            "evidence_ids":sorted(set(_evidence_ids(facts)) | {e["validation_evidence_id"] for e in matches}),
            "facts":facts, "prospective":prospective, "match_impacts":matches, "impact_exclusions":exclusions,
            "source_priority":priorities, "alternative_evidence_ids":alternatives,
            "corrections":changes, "conflicts":conflicts}


def _single_source(conn, source_id, fixture_id, team, field, cutoff, subject, purpose, prospective=False):
    rows, _, conflicts = _collect(conn, fixture_id, cutoff, purpose, source_id=source_id, field=field,
                                  teams=(name_key(team),), subject=subject, prospective=prospective)
    if field == "lineup":
        best, issue = _selected_lineup(rows)
        return (best if best and best["value"]["confirmed"] else None), bool(issue or conflicts)
    if len(rows) > 1 and len({dump(r["value"]) for r in rows}) > 1:
        return None, True
    return (rows[-1] if rows else None), bool(conflicts)


def source_reference(data_dir, source_id, fixture_id, team, field, *, cutoff, subject=None,
                     purpose="prediction", prospective=False):
    with db(data_dir) as conn:
        selected, conflict = _single_source(conn, source_id, fixture_id, team, field, utc(cutoff),
                                            subject, purpose, prospective=prospective)
        return None if conflict else selected


def _matches_expected(observed, expected):
    if not isinstance(expected, dict) or not expected:
        return False
    for key, target in expected.items():
        actual = observed.get(key)
        if key == "starters" and isinstance(actual, list) and isinstance(target, list):
            if sorted(map(_lineup_key, actual)) != sorted(map(_lineup_key, target)):
                return False
        elif actual != target:
            return False
    return True


def source_comparison(data_dir, source_ids, field, observations=None, *, cutoff=None):
    """Evaluate an independently enumerated fixture/team/subject schedule, counting absent rows."""
    if field not in FIELDS or len(set(source_ids)) != len(source_ids) or not source_ids:
        raise ValueError("provide a supported field and distinct source ids")
    supplied = observations is not None
    rows = []
    summary = {s:{"on_time":0,"verified":0,"correct":0,"conflicts":0,"missing":0,"evidence_ids":[]} for s in source_ids}
    seen, fixtures, rounds = set(), set(), set()
    overlap = disagreements = 0
    with db(data_dir) as conn:
        for s in source_ids:
            if not source_at(conn,s):
                raise ValueError(f"unknown source: {s}")
        for obs in observations or []:
            fid = required(obs.get("fixture_id"), "fixture_id")
            team, subject = obs.get("team",""), obs.get("subject")
            at = utc(obs.get("cutoff",cutoff))
            key = (fid, name_key(team),subject,at)
            if key in seen:
                raise ValueError("duplicate comparison observation")
            seen.add(key)
            fixtures.add(fid)
            if obs.get("round") is not None:
                rounds.add(str(obs["round"]))
            cells, actual_values = {}, []
            for s in source_ids:
                fact, conflict = _single_source(conn,s,fid,team,field,at,subject,"trial",
                                                prospective=obs.get("prospective") is True)
                counter = summary[s]
                if conflict:
                    counter["conflicts"] += 1
                if not fact or conflict:
                    counter["missing"] += 1
                    cells[s] = {"state":"conflict" if conflict else "missing"}
                    continue
                counter["on_time"] += 1
                eid = fact["evidence_id"]
                counter["evidence_ids"].append(eid)
                actual_values.append(fact["value"])
                cell = {"state":"observed", "evidence_id":eid}
                origin, reference = obs.get("reference_origin"), obs.get("expected_value")
                if (isinstance(reference,dict) and reference and origin and obs.get("reference_locator")
                        and origin not in (fact["origin_id"],fact["upstream"],s)):
                    correct = _matches_expected(fact["value"],reference)
                    cell.update(correct=correct, reference_locator=obs["reference_locator"])
                    counter["verified"] += 1
                    counter["correct"] += correct
                cells[s] = cell
            if len(actual_values) == len(source_ids) and len(source_ids) > 1:
                overlap += 1
                if not all(_matches_expected(v, actual_values[0]) and _matches_expected(actual_values[0],v)
                           for v in actual_values[1:]):
                    disagreements += 1
            rows.append({"fixture_id":fid,"team":team,"subject":subject,"cutoff":at,"sources":cells})
    denom = len(rows)
    for stats in summary.values():
        stats["on_time_rate"] = stats["on_time"]/denom if denom else None
        stats["verified_correct_rate"] = stats["correct"]/denom if denom else None
    return {"field":field,"denominator":denom,"fixture_count":len(fixtures),"rounds":sorted(rounds),
            "denominator_provided":supplied,"by_source":summary,"overlap_count":overlap,
            "disagreement_count":disagreements,"rows":rows}


_ACTIONS = {"evidence":"quarantine", "feature":"rebuild", "training_set":"rebuild",
            "calibrator":"retrain_or_rollback", "model":"retrain_or_rollback",
            "prediction":"recompute_open_or_annotate_sealed", "sim_order":"annotate_sealed",
            "ledger":"reconcile_without_rewriting"}


def _parents(parents):
    if not isinstance(parents, list) or not parents:
        raise ValueError("supply at least one parent artifact or evidence id")
    result = set()
    for parent in parents:
        if isinstance(parent,str):
            result.add(("evidence",required(parent,"evidence id")))
        elif isinstance(parent,dict) and parent.get("kind") in _ACTIONS:
            result.add((parent["kind"],required(parent.get("key"),"parent key")))
        else:
            raise ValueError("invalid parent reference")
    return sorted(result)


def link_lineage_db(conn, kind, key, parents):
    """Attach a derived artifact within the caller's initialized SQLite transaction."""
    if kind not in _ACTIONS or kind == "evidence":
        raise ValueError("unsupported derived artifact kind")
    key = required(key,"artifact key (including version)")
    refs = _parents(parents)
    for parent_kind, parent_key in refs:
        if parent_kind == "evidence":
            exists = conn.execute("SELECT 1 FROM ev_facts WHERE evidence_id=?", (parent_key,)).fetchone()
        else:
            exists = conn.execute("SELECT 1 FROM ev_artifacts WHERE kind=? AND artifact_key=?",
                                  (parent_kind, parent_key)).fetchone()
        if not exists or (parent_kind, parent_key) == (kind, key):
            raise ValueError(f"unknown or self-referential lineage parent: {parent_kind}/{parent_key}")
    encoded = dump(refs)
    old = conn.execute("SELECT parents FROM ev_artifacts WHERE kind=? AND artifact_key=?", (kind, key)).fetchone()
    if old and old[0] != encoded:
        raise ValueError("artifact lineage is immutable; give the new artifact a new version key")
    conn.execute("INSERT OR IGNORE INTO ev_artifacts VALUES(?,?,?)", (kind, key, encoded))
    conn.executemany("INSERT OR IGNORE INTO ev_edges VALUES(?,?,?,?)",
                     [(pk, pi, kind, key) for pk, pi in refs])
    return {"kind":kind,"key":key,"parents":[{"kind":k,"key":v} for k,v in refs]}


def link_lineage(data_dir, kind, key, parents):
    with db(data_dir, write=True) as conn:
        return link_lineage_db(conn, kind, key, parents)


def _impacted(conn, evidence_ids):
    if not evidence_ids:
        return []
    seed = ",".join("(?,?)" for _ in evidence_ids)
    params = [v for eid in evidence_ids for v in ("evidence",eid)]
    sql = f"""WITH RECURSIVE affected(kind,k) AS (
                  VALUES {seed} UNION SELECT e.child_kind,e.child_key FROM ev_edges e
                  JOIN affected a ON e.parent_kind=a.kind AND e.parent_key=a.k
              ) SELECT kind,k FROM affected ORDER BY kind,k"""
    return [{"kind":r[0],"key":r[1],"action":_ACTIONS[r[0]]} for r in conn.execute(sql,params)]


def affected_artifacts(data_dir, evidence_ids):
    with db(data_dir) as conn:
        return _impacted(conn,sorted(set(evidence_ids)))


def _incident_at(conn, incident_id):
    row = conn.execute("""SELECT document FROM ev_incident_versions WHERE incident_id=?
                          ORDER BY updated_at DESC LIMIT 1""",(incident_id,)).fetchone()
    if not row:
        return None
    record = json.loads(row[0])
    record["impacted"] = _impacted(conn,record["affected_evidence_ids"])
    return record


def get_incident(data_dir, incident_id):
    with db(data_dir) as conn:
        record = _incident_at(conn,incident_id)
        if not record:
            raise ValueError(f"unknown incident: {incident_id}")
        return record


def open_incident(data_dir, record):
    ident = required(record.get("incident_id"),"incident_id")
    symptom = required(record.get("symptom"),"observed symptom")
    raw_ids = record.get("affected_evidence_ids")
    if not isinstance(raw_ids,list) or not raw_ids:
        raise ValueError("incident needs affected evidence ids")
    ids = sorted({required(e,"evidence id") for e in raw_ids})
    opened = utc(record.get("opened_at",now_utc()))
    with db(data_dir,write=True) as conn:
        if _incident_at(conn,ident):
            raise ValueError("incident already exists")
        for eid in ids:
            prior = conn.execute("SELECT received_at FROM ev_facts WHERE evidence_id=?", (eid,)).fetchone()
            if not prior or prior[0] > opened:
                raise ValueError("incident must concern evidence received before discovery")
        result = {"incident_id":ident,"status":"open","symptom":symptom,"opened_at":opened,"closed_at":None,
                  "quarantine_recorded_at":utc(now_utc()),
                  "affected_evidence_ids":ids,"root_cause":record.get("root_cause"),
                  "remedy":None,"regression_case":None,"impacted":_impacted(conn,ids)}
        conn.execute("INSERT INTO ev_incident_versions VALUES(?,?,?)",(ident,opened,dump(result)))
        conn.executemany("INSERT INTO ev_quarantine VALUES(?,?,?)",[(ident,eid,opened) for eid in ids])
    save_report(data_dir,"incident",ident,result)
    return result


def resolve_incident(data_dir, record):
    ident = required(record.get("incident_id"),"incident_id")
    closed = utc(record.get("closed_at",now_utc()))
    checks = record.get("verification")
    valid_checks = ("bad_sample_rejected","good_sample_accepted","affected_range_replayed")
    if not isinstance(checks,dict) or any(checks.get(k) is not True for k in valid_checks):
        raise ValueError("closing requires successful bad/good sample tests and an affected-range replay")
    fix = {k:required(record.get(k),k) for k in ("root_cause","remedy","regression_case","fix_version")}
    with db(data_dir,write=True) as conn:
        current = _incident_at(conn,ident)
        if not current or current["status"] != "open" or closed <= current["opened_at"]:
            raise ValueError("resolve an open incident after its discovery")
        corrections = record.get("correction_ids",[])
        if not isinstance(corrections,list):
            raise ValueError("correction_ids must be a list")
        original = [json.loads(conn.execute("SELECT document FROM ev_facts WHERE evidence_id=?",
                                            (eid,)).fetchone()[0]) for eid in current["affected_evidence_ids"]]
        repaired = []
        identity = ("source_id", "fixture_id", "team", "field", "subject", "origin_id", "claim_key")
        for eid in corrections:
            row = conn.execute("SELECT document FROM ev_facts WHERE evidence_id=? AND received_at<=?",
                               (eid, closed)).fetchone()
            if not row:
                raise ValueError(f"missing correction at repair time: {eid}")
            corrected = json.loads(row[0])
            if not any(all(corrected[k] == bad[k] for k in identity) and corrected["revision"] > bad["revision"]
                       for bad in original):
                raise ValueError(f"correction must be a later revision of affected evidence: {eid}")
            repaired.append(corrected)
        guard = None
        if "guard" in record:
            guard = _guard_rule(record["guard"])
            def applies(fact):
                return fact["source_id"] == guard["source_id"] and fact["field"] == guard["field"] and fact["state"] == "active"
            bad_sample = [fact for fact in original if applies(fact)]
            good_sample = [fact for fact in repaired if applies(fact)]
            if not bad_sample or not good_sample or any(_passes_guard(f["value"], guard) for f in bad_sample):
                raise ValueError("guard must reject the affected originals and have a corrected control")
            if not all(_passes_guard(f["value"], guard) for f in good_sample):
                raise ValueError("guard rejected a corrected control")
            conn.execute("INSERT INTO ev_guards VALUES(?,?,?,?)",
                         (guard["source_id"], guard["field"], ident, dump(guard)))
        current.update(fix, status="resolved", closed_at=closed, verification=checks,
                       correction_ids=corrections, guard=guard,
                       verification_mode="enforced_guard" if guard else "operator_attested")
        conn.execute("INSERT INTO ev_incident_versions VALUES(?,?,?)",(ident,closed,dump(current)))
    save_report(data_dir,"incident",ident,current)
    return current


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def register(subparsers):
    """Attach source, evidence and incident operations to the main CLI."""
    from . import context_feeds
    context_feeds.register(subparsers)
    src = subparsers.add_parser("source",help="免费数据源、按字段授权及试采").add_subparsers(dest="source_op",required=True)
    ev = subparsers.add_parser("evidence",help="赛前证据及来源血缘").add_subparsers(dest="evidence_op",required=True)
    inc = subparsers.add_parser("incident",help="隔离错数并记录根因和修复").add_subparsers(dest="incident_op",required=True)
    def bind(cmd, func):
        cmd.set_defaults(handler=func, func=func)
    def file_command(group,name,help_text,func):
        cmd = group.add_parser(name,help=help_text)
        cmd.add_argument("file",type=Path)
        bind(cmd, lambda args:func(args.data_dir,_read(args.file)))
    def id_command(group,name,help_text,func):
        cmd = group.add_parser(name,help=help_text)
        cmd.add_argument("id")
        bind(cmd, lambda args:func(args.data_dir,args.id))
    file_command(src,"register","登记来源及字段使用许可",register_source)
    bind(src.add_parser("list",help="列出来源及其质量记录"), lambda a:{"sources":list_sources(a.data_dir)})
    file_command(src, "discover", "从获准目录登记新候选，保留现有用途许可", discover_sources)
    gaps = src.add_parser("gaps", help="按字段找出免费来源、备用来源和实采覆盖缺口")
    gaps.add_argument("field", nargs="?")
    bind(gaps, lambda a:coverage_gaps(a.data_dir, a.field))
    id_command(src,"show","查看来源、使用说明和试采统计",source_report)
    file_command(src,"check","记录实际获取结果、时效和源冻结检查",observe_source)
    probe = src.add_parser("trial",help="只作试采的样本与质量统计")
    probe.add_argument("id")
    probe.add_argument("--file",type=Path)
    def trial(args):
        if args.file:
            raw = _read(args.file)
            rows = raw if isinstance(raw,list) else [raw]
            for row in rows:
                if row.get("source_id",args.id) != args.id:
                    raise ValueError("mismatched source id in trial batch")
            import_evidence(args.data_dir,[dict(r,source_id=args.id,trial_only=True) for r in rows])
        return source_report(args.data_dir,args.id)
    bind(probe, trial)
    file_command(src,"compare","同赛程、同字段、独立来源的对照",lambda d,r:source_comparison(
        d,r["source_ids"],r["field"],r.get("observations"),cutoff=r.get("cutoff")))
    file_command(ev,"import","导入具有出处和用途授权的事实",import_evidence)
    id_command(ev,"show","按 ID 读取原始证据定位和版本",get_evidence)
    file_command(ev,"link","登记特征、训练和预测血缘",lambda d,r:link_lineage(d,r["kind"],r["key"],r["parents"]))
    context = ev.add_parser("context",help="按决策截止时间重建可见信息")
    for key in ("fixture_id","home","away","cutoff"):
        context.add_argument(key)
    context.add_argument("--purpose",choices=sorted(USES),default="prediction")
    context.add_argument("--prospective", action="store_true", help="仅接纳系统真实写入早于截止的证据")
    bind(context, lambda a:as_of_context(a.data_dir,a.fixture_id,a.home,a.away,a.cutoff,
                                        purpose=a.purpose, prospective=a.prospective))
    file_command(inc,"open","标记错误输入并立即隔离",open_incident)
    file_command(inc,"resolve","登记根因、范围和防复发验证",resolve_incident)
    id_command(inc,"show","查看受污染的证据和派生产物",get_incident)
