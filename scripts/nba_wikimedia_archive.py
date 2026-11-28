"""Rebuild the optional technical NBA score archive from revisioned team game logs."""

import hashlib
import json
import time
import re
from datetime import date, datetime
from pathlib import Path

import httpx

TEAMS = dict(
    line.split(" ", 1)
    for line in """ATL Atlanta Hawks
BOS Boston Celtics
BKN Brooklyn Nets
CHA Charlotte Hornets
CHI Chicago Bulls
CLE Cleveland Cavaliers
DAL Dallas Mavericks
DEN Denver Nuggets
DET Detroit Pistons
GSW Golden State Warriors
HOU Houston Rockets
IND Indiana Pacers
LAC Los Angeles Clippers
LAL Los Angeles Lakers
MEM Memphis Grizzlies
MIA Miami Heat
MIL Milwaukee Bucks
MIN Minnesota Timberwolves
NOP New Orleans Pelicans
NYK New York Knicks
OKC Oklahoma City Thunder
ORL Orlando Magic
PHI Philadelphia 76ers
PHX Phoenix Suns
POR Portland Trail Blazers
SAC Sacramento Kings
SAS San Antonio Spurs
TOR Toronto Raptors
UTA Utah Jazz
WAS Washington Wizards""".splitlines()
)
API = "https://en.wikipedia.org/w/api.php"


def fetch(directory, pinned=None):
    directory.mkdir(parents=True, exist_ok=True)
    manifest = []
    client = httpx.Client(
        headers={
            "User-Agent": "polymarket861-archive/0.6 (https://github.com/wmqfl861/polymarket861; offline score validation)"
        },
        timeout=45,
        follow_redirects=True,
    )
    with client:
        for year in range(2020, 2026):
            names = list(TEAMS.items())
            for offset in range(0, len(names), 5):
                batch = names[offset : offset + 5]
                pending = [
                    (code, name)
                    for code, name in batch
                    if not (directory / f"{year}-{code}.json").exists()
                ]
                if pending:
                    params = {
                        "action": "query",
                        "prop": "revisions",
                        "rvprop": "ids|timestamp|content",
                        "rvslots": "main",
                        "format": "json",
                        "formatversion": 2,
                    }
                    titles = {
                        f"{year}–{(year + 1) % 100:02d} {name} season": code
                        for code, name in pending
                    }
                    if pinned:
                        params["revids"] = "|".join(
                            str(pinned[year, code]) for code, _ in pending
                        )
                    else:
                        params.update(redirects=1, titles="|".join(titles))
                    for attempt in range(5):
                        try:
                            reply = client.get(API, params=params)
                            reply.raise_for_status()
                            query = reply.json()["query"]
                            break
                        except (httpx.HTTPError, ValueError, KeyError):
                            if attempt == 4:
                                raise
                            time.sleep(2**attempt)
                    for redirect in query.get("redirects", []):
                        titles[redirect["to"]] = titles[redirect["from"]]
                    for page in query["pages"]:
                        rev = page["revisions"][0]
                        content = rev["slots"]["main"]["content"]
                        code = titles.get(page["title"]) or next(
                            c
                            for c, _ in pending
                            if pinned and pinned[year, c] == rev["revid"]
                        )
                        row = {
                            "year": year,
                            "team": code,
                            "title": page["title"],
                            "revision": rev["revid"],
                            "timestamp": rev["timestamp"],
                            "sha256": hashlib.sha256(content.encode()).hexdigest(),
                        }
                        (directory / f"{year}-{code}.json").write_text(
                            json.dumps({**row, "content": content}, ensure_ascii=False)
                        )
                    time.sleep(0.3)
                for code, _ in batch:
                    row = json.loads((directory / f"{year}-{code}.json").read_text())
                    row.pop("content")
                    manifest.append(row)
    return manifest


MONTH = r"(?:October|November|December|January|February|March|April|May)"
ALIASES = {
    word.casefold(): code
    for code, name in TEAMS.items()
    for word in (code, name, name.rsplit(" ", 1)[1], name.rsplit(" ", 1)[0])
    if word != "Los Angeles"
}
ALIASES.update(
    {
        "la clippers": "LAC",
        "la lakers": "LAL",
        "portland": "POR",
        "blazers": "POR",
        "trail blazers": "POR",
        "golden state": "GSW",
    }
)


