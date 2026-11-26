"""Point-in-time Touchline identity joins used only for its typed availability rows."""

import json
from collections import Counter
from datetime import timedelta

from . import (
    football,
    news_ai,
    news_importance,
    news_minutes,
    news_roster,
    news_sources,
    store,
)


def _full_names(values):
    return {
        key
        for value in values
        if isinstance(value, str) and len(key := news_ai._name_key(value)) > 1
    }


def _other_members(db, root, season, club, at):
    """Exclude positively conflicting EPL memberships known by this cutoff."""
    foreign = set()
    for row in db.execute(
        "SELECT DISTINCT club FROM news_usage_history WHERE season=? AND club<>? AND known_at<=?",
        (season, club, at),
    ):
        other = row[0]
        rec = news_minutes.latest(db, season, other, at)
        if not rec or store.instant(at) - store.instant(
            rec["provider_as_of"]
        ) > timedelta(days=news_importance.POLICY["directory_max_age_days"]):
            continue
        try:
            players = news_minutes.verified_players(root, rec, other, season)
            foreign.update(p["id"] for p in players)
        except (OSError, ValueError, TypeError, KeyError):
            continue
    names = set()
    for other, seen, digest in news_roster._latest(db, season, at):
        if club == other or at >= news_roster._expiry(seen):
            continue
        rec = db.execute(
            "SELECT document FROM news_roster_content WHERE digest=?", (digest,)
        ).fetchone()
        if not rec:
            continue
        doc = json.loads(rec[0])
        if store.digest(doc) != digest or doc.get("withheld"):
            continue
        for player in doc["players"]:
            names.update(_full_names([player.get("name"), *player.get("aliases", ())]))
            names.add(news_ai._name_key(player["name"]))
    return foreign, names


