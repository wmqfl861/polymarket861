"""Count EPL rule and fee coverage in a captured Polymarket season catalogue."""

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date
from pathlib import Path

from polymarket861 import ledger_terms, market_provider


def measure(root: Path):
    sql = """SELECT slug, fixture, sha, received_at FROM (
        SELECT *, ROW_NUMBER() OVER (PARTITION BY slug ORDER BY received_at DESC, sha DESC) AS version
        FROM ma_events
    ) WHERE version = 1 ORDER BY received_at DESC, sha DESC, slug"""
    by_source = defaultdict(dict)
    with closing(
        sqlite3.connect(
            (root / "runtime.sqlite3").resolve().as_uri() + "?mode=ro", uri=True
        )
    ) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute(sql):
            by_source[row["sha"]][row["slug"]] = row

    counts = defaultdict(Counter)
    unrecognized = defaultdict(Counter)
    fixtures = defaultdict(set)
    seen = set()
    for digest, requested in by_source.items():
        raw = (root / "archive" / digest[:2] / digest).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(f"catalogue SHA256 mismatch for {digest}")
        payload = json.loads(raw)
        for event in payload if isinstance(payload, list) else [payload]:
            slug = event.get("slug")
            if slug not in requested:
                continue
            row = requested[slug]
            match_date = date.fromisoformat("-".join(slug.split("-")[3:6]))
            first_year = match_date.year - int(match_date.month < 7)
            season = f"{first_year}-{str(first_year + 1)[2:]}"
            fixtures[season].add(row["fixture"])
            for market in market_provider.normalized_markets(event):
                condition = market["condition_id"]
                if market["mapping_error"] or condition in seen:
                    continue
                seen.add(condition)
                stats = counts[season]
                stats["mapped_conditions"] += 1
                market.update(
                    event_teams=event["teams"],
                    source_sha256=digest,
                    recorded_at=row["received_at"],
                )
                try:
                    ledger_terms._interpret(
                        market, market["tokens"][0]["outcome"].casefold()
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    unrecognized[season][str(exc)] += 1
                    continue
                stats["interpreted_rules"] += 1
                if market["fees_enabled"] is True:
                    stats["interpreted_fee_enabled"] += 1
                elif market["fees_enabled"] is False:
                    ledger_terms._fee(market)
                    stats["verified_fee_disabled"] += 1

    return {
        "scope": "Latest raw Gamma event snapshots in this catalogue. Counts are distinct mapped conditions. This check measures published wording and the fee switch. It does not establish pregame book depth or executable fills.",
        "latest_catalogue_receipt": max(
            row["received_at"] for rows in by_source.values() for row in rows.values()
        )
        if by_source
        else None,
        "seasons": {
            season: {
                "fixtures": len(fixtures[season]),
                **{
                    key: stats[key]
                    for key in (
                        "mapped_conditions",
                        "interpreted_rules",
                        "interpreted_fee_enabled",
                        "verified_fee_disabled",
                    )
                },
                "unrecognized_rules": dict(sorted(unrecognized[season].items())),
            }
            for season, stats in sorted(counts.items())
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("catalogue", type=Path)
    print(
        json.dumps(measure(parser.parse_args().catalogue), indent=2, ensure_ascii=False)
    )