def opponent(cell):
    link = re.search(r"\[\[[^|\]]+ season\|", cell, re.I)
    if link:
        for code, name in TEAMS.items():
            if name.casefold() in link[0].casefold():
                return code
    plain = re.sub(r"<!--.*?-->|<[^>]*>|\{\{[^}]*}}", "", cell)
    plain = re.sub(r"\[\[(?:[^|]*\|)?([^]\|]+)]]", r"\1", plain)
    plain = re.sub(r"^(?:@|at |vs\.? )\s*", "", plain.strip(), flags=re.I)
    return ALIASES.get(
        plain.strip(" .'").replace(".", "").replace("L A ", "LA ").casefold()
    )


def perspectives(page):
    """Return numbered finals from regular-season game logs, with designated sides."""
    year, team, content = page["year"], page["team"], page["content"]
    output = set()
    headings = list(re.finditer(r"(?m)^(={2,})([^=\n]+)\1[ \t]*$", content))
    for i, match in enumerate(headings):
        if match[2].lower().strip() != "regular season":
            continue
        end = next(
            (m.start() for m in headings[i + 1 :] if len(m[1]) <= len(match[1])),
            len(content),
        )
        for row in re.split(r"(?m)^\|-", content[match.end() : end]):
            cells = [
                s.lstrip("|").strip() for s in row.splitlines() if s.startswith("|")
            ]
            if len(cells) < 4 or not re.fullmatch(
                r"(?:(?:scope|style)=[^|]+\|\s*)?\d{1,2}", cells[0]
            ):
                continue
            match_date = re.search(rf"({MONTH})\.?\s+(\d{{1,2}})\b", cells[1], re.I)
            if match_date:
                month, day = match_date.groups()
            else:
                reverse = re.search(rf"\b(\d{{1,2}})\s+({MONTH})", cells[1], re.I)
                if not reverse:
                    continue
                day, month = reverse.groups()
            month = datetime.strptime(month, "%B").month
            game_day = date(year + int(month < 8), month, int(day)).isoformat()
            opp = opponent(cells[2])
            if opp is None or opp == team:
                continue
            pf = re.search(r"(?i)\|\s*pf\s*=\s*(\d+)", cells[3])
            pa = re.search(r"(?i)\|\s*pa\s*=\s*(\d+)", cells[3])
            scores = (int(pf[1]), int(pa[1])) if pf and pa else None
            if not scores:
                plain = re.search(r"\b(\d{2,3})\s*[–−-]\s*(\d{2,3})\b", cells[3])
                if plain:
                    scores = int(plain[1]), int(plain[2])
            if not scores or scores[0] == scores[1]:
                continue
            away = bool(re.search(r"@|^at\b", cells[2], re.I))
            home, road, h, a = (
                (opp, team, *reversed(scores)) if away else (team, opp, *scores)
            )
            output.add((game_day, home, road, h, a))
    return sorted(output)


def build(cache, output, pins):
    import csv
    from collections import Counter, defaultdict

    manifest = fetch(
        cache, {(p["year"], p["team"]): p["revision"] for p in pins} if pins else None
    )
    if pins and manifest != pins:
        raise ValueError(
            "page revision/contents differ from pinned source manifest; use an empty cache"
        )
    groups = defaultdict(lambda: defaultdict(set))
    for p in manifest:
        page = json.loads((cache / f"{p['year']}-{p['team']}.json").read_text())
        if hashlib.sha256(page["content"].encode()).hexdigest() != p["sha256"]:
            raise ValueError("cached wikitext does not match its recorded digest")
        for day, home, away, hs, vs in perspectives(page):
            groups[p["year"], day, *sorted((home, away))][p["team"]].add(
                (home, away, hs, vs)
            )
    matches = []
    disputed = []
    for (season, day, t1, t2), views in sorted(groups.items()):
        if set(views) == {t1, t2} and len(views[t1]) == 1 and views[t1] == views[t2]:
            ((home, away, hs, vs),) = views[t1]
            matches.append((season, day, home, away, hs, vs))
        else:
            disputed.append(
                {
                    "season": season,
                    "date": day,
                    "teams": [t1, t2],
                    "views": {t: sorted(entries) for t, entries in views.items()},
                }
            )
    starts = Counter((day, team) for _, day, h, a, _, _ in matches for team in (h, a))
    if max(starts.values()) > 1:
        raise ValueError("a team has two accepted games on one date")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "agreed-finals.csv").open("w", newline="") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(("season", "date", "home", "away", "home_score", "away_score"))
        w.writerows(matches)
    (output / "wikipedia-revisions.json").write_text(
        json.dumps(manifest, indent=1, ensure_ascii=False) + "\n"
    )
    (output / "withheld.json").write_text(
        json.dumps(disputed, separators=(",", ":")) + "\n"
    )
    return len(matches)