def directory(db, root, fixture, team, at, roster):
    # The same validation of original, club, EPL season and played-game
    # denominator is used for both identity and reported zero participation.
    if not roster:
        return {}
    rows, _ = news_importance.directory_players(db, root, fixture, team, at)
    if not rows or next(iter(rows.values())).get("directory_fallback_reason"):
        # An older directory can still support a weight for an established
        # player. A broken newer receipt cannot establish current membership.
        return {}
    season, club = fixture["season"], football.team_key(team)
    foreign, other_names = _other_members(db, root, season, club, at)
    aliases = news_ai._roster_index(roster)[1]
    published = {news_ai._name_key(p["name"]): p["player_id"] for p in roster}
    mononyms = {key for key in published if len(key) == 1}

    def distinctive(names):
        tokens = {news_ai._name_key(n) for n in names}
        return {key for key in tokens if len(key) > 1 or key in mononyms}

    counts = Counter(key for row in rows.values() for key in distinctive(row["names"]))
    # The published default directory has starts/minutes. Only an explicit
    # zero appearance count can justify omitting an unmatched row entirely.
    # A directory predating another league fixture can still help identify a
    # player. It cannot establish that he has never appeared as of this check.
    zero_current = False
    if any(
        item.get("appearances") == 0 and not item["later_completed_matches"]
        for item in rows.values()
    ):
        cutoff = store.iso(at)
        stamp = next(iter(rows.values()))["provider_as_of"]
        zero_current = not any(
            stamp <= g["kickoff"] < cutoff
            for g in news_minutes.club_games(db, root, season, team, cutoff)
        )
    original = next(iter(rows.values()))["original_sha256"]
    # The usage projection preserves a set of aliases. The original also
    # identifies which spelling the publisher supplied as the full name.
    native_names = {
        p["id"]: p.get("name")
        for p in json.loads(store.read_blob(root, original))["data"]
    }
    records = {}
    manual = _aliases_at(db, season, club, at)
    roster_ids = {p["player_id"] for p in roster}
    for number, row in rows.items():
        labels = sorted(row["names"])
        full = _full_names(labels)
        all_labels = {news_ai._name_key(label) for label in labels}
        exact = distinctive(labels)
        possible = set().union(*(aliases.get(k, set()) for k in exact))
        short = set().union(*(aliases.get(k, set()) for k in all_labels - full))
        if not full and len(short) == 1:
            # Preserve a published, unique mononym already eligible for wire extraction.
            possible = short
        blocked = (
            (not full and len(short) > 1)
            or number in foreign
            or bool(exact & other_names)
            or any(counts[k] > 1 for k in exact)
        )
        reviewed = manual.get(number)
        chosen = next(iter(possible)) if len(possible) == 1 else None
        if reviewed:
            ident = reviewed["player_id"]
            if (
                not _alias_proof(db, root, season, team, reviewed, labels)
                or ident not in roster_ids
                or (possible and ident not in possible)
            ):
                blocked = True
            else:
                chosen = ident
        elif len(possible) > 1:
            blocked = True
        # Reversed names and a shared surname/first initial are not identities.
        # They only flag a possible duplicate if both people carry an active
        # claim. A reviewed alias can disambiguate those cases.
        alternatives = (
            {
                ident
                for key, ident in published.items()
                for label in full
                if len(key) >= 2
                and (
                    key == label[::-1]
                    or (key[-1] == label[-1] and key[0][0] == label[0][0])
                )
            }
            if not chosen
            else set()
        )
        external = f"touchline:epl:{number}"
        appearances = row.get("appearances")
        zero = (
            zero_current
            and type(appearances) is int
            and appearances == 0
            and row["minutes"] == row["starts"] == 0
        )
        records[number] = {
            "player_id": chosen or external,
            "name": labels[0],
            "names": labels,
            "native_name": native_names[number],
            "team": team,
            "season": season,
            "position": {
                "GK": "goalkeeper",
                "DEF": "defender",
                "MID": "midfielder",
                "FWD": "forward",
            }[row["position"]],
            "provider_player_id": number,
            "possible_roster_ids": sorted(alternatives),
            "seen_at": row["known_at"],
            "source_url": news_minutes.source_url(team),
            "original_sha256": row["original_sha256"],
            "provider_as_of": row["provider_as_of"],
            "received_at": row["received_at"],
            "minutes": row["minutes"],
            "starts": row["starts"],
            "zero_appearances": zero,
            "blocked": blocked,
            "identity_basis": "reviewed_provider_alias"
            if reviewed
            else "touchline_id_to_roster"
            if chosen
            else "touchline_directory_id",
            **({"alias_receipt": reviewed["digest"]} if reviewed else {}),
        }
    bound = Counter(rec["player_id"] for rec in records.values() if not rec["blocked"])
    for rec in records.values():
        if bound[rec["player_id"]] > 1:
            rec["blocked"] = True
    return records


def signature(records):
    # Re-parse the original for each lookup, but run extraction again only
    # when the facts that can change an identity decision actually change.
    return [
        [
            k,
            v["player_id"],
            v["names"],
            v["native_name"],
            v["position"],
            v["blocked"],
            v["zero_appearances"],
            v.get("alias_receipt"),
            v["possible_roster_ids"],
        ]
        for k, v in sorted(records.items())
    ]


def bind(original, aliases, direct, records):
    """Return a grounded claimant or a replayable reason for abstaining."""
    row = records.get(original.get("provider_player_id"))
    if row is None:
        return direct, None
    fields = original["source_fields"]
    spellings = [fields.get(k) for k in ("name", "display_name")]
    full = _full_names(spellings)
    dnames = _full_names(row["names"])
    direct_id = direct[0]["player_id"] if direct else None
    # A shirt surname alone cannot override a conflicting full name.
    tokens = {news_ai._name_key(s) for s in spellings if isinstance(s, str)}
    directory_tokens = {news_ai._name_key(s) for s in row["names"]}
    wire_name = fields.get("name")
    primary = news_ai._name_key(wire_name) if isinstance(wire_name, str) else ()
    native = (
        news_ai._name_key(row["native_name"])
        if isinstance(row["native_name"], str)
        else ()
    )
    named = set().union(*(aliases.get(k, set()) for k in full or tokens))
    if len(primary) > 1 and len(native) > 1:
        agreement = primary == native
    elif full and dnames:
        agreement = bool(full & dnames)
    else:
        agreement = bool(tokens & directory_tokens)
    if (
        not agreement
        or row["blocked"]
        or (named and named != {row["player_id"]})
        or (direct_id and direct_id != row["player_id"])
    ):
        return [], None
    if direct:
        return direct, None
    if row["zero_appearances"] and row["identity_basis"] == "touchline_directory_id":
        return [], row
    # wire_claim supplies the same typed labels and quote used by the ordinary
    # roster path. Do not mix names from the directory into the text extractor.
    confirmed = {
        news_ai._name_key(name): {row["player_id"]}
        for name in spellings
        if isinstance(name, str)
    }
    return news_sources.wire_claim(original, confirmed), row


