"""Schedule evidence for optional five-league paper entries with incomplete venue flags."""

from datetime import timedelta

from . import (
    archive_proof,
    big5_market,
    big5_market_schedule,
    dbscope,
    kickoff_proof,
    market_provider,
    opportunity,
    store,
)


def _observation(root, db, league, row):
    def read():
        event = archive_proof._event_capture(
            db, root, row["sha"], row["received_at"], row["slug"]
        )
        fields = big5_market._identity(event, league)
        return (
            fields,
            big5_market_schedule.pregame_flags(event),
            big5_market_schedule.has_started(event),
        )

    return dbscope.checked(
        root,
        ("paper_schedule", league, row["slug"], row["sha"], row["received_at"]),
        read,
    )


def _event_rows(db, fixture, *, through=None, after=None):
    # Refresh can retain a successful native response but reject an inconsistent
    # auxiliary before indexing it. Retain that evidence in the schedule timeline.
    return db.execute(
        """WITH receipts(slug,sha,received_at) AS (
               SELECT slug,sha,received_at FROM ma_events WHERE fixture=:main
               UNION SELECT selector,sha,received_at FROM ma_requests
               WHERE status='ok' AND operation IN ('event','league_event') AND selector IN (:main,:aux))
           SELECT r.*,e.event_id,e.kickoff FROM receipts r LEFT JOIN ma_events e
           ON (e.slug,e.sha,e.received_at)=(r.slug,r.sha,r.received_at)
           WHERE (:through IS NULL OR r.received_at<=:through)
             AND (:after IS NULL OR (r.received_at>:after AND (e.kickoff IS NULL OR julianday(e.kickoff)<=julianday(:after))))
           ORDER BY r.received_at,r.slug,r.sha""",
        {
            "main": fixture,
            "aux": fixture + "-more-markets",
            "through": through,
            "after": after,
        },
    )


def _later_early_start(root, db, fixture, market_slug, ids, execution, as_of):
    # A later live/final flag records the state on its own receipt date. It cannot
    # place the start before our buy. A native revision placing kickoff at or
    # before the buy, or a separately attested actual whistle, can invalidate it.
    league, _, home, away = fixture.split("/")
    for row in _event_rows(db, market_slug, after=execution, through=as_of):
        event, _ = archive_proof._event_with_operation(
            db, root, row["sha"], row["received_at"], row["slug"], allow_undated=True
        )
        if not event.get("startTime"):
            # A later terminal receipt without a start clock cannot place play before this order.
            continue
        fields = big5_market._identity(event, league)
        if store.instant(fields[3]) <= store.instant(execution):
            slug, main, sport, _, host, visitor, event_id = fields
            if (main, sport, host, visitor) != (
                market_slug,
                league,
                home,
                away,
            ) or ids.get(slug, event_id) != event_id:
                raise ValueError(
                    "later Gamma identity conflicts with the paper entry fixture"
                )
            raise ValueError(
                "later Gamma schedule places paper entry at or after kickoff"
            )


def _actual_whistle(source, fixture, teams, kickoff, execution, as_of):
    root, db = source
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='football_kickoff_receipts'"
    ).fetchone():
        return
    decision = kickoff - timedelta(hours=1)
    season = fixture.split("/")[1]
    aliases = {
        side: {opportunity._club(club[k]) for k in ("name", "alias") if club.get(k)}
        for side, club in market_provider.teams({"teams": teams}).items()
    }
    candidates = db.execute(
        """SELECT DISTINCT r.fixture_id FROM football_kickoff_receipts r
           JOIN fixture_versions f ON (r.fixture_id,r.schedule_version)=(f.fixture_id,f.version)
           WHERE f.kickoff=? AND (? IS NULL OR r.received_at<=?)""",
        (store.iso(kickoff), as_of, as_of),
    )
    for row in candidates:
        fid = row["fixture_id"]
        # Even a matching local ID or wall clock needs the same ordered native sides
        # and precise domestic season schedule that was actually received by decision.
        try:
            fx = kickoff_proof._fixture(
                db, fid, decision, kickoff, require_local_receipt=True
            )
        except ValueError:
            continue
        if fx["season"] != season or any(
            opportunity._club(fx[side]) not in aliases[side]
            for side in ("home", "away")
        ):
            continue
        try:
            witness = kickoff_proof.resolve(
                root, fid, kickoff, decision, db=db, received_through=as_of
            )
        except kickoff_proof.MissingKickoffProof:
            continue
        if store.instant(witness["actual_kickoff"]) <= store.instant(execution):
            raise ValueError("verified actual kickoff precedes or equals paper entry")


