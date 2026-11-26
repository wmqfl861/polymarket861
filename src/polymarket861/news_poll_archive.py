"""Store consecutive equivalent news checks once, with an exact witness for every poll."""

import json
import re

from . import store

# Keep brief runs in the ordinary tables. The newest check stays there for
# interrupted collectors, latest-status queries and compatibility with older data.
_MIN_STREAK = 8
_DETAIL = {"checked_at": "checked", "source_observed_at": "seen", "next_at": "next"}
_PROOF = {
    "requested_at": "request",
    "received_at": "received",
    "observed_at": "observed",
}
_WIRE = {
    "observed_at": "wire_seen",
    "provider_at": "wire_provider",
    "recorded_at": "wire_recorded",
}
_TABLES = {
    "news_checks": "news_check_history",
    "news_poll_proofs": "news_receipt_history",
    "news_wire_polls": "news_wire_history",
}


def _clock_sql(key):
    return f"coalesce(json_extract(t.clocks,'$.{key}'),t.checked_at)"


def _restore_sql(template, fields, wire_clock):
    values = ",".join(f"'$.{name}',{_clock_sql(key)}" for name, key in fields.items())
    base = f"json_set(s.{template},{values},'$.response_sha256',lower(hex(t.response)))"
    return (
        f"CASE WHEN s.members IS NULL THEN {base} ELSE "
        f"json_set({base},'$.provider_state_as_of',{_clock_sql(wire_clock)}) END"
    )