def proof(row, original_sha256):
    return {
        k: row[k]
        for k in (
            "provider_player_id",
            "player_id",
            "possible_roster_ids",
            "identity_basis",
            "name",
            "position",
            "team",
            "season",
            "original_sha256",
            "provider_as_of",
            "seen_at",
            "received_at",
            "minutes",
            "starts",
            "zero_appearances",
        )
    } | {
        "availability_original_sha256": original_sha256,
        **({"alias_receipt": row["alias_receipt"]} if row.get("alias_receipt") else {}),
    }


def revalidates(root, evidence, records, aliases):
    try:
        doc = json.loads(
            store.read_blob(root, evidence["availability_original_sha256"])
        )
        claims, current = bind(
            doc, aliases, news_sources.wire_claim(doc, aliases), records
        )
        if current is None:
            return False
        if current["original_sha256"] != evidence["original_sha256"]:
            # A later directory can corroborate membership but cannot repair
            # a missing original used for the recorded identity decision.
            stamp, members = news_minutes.parse(
                store.read_blob(root, evidence["original_sha256"]),
                evidence["team"],
                evidence["season"],
                evidence["received_at"],
            )
            initial = next(
                (p for p in members if p["id"] == evidence["provider_player_id"]), None
            )
            if (
                stamp != evidence["provider_as_of"]
                or initial is None
                or (initial["minutes"], initial["starts"])
                != (evidence["minutes"], evidence["starts"])
                or (evidence["zero_appearances"] and initial.get("appearances") != 0)
            ):
                return False
        was_exempt = (
            evidence["zero_appearances"]
            and evidence["identity_basis"] == "touchline_directory_id"
        )
        now_exempt = (
            current["zero_appearances"]
            and current["identity_basis"] == "touchline_directory_id"
        )
        return (
            was_exempt == now_exempt
            and current["player_id"] == evidence["player_id"]
            and current["provider_player_id"] == evidence["provider_player_id"]
            and current["identity_basis"] == evidence["identity_basis"]
            and current.get("alias_receipt") == evidence.get("alias_receipt")
            and (
                bool(claims)
                or (evidence["zero_appearances"] and current["zero_appearances"])
            )
        )
    except (ValueError, TypeError, KeyError, OSError):
        return False


def _alias_proof(db, root, season, team, decision, current_names):
    try:
        _, native = news_minutes.parse(
            store.read_blob(root, decision["directory_sha256"]),
            team,
            season,
            decision["directory_received_at"],
        )
        prior = next(p for p in native if p["id"] == decision["provider_id"])
        existing = {news_ai._name_key(n) for n in prior["names"]}
        current = {news_ai._name_key(n) for n in current_names}
        record = db.execute(
            "SELECT document FROM news_roster_content WHERE digest=?",
            (decision["roster_sha256"],),
        ).fetchone()
        if not record:
            return False
        doc = json.loads(record[0])
        return (
            bool(current & existing)
            and store.digest(doc) == decision["roster_sha256"]
            and doc["season"] == season
            and football.team_key(doc["team"]) == football.team_key(team)
            and any(p["player_id"] == decision["player_id"] for p in doc["players"])
        )
    except (OSError, ValueError, TypeError, KeyError, StopIteration):
        return False