def attest(source, fixture, market, kickoff, snapshots, as_of, football=None):
    """Bind both entries to the same ordered, uninterrupted domestic event clock."""
    root, db = source
    league, _, home, away = fixture.split("/")
    main = market["fixture"]
    first, second = (b["received_at"] for b in snapshots)
    rows = list(_event_rows(db, main, through=second))
    heads = {r["slug"]: r["received_at"] for r in rows if r["received_at"] <= first}
    if main not in heads:
        raise ValueError("missing native main event at first paper book")
    ids = {market["event_slug"]: market["event_id"]}
    schedule = []
    for row in rows:
        fields, flags, started = _observation(root, db, league, row)
        slug, held_main, sport, when, host, visitor, event_id = fields
        if started:
            raise ValueError("exchange had already observed play before paper entry")
        if row["received_at"] < heads.get(slug, ""):
            continue  # Superseded schedule before the first book, not a revision in the pair.
        if (
            (held_main, sport, host, visitor) != (main, league, home, away)
            or row["event_id"] is not None
            and event_id != row["event_id"]
            or ids.setdefault(slug, event_id) != event_id
            or store.instant(when) != kickoff
        ):
            raise ValueError(
                "Gamma schedule or fixture changed across paper entry books"
            )
        if flags not in {"explicit_pregame", "schedule_only"}:
            raise ValueError(
                "unavailable or contrary Gamma event state across paper entry books"
            )
        if slug == market["event_slug"]:
            original, _, _ = archive_proof._gamma_markets(
                db, root, row["sha"], row["received_at"], slug
            )
            contracts = original.get(market["condition_id"], [])
            if len(contracts) != 1 or any(
                contracts[0][name] != market[name]
                for name in (
                    "game_times",
                    "tokens",
                    "rule_sha256",
                    "slug",
                    "family",
                    "side",
                    "line",
                    "mapping_error",
                    "market_open",
                    "seconds_delay",
                    "fees_enabled",
                    "fee_schedule",
                )
            ):
                raise ValueError(
                    "frozen market changed during schedule-based paper entry"
                )
        schedule.append(
            {
                "event_slug": slug,
                "received_at": row["received_at"],
                "source_sha256": row["sha"],
                "state": flags,
            }
        )
    basis = [b["research_pregame_basis"] for b in snapshots]
    weakest = (
        "explicit_pregame"
        if all(b == "explicit_pregame" for b in basis)
        else "scheduled_with_incomplete_flags"
    )
    if weakest == "scheduled_with_incomplete_flags":
        cutoff = store.iso(as_of) if as_of else None
        _later_early_start(root, db, fixture, main, ids, second, cutoff)
        # The currently supported independent first-whistle formats prove English competition only.
        if market_provider.FIXTURE.fullmatch(main):
            for source_db in (source, football):
                if source_db:
                    _actual_whistle(
                        source_db,
                        fixture,
                        market["event_teams"],
                        kickoff,
                        second,
                        cutoff,
                    )
    return {
        "basis": weakest,
        "decision": basis[0],
        "execution": basis[1],
        "books": [
            {
                "received_at": b["received_at"],
                "source_sha256": b["source_sha256"],
                "contract_source_sha256": b["contract_source_sha256"],
            }
            for b in snapshots
        ],
        "schedule": schedule,
    }