def verified_finals(directory):
    import csv
    from collections import Counter

    cross = directory / "crosschecks"
    checks = json.loads((cross / "counts.json").read_text())
    for name, expected in checks["baseline_source_sha256"].items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"English base archive changed: {name}")

    def read(path):
        with path.open(newline="") as f:
            for r in csv.DictReader(f):
                key = (int(r["season"]), r["date"], *sorted((r["home"], r["away"])))
                yield (
                    key,
                    (r["home"], r["away"], int(r["home_score"]), int(r["away_score"])),
                )

    records = dict(read(directory / "agreed-finals.csv"))
    for key, original in read(cross / "withdraw.csv"):
        if records.pop(key, None) != original:
            raise ValueError(f"withdrawal did not match its original row: {key}")
    for key, final in read(cross / "resolved.csv"):
        records[key] = final
    canonical = "\n".join(
        ",".join(map(str, (year, day, *values)))
        for (year, day, *_), values in sorted(records.items())
    )
    checksum = hashlib.sha256(canonical.encode()).hexdigest()
    if checksum != checks["canonical_final_facts_sha256"]:
        raise ValueError(
            "adjudicated scores differ from the independently verified signature"
        )
    teams = Counter((year, team) for (year, day, *pair) in records for team in pair)
    starts = Counter((day, team) for (year, day, *pair) in records for team in pair)
    if (
        len(teams) != 180
        or max(starts.values()) != 1
        or any(
            teams[season, team] != (72 if season == 2020 else 82)
            for season in range(2020, 2026)
            for team in TEAMS
        )
    ):
        raise ValueError("incomplete/duplicate team-season schedule")
    rows = [
        {
            "source": "revisioned_historical_scores",
            "game_date": day,
            "season": year,
            "game_id": "/".join((day, home, away)),
            "home": home,
            "away": away,
            "home_abbr": home,
            "away_abbr": away,
            "home_score": hs,
            "away_score": vs,
            "stage": "regular",
            "status": "final",
            "neutral": None,
        }
        for (year, day, *_), (home, away, hs, vs) in sorted(records.items())
    ]
    return rows, checksum


def export(directory, path):
    import csv

    rows, checksum = verified_finals(directory)
    with path.open("w", newline="") as output:
        keys = ("season", "game_date", "home", "away", "home_score", "away_score")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(("season", "date", *keys[2:]))
        writer.writerows(tuple(r[k] for k in keys) for r in rows)
    return checksum


def study(directory, lag, replicates):
    from collections import Counter
    from polymarket861.nba.model_replay import backtest

    rows, checksum = verified_finals(directory)
    result = backtest(
        rows, first_season=2021, last_season=2025, lag_days=lag, replicates=replicates
    )
    counts = Counter(x["season"] for x in rows)
    result["archive"] = {
        "source": "Pinned English and French team-season records, licensed FiveThirtyEight archive and individually adjudicated disputed score facts",
        "revision_manifest_sha256": hashlib.sha256(
            (directory / "wikipedia-revisions.json").read_bytes()
        ).hexdigest(),
        "sha256": checksum,
        "warmup_starting_season": 2020,
        "observed_finals_by_season": dict(sorted(counts.items())),
        "evaluated_full_schedule": 6150,
        "excluded_or_missing": 6150 - result["pooled"]["games"],
        "limitations": "Complete regular-season score counts. Historic sources lack the original receipt times; both team pages can share a transcription error. No forward market or news receipts.",
    }
    (directory / f"study-{lag * 24}h.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n"
    )
    return result["pooled"]["losses"]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    collect = actions.add_parser(
        "build",
        help="download isolated research cache and cross-check both team game logs",
    )
    collect.add_argument("--cache", type=Path, required=True)
    collect.add_argument(
        "--pins", type=Path, help="revision manifest; omit only to select a new archive"
    )
    run = actions.add_parser(
        "study", help="replay the agreed score facts with frozen product parameters"
    )
    run.add_argument("--lag-days", type=int, choices=(1, 2), required=True)
    run.add_argument("--replicates", type=int, default=10000)
    export_cmd = actions.add_parser(
        "export", help="write the checked, adjudicated final scores as CSV"
    )
    export_cmd.add_argument("--output", type=Path, required=True)
    for sub in (collect, run, export_cmd):
        sub.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "build":
        print(
            build(
                args.cache,
                args.directory,
                json.loads(args.pins.read_text()) if args.pins else None,
            )
        )
    elif args.action == "export":
        print(export(args.directory, args.output))
    else:
        print(
            json.dumps(study(args.directory, args.lag_days, args.replicates), indent=2)
        )