def _aliases_at(db, season, club, at):
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='news_player_aliases'"
    ).fetchone():
        return {}
    result = {}
    for rec in db.execute(
        "SELECT provider_id,known_at,digest,document FROM (SELECT *,row_number() OVER(PARTITION BY provider_id ORDER BY known_at DESC,rowid DESC) n "
        "FROM news_player_aliases WHERE season=? AND club=? AND known_at<=?) WHERE n=1",
        (season, club, at),
    ):
        doc = json.loads(rec["document"])
        if (
            store.digest(doc) == rec["digest"]
            and not doc["withdrawn"]
            and (doc["season"], doc["club"], doc["provider_id"], doc["known_at"])
            == (season, club, rec["provider_id"], rec["known_at"])
        ):
            result[doc["provider_id"]] = doc | {"digest": rec["digest"]}
    return result


def register_alias(
    root, team, season, provider_id, player_id, reviewer, reason, *, withdraw=False
):
    """Record a named, forward-only decision tied to the actual squad and directory originals."""
    from . import news

    if (
        type(provider_id) is not int
        or provider_id < 1
        or not all(
            isinstance(value, str) and value.strip()
            for value in (player_id, reviewer, reason)
        )
    ):
        raise ValueError(
            "a positive provider ID, roster player ID, reviewer and reason are required"
        )
    with news._db(root, write=True) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS news_player_aliases (
            season TEXT NOT NULL, club TEXT NOT NULL, provider_id INTEGER NOT NULL, known_at TEXT NOT NULL,
            digest TEXT UNIQUE NOT NULL, document TEXT NOT NULL)""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS news_alias_asof ON news_player_aliases(season,club,known_at)"
        )
        at, club = store.iso(store.now_utc()), football.team_key(team)
        active = _aliases_at(db, season, club, at)
        if withdraw:
            if (
                provider_id not in active
                or active[provider_id]["player_id"] != player_id
            ):
                raise ValueError("no matching active alias to withdraw")
            refs = {"previous_alias": active[provider_id]["digest"]}
        else:
            if any(
                v["player_id"] == player_id and k != provider_id
                for k, v in active.items()
            ):
                raise ValueError(
                    "the roster player already has another active provider ID"
                )
            if provider_id in active:
                if active[provider_id]["player_id"] == player_id:
                    return active[provider_id]
                raise ValueError(
                    "withdraw the existing alias before changing the player"
                )
            game = next(
                (
                    g
                    for g in news._schedule(db, at, season)
                    if g["kickoff"] > at
                    and club
                    in (football.team_key(g["home"]), football.team_key(g["away"]))
                ),
                None,
            )
            squad = news_roster.roster_views(db, [team], season, at)[club]
            if not game or player_id not in {p["player_id"] for p in squad["players"]}:
                raise ValueError(
                    "a scheduled fixture and a unique current roster player are required"
                )
            candidates = directory(db, root, game, team, at, squad["players"])
            row = candidates.get(provider_id)
            if (
                not row
                or row["blocked"]
                or row["identity_basis"] == "touchline_id_to_roster"
                and row["player_id"] != player_id
            ):
                raise ValueError(
                    "no verified unconflicted provider directory row for that roster player"
                )
            refs = {
                "directory_sha256": row["original_sha256"],
                "directory_received_at": row["received_at"],
                "roster_sha256": squad["digest"],
            }
        doc = {
            "season": season,
            "club": club,
            "provider_id": provider_id,
            "player_id": player_id,
            "known_at": at,
            "reviewer": reviewer.strip(),
            "reason": reason.strip(),
            "withdrawn": withdraw,
            **refs,
        }
        digest = store.digest(doc)
        db.execute(
            "INSERT INTO news_player_aliases VALUES(?,?,?,?,?,?)",
            (season, club, provider_id, at, digest, store.canonical(doc)),
        )
        # Permit re-analysis of the archived input at the next service pass without
        # making a second network request before the publisher's cache expires.
        db.execute(
            "UPDATE news_local_work SET retry_at=? WHERE season=? AND team_key=? AND source=?",
            (at, season, club, news_sources.WIRE),
        )
        return doc | {"digest": digest}