_HEAD_COLUMNS = """(
 season TEXT NOT NULL, team_key TEXT NOT NULL, source TEXT NOT NULL, shape TEXT NOT NULL,
 last_check INTEGER NOT NULL,last_receipt TEXT NOT NULL,streak INTEGER NOT NULL,run INTEGER,
 PRIMARY KEY(season,team_key,source))"""

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS news_poll_runs(
 id INTEGER PRIMARY KEY, season TEXT NOT NULL, team_key TEXT NOT NULL, source TEXT NOT NULL,
 fixture_id TEXT NOT NULL, kickoff TEXT NOT NULL, state TEXT NOT NULL, detail_template TEXT NOT NULL,
 proof_template TEXT NOT NULL, members TEXT, wire_fixture TEXT, first_checked_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
 observations INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS news_poll_run_stream ON news_poll_runs(season,team_key,source,fixture_id);
CREATE INDEX IF NOT EXISTS news_poll_run_fixture ON news_poll_runs(wire_fixture);
CREATE TABLE IF NOT EXISTS news_poll_ticks(
 id INTEGER PRIMARY KEY, run INTEGER NOT NULL REFERENCES news_poll_runs(id), checked_at TEXT NOT NULL,
 receipt TEXT NOT NULL UNIQUE, response BLOB NOT NULL, clocks TEXT NOT NULL, wire_order INTEGER,
 standalone_proof INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS news_tick_reopened ON news_poll_ticks(standalone_proof) WHERE standalone_proof=1;
CREATE INDEX IF NOT EXISTS news_poll_run_time ON news_poll_ticks(run,checked_at,id);
CREATE TABLE IF NOT EXISTS news_poll_heads{_HEAD_COLUMNS};
CREATE TABLE IF NOT EXISTS news_poll_upgrade_heads{_HEAD_COLUMNS};
CREATE TABLE IF NOT EXISTS news_poll_upgrade(
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), through_check INTEGER NOT NULL, cursor INTEGER NOT NULL);
INSERT OR IGNORE INTO news_poll_upgrade SELECT 1,coalesce(max(id),0),0 FROM news_checks;
CREATE INDEX IF NOT EXISTS news_article_receipt ON news_article_proofs(receipt);
CREATE INDEX IF NOT EXISTS news_conflict_receipt ON news_publisher_conflicts(receipt);
CREATE INDEX IF NOT EXISTS news_rejected_receipt ON news_rejected_items(response_receipt);
CREATE VIEW IF NOT EXISTS news_folded_observations AS SELECT
 t.id,s.season,s.team_key,s.source,s.fixture_id,s.kickoff,t.checked_at,s.state,
 {_restore_sql("detail_template", _DETAIL, "asof")} AS detail,
 t.receipt,{_restore_sql("proof_template", _PROOF, "provider")} AS payload,
 t.wire_order,s.members,s.wire_fixture,{_clock_sql("wire_seen")} AS observed_at,
 {_clock_sql("wire_provider")} AS provider_at,{_clock_sql("wire_recorded")} AS recorded_at
 FROM news_poll_runs s JOIN news_poll_ticks t ON t.run=s.id;
CREATE VIEW IF NOT EXISTS news_check_history AS SELECT * FROM news_checks UNION ALL
 SELECT id,season,team_key,source,fixture_id,kickoff,checked_at,state,detail FROM news_folded_observations;
CREATE VIEW IF NOT EXISTS news_receipt_history AS SELECT id,payload FROM news_poll_proofs UNION ALL
 SELECT receipt,payload FROM news_folded_observations f
 WHERE NOT EXISTS(SELECT 1 FROM news_poll_proofs p WHERE p.id=f.receipt);
CREATE VIEW IF NOT EXISTS news_wire_history AS
 SELECT rowid AS rowid,receipt,season,team_key,fixture_id,observed_at,provider_at,recorded_at,articles
 FROM news_wire_polls UNION ALL
 SELECT wire_order,receipt,season,team_key,wire_fixture,observed_at,provider_at,recorded_at,members
 FROM news_folded_observations f WHERE wire_order IS NOT NULL AND
 NOT EXISTS(SELECT 1 FROM news_wire_polls w WHERE w.receipt=f.receipt);
"""


def history(db, table):
    """Older read-only databases can be replayed without installing any new schema."""
    view = _TABLES[table]
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='view' AND name=?", (view,)
    ).fetchone()
    return view if exists else table


def receipt(db, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
        raise ValueError("invalid news receipt SHA-256")
    source = history(db, "news_poll_proofs")
    row = db.execute(
        f"SELECT payload FROM {source} WHERE id=?", (identifier,)
    ).fetchone()
    if not row:
        raise ValueError("unknown news poll receipt")
    payload = json.loads(row[0])
    if store.digest(payload) != identifier:
        raise ValueError("news poll receipt failed SHA-256 verification")
    return {"id": identifier, **payload}


def _verified_members(db, wire, proof, check):
    """Bind the membership list and its player states back to the original response projection."""
    if not (wire["season"] == check["season"] and wire["team_key"] == check["team_key"]
            and wire["fixture_id"] in (None, check["fixture_id"])
            and wire["observed_at"] == proof["observed_at"]
            and wire["provider_at"] == proof["provider_state_as_of"]
            and wire["observed_at"] <= wire["recorded_at"] <= check["checked_at"]):
        return False
    manifest = db.execute(
        "SELECT articles,projection FROM news_wire_manifests WHERE id=?", (wire["articles"],)
    ).fetchone()
    if not manifest:
        return False
    members, projection = (json.loads(value) for value in manifest)
    if (not isinstance(members, list) or any(type(n) is not int for n in members)
            or members != sorted(set(members)) or not isinstance(projection, dict)
            or not isinstance(projection.get("states"), dict)
            or store.digest([members, projection]) != wire["articles"]
            or store.digest(projection) != proof.get("wire_projection_sha256")):
        return False
    rows = list(db.execute(
        """SELECT a.external_id,s.state_sha256 FROM json_each(?) j
        JOIN news_articles a ON a.seq=j.value JOIN news_wire_states s ON s.article=a.seq
        WHERE a.season=? AND a.team_key=? AND a.source=? AND a.fixture_id IS ?
          AND a.received_at<=? AND a.recorded_at<=?""",
        (manifest["articles"], check["season"], check["team_key"], check["source"],
         wire["fixture_id"], wire["observed_at"], wire["recorded_at"]),
    ))
    states = projection["states"]
    if len(rows) != len(members) or any(states.get(key) != value for key, value in rows):
        return False
    if wire["fixture_id"] is None:
        # Collections made before the news window can have genuine provider
        # states and no in-scope edition. Recheck the original schedule before
        # accepting that exception, even when the manifest lists zero articles.
        from .news import _schedule, _target_for, _timetables

        when = wire["recorded_at"]
        games = _timetables(_schedule(db, when, check["season"]))
        targets = games.get((check["season"], check["team_key"]), ())
        return not rows and _target_for(wire["provider_at"], targets, when) is None
    return len(rows) == len(states) and dict(rows) == states


def wire_coverage_received(db, root, check, detail, scope, config):
    """Bind both current and legacy observations to the actual scoped upstream receipt."""
    from . import news_minutes, news_sources

    try:
        if news_sources.validate_source(config)["id"] != news_sources.WIRE:
            return False
    except (KeyError, TypeError, ValueError):
        return False
    observed, provider = detail.get("source_observed_at"), detail.get("provider_state_as_of")
    if not (check and observed and provider and scope["valid_from"] <= provider <= observed <= check["checked_at"]
            and provider < scope["valid_until"] and detail.get("response_hash_basis") == "upstream_response_bytes"
            and detail.get("coverage_evidence", scope) == scope and
            db.execute("SELECT 1 FROM sqlite_master WHERE name='news_wire_polls'").fetchone()):
        return False
    wires = history(db, "news_wire_polls")
    for wire in db.execute(f"""SELECT * FROM {wires} WHERE team_key=? AND season=? AND observed_at=?
            AND provider_at=? AND recorded_at<=? AND (fixture_id=? OR fixture_id IS NULL)""",
            (check["team_key"], check["season"], observed, provider, check["checked_at"], check["fixture_id"])):
        try:
            proof = receipt(db, wire["receipt"])
            if "coverage_evidence" not in proof:
                # Old zero-row responses carry no club identity. Require a named,
                # league-scoped directory already received on this installation.
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_usage_history'").fetchone():
                    continue
                prior = news_minutes.latest(db, scope["season"], scope["club"], observed)
                if not (prior and prior["provider_as_of"] <= observed and prior["received_at"] <= observed
                        and news_minutes.verified_players(root, prior, config["team"], scope["season"])):
                    continue
            if (proof["source"] == scope["source"] and proof["team"] == scope["club"]
                    and proof["season"] == scope["season"] and proof["hash_basis"] == "upstream_response_bytes"
                    and proof["request_url"] == config.get("url") and proof["observed_at"] == observed
                    and proof["requested_at"] <= proof["received_at"] <= observed
                    and provider <= proof["received_at"] and proof["provider_state_as_of"] == provider
                    and proof["response_sha256"] == detail.get("response_sha256")
                    and proof.get("coverage_evidence", scope) == scope and _verified_members(db, wire, proof, check)):
                return True
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return False


def verified_check(db, check, detail):
    """Require the folded check's own physical observation before declaring it fresh."""
    if not check:
        return True
    # Older read-only stores and standalone checks have no folded tick.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='news_poll_ticks'").fetchone():
        return True
    tick = db.execute("SELECT receipt FROM news_poll_ticks WHERE id=?", (check["id"],)).fetchone()
    if not tick:
        return True
    try:
        proof = receipt(db, tick["receipt"])
        if not (proof["season"] == check["season"] and proof["team"] == check["team_key"]
                and proof["source"] == check["source"] and detail["checked_at"] == check["checked_at"]
                and proof["observed_at"] == detail["source_observed_at"]
                and proof["requested_at"] <= proof["received_at"] <= proof["observed_at"] <= check["checked_at"]
                and proof["response_sha256"] == detail["response_sha256"]
                and proof["hash_basis"] == detail["response_hash_basis"]):
            return False
        if "wire_projection_sha256" not in proof:
            return "provider_state_as_of" not in detail
        if not detail.get("provider_state_as_of") == proof["provider_state_as_of"] <= proof["observed_at"]:
            return False
        wire = db.execute(
            "SELECT * FROM news_wire_history WHERE receipt=?", (tick["receipt"],)
        ).fetchone()
        return bool(wire and _verified_members(db, wire, proof, check))
    except (ValueError, TypeError, KeyError):
        return False


def inventory(db):
    runs, ticks, wire = db.execute("""SELECT count(*),coalesce(sum(observations),0),
        coalesce(sum(CASE WHEN members IS NOT NULL THEN observations ELSE 0 END),0)
        FROM news_poll_runs""").fetchone()
    reopened = db.execute(
        "SELECT count(*) FROM news_poll_ticks WHERE standalone_proof=1"
    ).fetchone()[0]
    return {
        "unchanged_check_runs": runs,
        "reopened_standalone_proofs": reopened,
        "individually_replayable_checks_in_runs": ticks,
        "wire_confirmations_in_runs": wire,
        "standalone_checks": db.execute("SELECT count(*) FROM news_checks").fetchone()[
            0
        ],
    }


def _sample(db, row, ident):
    """Limit folding to successful, complete reads with no outstanding or changed evidence."""
    if not ident:
        return None
    try:
        detail = json.loads(row["detail"])
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(detail, dict)
        or detail.get("state") != "ok"
        or not detail.get("polled")
        or not detail.get("network_complete")
        or not detail.get("fetch_complete")
        or detail.get("issues")
        or detail.get("articles_added")
        or detail.get("claims_added")
        or detail.get("pending_entries")
    ):
        return None
    saved = db.execute(
        "SELECT payload FROM news_poll_proofs WHERE id=?", (ident,)
    ).fetchone()
    if not saved:
        return None
    try:
        proof = json.loads(saved[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(proof, dict):
        return None
    response = proof.get("response_sha256")
    if (
        not isinstance(response, str)
        or not re.fullmatch(r"[0-9a-f]{64}", response)
        or detail.get("response_sha256") != response
        or store.digest(proof) != ident
    ):
        return None
    wire = db.execute(
        "SELECT rowid,* FROM news_wire_polls WHERE receipt=?", (ident,)
    ).fetchone()
    if (wire is not None) != ("provider_state_as_of" in proof) or (
        wire is not None
    ) != ("provider_state_as_of" in detail):
        return None
    if wire is not None and (wire["season"], wire["team_key"]) != (
        row["season"],
        row["team_key"],
    ):
        return None
    detail.pop("response_sha256")
    proof.pop("response_sha256")
    clocks = {}
    for metadata, fields in ((detail, _DETAIL), (proof, _PROOF)):
        for name, key in fields.items():
            stamp = metadata.pop(name, None)
            if not isinstance(stamp, str):
                return None
            if stamp != row["checked_at"]:
                clocks[key] = stamp
    members = wire_order = None
    if wire is not None:
        members, wire_order = wire["articles"], wire["rowid"]
        for key, stamp in (
            ("asof", detail.pop("provider_state_as_of")),
            ("provider", proof.pop("provider_state_as_of")),
            *((key, wire[name]) for name, key in _WIRE.items()),
        ):
            if not isinstance(stamp, str):
                return None
            if stamp != row["checked_at"]:
                clocks[key] = stamp
    header = (
        row["season"],
        row["team_key"],
        row["source"],
        row["fixture_id"],
        row["kickoff"],
        row["state"],
        store.canonical(detail),
        store.canonical(proof),
        members,
        wire["fixture_id"] if wire is not None else None,
    )
    return header, (
        row["checked_at"],
        ident,
        bytes.fromhex(response),
        store.canonical(clocks),
        wire_order,
    )


def _referenced(db, row, ident, next_at):
    # Explicitly linked originals and the check used by an existing seal stay
    # standalone. Future historical reads can reopen *any* other tick by its SHA.
    linked = db.execute(
        """SELECT 1 FROM news_article_proofs WHERE receipt=?
        UNION ALL SELECT 1 FROM news_feed_inbox WHERE receipt=?
        UNION ALL SELECT 1 FROM news_publisher_conflicts WHERE receipt=?
        UNION ALL SELECT 1 FROM news_rejected_items WHERE response_receipt=? LIMIT 1""",
        (ident,) * 4,
    )
    if linked.fetchone():
        return True

    def used(table, document, cutoff):
        return db.execute(
            f"""SELECT 1 FROM {table} WHERE fixture_id=?
            AND CASE WHEN json_valid({document}) THEN
                 instr({document},?)>0 OR ({cutoff}>=? AND {cutoff}<?)
                ELSE 1 END LIMIT 1""",
            (row["fixture_id"], ident, row["checked_at"], next_at),
        ).fetchone()

    if used(
        "forecast_seals",
        "data_json",
        "json_extract(data_json,'$.news_pair.input_cutoff')",
    ):
        return True
    events = db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='news_event_updates'"
    ).fetchone()
    return bool(
        events
        and used("news_event_updates", "document", "json_extract(document,'$.cutoff')")
    )


def _fold(db, previous, sample, run):
    header, tick = sample
    if run is None:
        run = db.execute(
            """INSERT INTO news_poll_runs(season,team_key,source,fixture_id,kickoff,state,
            detail_template,proof_template,members,wire_fixture,first_checked_at,last_checked_at,observations)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (*header, tick[0], tick[0]),
        ).lastrowid
    db.execute(
        """INSERT INTO news_poll_ticks(id,run,checked_at,receipt,response,clocks,wire_order)
        VALUES (?,?,?,?,?,?,?)""",
        (previous["id"], run, *tick),
    )
    # Check the lossless codec on the actual historical record before retiring any
    # redundant copy. All writes, including the collector checkpoint, are atomic.
    folded = db.execute(
        "SELECT detail,payload FROM news_folded_observations WHERE id=?",
        (previous["id"],),
    ).fetchone()
    if (
        json.loads(folded["detail"]) != json.loads(previous["detail"])
        or store.digest(json.loads(folded["payload"])) != tick[1]
    ):
        raise ValueError("news check compaction changed its evidence")
    db.execute(
        "UPDATE news_poll_runs SET observations=observations+1,last_checked_at=? WHERE id=?",
        (tick[0], run),
    )
    db.execute("DELETE FROM news_wire_polls WHERE receipt=?", (tick[1],))
    db.execute("DELETE FROM news_poll_proofs WHERE id=?", (tick[1],))
    db.execute("DELETE FROM news_checks WHERE id=?", (previous["id"],))
    return run


def track(db, check_id, ident, *, upgrading=False):
    """Advance one stream in the same transaction that finishes its newest check."""
    from .news import POLL_INTERVAL

    row = db.execute("SELECT * FROM news_checks WHERE id=?", (check_id,)).fetchone()
    key = (row["season"], row["team_key"], row["source"])
    sample = _sample(db, row, ident)
    where = "season=? AND team_key=? AND source=?"
    heads = "news_poll_upgrade_heads" if upgrading else "news_poll_heads"
    if sample is None:
        db.execute(f"DELETE FROM {heads} WHERE " + where, key)
        return
    shape = store.digest(sample[0])
    previous = db.execute(f"SELECT * FROM {heads} WHERE " + where, key).fetchone()
    run, streak = None, 1
    folded = False
    if previous and previous["shape"] == shape and ident != previous["last_receipt"]:
        prev = db.execute(
            "SELECT * FROM news_checks WHERE id=?", (previous["last_check"],)
        ).fetchone()
        # A legacy writer, an offline gap or a regressing wall clock ends the
        # run. Only actual network observations appear in its history.
        intervening = db.execute(
            "SELECT 1 FROM news_checks WHERE id>? AND id<? AND " + where + " LIMIT 1",
            (previous["last_check"], check_id, *key),
        ).fetchone()
        if (
            prev
            and not intervening
            and prev["checked_at"] < row["checked_at"]
            and store.instant(row["checked_at"])
            <= store.instant(json.loads(prev["detail"])["next_at"])
            + POLL_INTERVAL
        ):
            streak, run = previous["streak"] + 1, previous["run"]
            if streak >= _MIN_STREAK and not _referenced(
                db, prev, previous["last_receipt"], row["checked_at"]
            ):
                prior = _sample(db, prev, previous["last_receipt"])
                if (
                    prior
                    and prior[0] == sample[0]
                    and not db.execute(
                        "SELECT 1 FROM news_poll_ticks WHERE receipt=?",
                        (previous["last_receipt"],),
                    ).fetchone()
                ):
                    run = _fold(db, prev, prior, run)
                    folded = True
    db.execute(
        f"""INSERT INTO {heads} VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(season,team_key,source)
        DO UPDATE SET shape=excluded.shape,last_check=excluded.last_check,last_receipt=excluded.last_receipt,
        streak=excluded.streak,run=excluded.run""",
        (*key, shape, check_id, ident, streak, run),
    )
    return folded


def upgrade_inventory(db):
    through, cursor = db.execute(
        "SELECT through_check,cursor FROM news_poll_upgrade WHERE singleton=1"
    ).fetchone()
    return {
        "pre_upgrade_through_check_id": through,
        "examined_through_check_id": cursor,
        "complete": cursor >= through,
    }


def compact_existing(db, limit):
    """Advance a bounded, resumable scan of rows present before the first upgrade.

    Old check rows did not record their network receipt ID. Only an unambiguous match
    against the actual source, team, clocks and response body digest is admissible.
    """
    progress = upgrade_inventory(db)
    through, cursor = (
        progress["pre_upgrade_through_check_id"],
        progress["examined_through_check_id"],
    )
    examined = folded = ambiguous = 0
    if cursor < through:
        # This lookup index is needed only by old archives. Creating it on first
        # explicit maintenance keeps ordinary upgrade and collection free of a
        # full receipt scan. Malformed historical JSON is ineligible.
        clock = "CASE WHEN json_valid(payload) THEN json_extract(payload,'$.observed_at') END"
        db.execute(
            f"CREATE INDEX IF NOT EXISTS news_upgrade_receipt_clock ON news_poll_proofs({clock})"
        )
        batch = db.execute(
            "SELECT * FROM news_checks WHERE id>? AND id<=? ORDER BY id LIMIT ?",
            (cursor, through, limit),
        ).fetchall()
        for check in batch:
            ident = None
            try:
                detail = json.loads(check["detail"])
            except (TypeError, ValueError):
                detail = {}
            if (
                isinstance(detail, dict)
                and detail.get("polled")
                and detail.get("response_sha256")
                and detail.get("source_observed_at")
            ):
                matches = db.execute(
                    f"""SELECT id FROM news_poll_proofs WHERE ({clock})=?
                    AND json_extract(payload,'$.response_sha256')=? AND json_extract(payload,'$.source')=?
                    AND json_extract(payload,'$.season')=? AND json_extract(payload,'$.team')=? LIMIT 2""",
                    (
                        detail["source_observed_at"],
                        detail["response_sha256"],
                        check["source"],
                        check["season"],
                        check["team_key"],
                    ),
                ).fetchall()
                if len(matches) == 1:
                    ident = matches[0][0]
                else:
                    ambiguous += 1
            folded += bool(track(db, check["id"], ident, upgrading=True))
            cursor, examined = check["id"], examined + 1
        if len(batch) < limit:
            cursor = through
        db.execute("UPDATE news_poll_upgrade SET cursor=? WHERE singleton=1", (cursor,))
        if cursor >= through:
            db.execute("DELETE FROM news_poll_upgrade_heads")
            db.execute("DROP INDEX IF EXISTS news_upgrade_receipt_clock")
    return {
        **upgrade_inventory(db),
        "examined": examined,
        "folded": folded,
        "unlinked_or_ambiguous": ambiguous,
    }
