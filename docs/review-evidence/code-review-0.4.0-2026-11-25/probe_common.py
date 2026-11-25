"""Shared offline scaffolding for the 0.4.0 review probes.

Nothing here talks to the network. The Wikimedia- and Touchline-shaped payloads follow the
shapes used by the repository's own tests (tests/test_news_roster.py, tests/test_news_sources.py).
Run with the package importable, e.g. `PYTHONPATH=src python docs/review-evidence/.../probe_*.py`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

from polymarket861 import news_roster, news_sources, store
from polymarket861.market_provider import Reply

SEASON = "2026-27"
ROLE_CATEGORY = {
    "goalkeeper": "Category:Men's association football goalkeepers",
    "defender": "Category:Men's association football defenders",
    "midfielder": "Category:Men's association football midfielders",
    "forward": "Category:Men's association football forwards",
}
WIRE_LICENSE = "Free for personal, academic, and open-source use with attribution"


class Clock:
    """A controllable replacement for store.now_utc."""

    def __init__(self, at):
        self.at = store.instant(at)

    def __call__(self):
        return store.iso(self.at)

    def advance(self, **delta):
        self.at += timedelta(**delta)
        return store.iso(self.at)


class FakeWiki:
    """Squad template + identity lookups for one club. `members` maps name -> role or None."""

    def __init__(self, clock, members, base_id):
        self.clock = clock
        self.members = dict(members)
        self.ids = {name: base_id + i for i, name in enumerate(self.members)}

    def __call__(self, url):
        args = parse_qs(urlsplit(url).query)
        self.clock.advance(seconds=2)
        if args["prop"] == ["revisions"]:
            (title,) = args["titles"]
            club = title.removeprefix("Template:").removesuffix(" squad")
            rows = "\n".join(
                f"{{{{football squad2 player|no={i + 1}|name=[[{n}]]}}}}"
                for i, n in enumerate(self.members)
            )
            body = f"{{{{football squad|teamname={club}|list=<div>\n{rows}\n</div>}}}}"
            revision = {
                "revid": 81,
                "timestamp": "2026-07-17T09:00:00Z",
                "slots": {"main": {"contentmodel": "wikitext", "content": body}},
            }
            result = {
                "query": {
                    "rightsinfo": {"url": news_roster.wiki.LICENSE},
                    "pages": [{"ns": 10, "title": title, "revisions": [revision]}],
                }
            }
        else:
            pages = []
            for title in args["titles"][0].split("|"):
                page = {
                    "title": title,
                    "ns": 0,
                    "pageid": self.ids[title],
                    "pageprops": {"wikibase_item": f"Q{self.ids[title]}"},
                }
                role = self.members.get(title)
                if role:
                    page["categories"] = [{"ns": 14, "title": ROLE_CATEGORY[role]}]
                pages.append(page)
            result = {"query": {"pages": pages}}
        return json.dumps(result).encode()


def squad(prefix, size=25):
    """A plausible 25-man squad: 3 GK, 8 DF, 8 MF, 6 FW. Names are synthetic."""
    roles = ["goalkeeper"] * 3 + ["defender"] * 8 + ["midfielder"] * 8 + ["forward"] * 6
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    return {f"{prefix} Player{letters[i]}": roles[i] for i in range(size)}


def register(root, clock, fixture_id, home, away, kickoff, round_id=12, source="probe"):
    return store.register_fixture(
        root,
        {
            "fixture_id": fixture_id,
            "season": SEASON,
            "round_id": round_id,
            "home": home,
            "away": away,
            "known_at": clock(),
            "kickoff": kickoff,
            "source": source,
            "locator": "probe:" + fixture_id + ":" + kickoff,
        },
    )


def availability_payload(team_label, rows, at):
    """Touchline Wire /api/v1/injuries shape. `rows` = [(player_id, name, status, category, chance)]."""
    data = [
        {
            "player_id": pid,
            "display_name": name,
            "team": team_label,
            "status": status,
            "category": category,
            "chance_of_playing": chance,
        }
        for pid, name, status, category, chance in rows
    ]
    return {
        "meta": {
            "source": "Touchline Wire Tactical API",
            "timestamp_iso": at,
            "attribution_url": news_sources.WIRE_HOST,
            "count": len(data),
            "license": WIRE_LICENSE,
        },
        "data": data,
    }


def reply(payload, at, url, max_age=180):
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return Reply(raw, at, at, {"Cache-Control": f"max-age={max_age}"}, url)


def table_counts(root, tables):
    from contextlib import closing

    with closing(store.connect(root)) as db:
        out = {}
        for table in tables:
            try:
                out[table] = db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except Exception:
                out[table] = None
        return out


def disk_usage(root):
    files = size = 0
    for path in root.rglob("*"):
        if path.is_file():
            files += 1
            size += path.stat().st_size
    return files, size


def checkpoint(root):
    """Fold the write-ahead log into the database file so sizes are comparable between polls."""
    from contextlib import closing

    with closing(store.connect(root)) as db:
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
