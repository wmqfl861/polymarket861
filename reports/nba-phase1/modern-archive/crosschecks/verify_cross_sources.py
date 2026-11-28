"""Verify each English/French/538 citation and optionally the full reconciled finals file.

Use a separate private --cache directory. Missing originals are fetched only with --fetch.
"""

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BUNDLE = Path(__file__).resolve().parent
REDIVIS = "https://redivis.com/api/v1/tables/datapages.irw_competitions:v7_2.fivethirtyeight_nba_1946_2023/rows?format=csv"
DIGEST_538 = "46814d80064bf7f7a80eb809b04a8d182cf4c6f53ab9c09f96f14e0e44fa625f"
AGENT = "polymarket861-archive/0.6 (https://github.com/wmqfl861/polymarket861; historical score verification)"


def load(path):
    return json.loads(path.read_bytes())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def download(url):
    for attempt in range(4):
        try:
            with urlopen(
                Request(url, headers={"User-Agent": AGENT}), timeout=45
            ) as response:
                return response.read()
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def fetch_wiki(pins, cache):
    for lang in ("en", "fr"):
        directory = cache / lang
        directory.mkdir(exist_ok=True, parents=True)
        needed = [
            p
            for p in pins
            if p["lang"] == lang
            and not (directory / f"{p['year']}-{p['team']}.json").is_file()
        ]
        for start in range(0, len(needed), 5):
            batch = {p["revision"]: p for p in needed[start : start + 5]}
            query = urlencode(
                {
                    "action": "query",
                    "format": "json",
                    "formatversion": 2,
                    "prop": "revisions",
                    "rvprop": "ids|timestamp|content",
                    "rvslots": "main",
                    "revids": "|".join(map(str, batch)),
                }
            )
            pages = json.loads(
                download(f"https://{lang}.wikipedia.org/w/api.php?{query}")
            )["query"]["pages"]
            for page in pages:
                for rev in page["revisions"]:
                    pin = batch[rev["revid"]]
                    target = directory / f"{pin['year']}-{pin['team']}.json"
                    content = rev["slots"]["main"]["content"]
                    require(
                        hashlib.sha256(content.encode()).hexdigest() == pin["sha256"],
                        "downloaded page digest differs",
                    )
                    target.write_text(
                        json.dumps({**pin, "content": content}, ensure_ascii=False)
                    )
            time.sleep(0.35)


def archive_538(path):
    require(
        hashlib.sha256(path.read_bytes()).hexdigest() == DIGEST_538,
        "FiveThirtyEight redistribution digest differs",
    )
    names = {"CHO": "CHA", "BRK": "BKN", "PHO": "PHX"}
    facts = set()
    for r in read_csv(path):
        year = int(r["season"]) - 1
        if not 2020 <= year <= 2022 or r["playoff"]:
            continue
        h, a = [names.get(r[k], r[k]) for k in ("agent_a", "agent_b")]
        hs, vs = int(r["score_a"]), int(r["score_b"])
        require(
            r["homefield"] in ("agent_a", "")
            and (hs > vs) == (r["winner"] == "agent_a"),
            "invalid FiveThirtyEight row",
        )
        day = datetime.fromtimestamp(int(r["date"]), timezone.utc).date().isoformat()
        facts.add((year, day, h, a, hs, vs))
    require(
        Counter(r[0] for r in facts) == {2020: 1080, 2021: 1230, 2022: 1230},
        "incomplete FiveThirtyEight seasons",
    )
    return facts


months = {
    "janvier": 1,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
}
aliases = {
    "BRK": "BKN",
    "PHO": "PHX",
    "CHO": "CHA",
    "NOH": "NOP",
    "UTH": "UTA",
    "NY": "NYK",
    "GS": "GSW",
    "SA": "SAS",
    "NO": "NOP",
}
franchise = dict(
    t.split(" ", 1)
    for t in """ATL Hawks
BOS Celtics
BKN Nets
CHA Hornets
CHI Bulls
CLE Cavaliers
DAL Mavericks
DEN Nuggets
DET Pistons
GSW Warriors
HOU Rockets
IND Pacers
LAC Clippers
LAL Lakers
MEM Grizzlies
MIA Heat
MIL Bucks
MIN Timberwolves
NOP Pelicans
NYK Knicks
OKC Thunder
ORL Magic
PHI 76ers
PHX Suns
POR Trail Blazers
SAC Kings
SAS Spurs
TOR Raptors
UTA Jazz
WAS Wizards""".splitlines()
)


def rows(page):
    text = page["content"]
    year, team = page["year"], page["team"]
    headings = list(re.finditer(r"(?m)^(={2,})\s*([^=\n]+?)\s*\1\s*$", text))
    for i, h in enumerate(headings):
        if h[2].strip().casefold() != "saison régulière":
            continue
        end = next(
            (y.start() for y in headings[i + 1 :] if len(y[1]) <= len(h[1])), len(text)
        )
        for line in text[h.end() : end].splitlines():
            cols = line.split("||")
            if len(cols) < 5 or not re.match(r"^\|\s*\d{1,2}\s*$", cols[0]):
                continue
            m = re.search(
                r"(?i)([0-3]?\d|1er)\s*(?:\||\s)\s*(" + "|".join(months) + r")\b",
                cols[1],
            )
            result = re.search(r"\b([VD])\s+(\d{2,3})\s*[-–]\s*(\d{2,3})\b", cols[3])
            pdf = re.search(
                r"https?://[^\s\]]+/(\d{8})_([A-Z]{4,6})\.pdf\b", cols[3], re.I
            )
            game = re.search(
                r"https?://(?:fr\.global\.nba\.com/boxscore/#!/|(?:www\.)?nba\.com/game/(?:[a-z-]*))00[2]\d{7}",
                cols[3],
                re.I,
            )
            if not (m and result and (pdf or game)):
                continue
            month = months[m[2].lower()]
            day = int(re.match(r"\d+", m[1])[0])
            when = date(year + int(month < 8), month, day)
            opponents = [
                t
                for t, n in franchise.items()
                if re.search(r"\b" + re.escape(n) + r"\b", cols[2], re.I)
            ]
            if len(opponents) != 1 or opponents[0] == team:
                continue
            opponent = opponents[0]
            away, home = (team, opponent) if "@" in cols[2] else (opponent, team)
            if pdf:
                if when.strftime("%Y%m%d") != pdf[1]:
                    continue
                combined = pdf[2].upper()
                pairs = {
                    (
                        aliases.get(combined[:j], combined[:j]),
                        aliases.get(combined[j:], combined[j:]),
                    )
                    for j in range(2, 4)
                }
                if (away, home) not in pairs:
                    continue
            reference = (pdf or game)[0]
            scored, allowed = int(result[2]), int(result[3])
            if (scored > allowed) != (result[1] == "V") or scored == allowed:
                continue
            scores = (scored, allowed) if home == team else (allowed, scored)
            yield (
                (year, when.isoformat(), tuple(sorted((home, away)))),
                (home, away, *scores),
                reference,
            )


def fact(r):
    return (
        int(r["season"]),
        r["date"],
        r["home"],
        r["away"],
        int(r["home_score"]),
        int(r["away_score"]),
    )


def identity(value):
    return value[0], value[1], tuple(sorted(value[2:4]))


def check_full(values, sources):
    by_id = {identity(v): v for v in values}
    require(len(by_id) == len(values), "duplicate game identity")
    team_seasons = Counter((v[0], t) for v in values for t in v[2:4])
    dates = Counter((v[1], t) for v in values for t in v[2:4])
    require(
        len(team_seasons) == 180 and max(dates.values()) == 1,
        "team/date duplication or missing team-season",
    )
    require(
        all(n == (72 if year == 2020 else 82) for (year, _), n in team_seasons.items()),
        "incomplete team season",
    )
    canonical = "\n".join(",".join(map(str, by_id[k])) for k in sorted(by_id))
    require(
        hashlib.sha256(canonical.encode()).hexdigest()
        == load(BUNDLE / "counts.json")["canonical_final_facts_sha256"],
        "full corrected finals digest differs",
    )
    require(
        all(v in by_id.values() for v in sources),
        "a corrected game is absent from full finals",
    )
    for r in load(BUNDLE / "discarded-claims.json"):
        require(
            (r["season"], r["withheld_date"], tuple(r["teams"])) not in by_id,
            "discarded English date exists",
        )
        require(
            any(
                (r["season"], day, tuple(r["teams"])) in by_id
                for day in r["same_pair_and_score_verified_nearby"]
            ),
            "discarded claim has no retained dated replacement",
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        required=True,
        type=Path,
        help="checkout with scripts/nba_wikimedia_archive.py",
    )
    parser.add_argument(
        "--cache",
        required=True,
        type=Path,
        help="separate cache for en/, fr/, irw-scores.csv",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help="download missing openly licensed originals",
    )
    parser.add_argument(
        "--final-csv", type=Path, help="optional six-season merged finals"
    )
    args = parser.parse_args()
    path = args.repo / "scripts/nba_wikimedia_archive.py"
    spec = importlib.util.spec_from_file_location("wiki_english_parser", path)
    english = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(english)
    pins = load(BUNDLE / "revision-pins.json")
    score_file = args.cache / "irw-scores.csv"
    if args.fetch:
        fetch_wiki(pins, args.cache)
        if not score_file.is_file():
            score_file.write_bytes(download(REDIVIS))
    facts = {"538": archive_538(score_file)}
    for pin in pins:
        page = load(args.cache / pin["lang"] / f"{pin['year']}-{pin['team']}.json")
        rev = page.get("revision", page.get("revid"))
        require(
            rev == pin["revision"]
            and page["timestamp"] == pin["timestamp"]
            and hashlib.sha256(page["content"].encode()).hexdigest() == pin["sha256"],
            "cached wiki revision/digest differs",
        )
        if pin["lang"] == "en":
            page_facts = {(pin["year"], *v) for v in english.perspectives(page)}
        else:
            page_facts = {(key[0], key[1], *r) for key, r, _ in rows(page)}
        facts[f"{pin['lang']}:{pin['revision']}"] = page_facts
    proofs = {r["game"]: r["proofs"] for r in load(BUNDLE / "proofs.json")}
    corrections = [fact(r) for r in read_csv(BUNDLE / "resolved.csv")]
    require(
        len(proofs) == len(set(map(identity, corrections))) == len(corrections),
        "duplicate correction",
    )
    for row in corrections:
        labels = proofs["/".join(map(str, row[:4]))]
        require(
            len(set(labels)) >= 2 and all(row in facts[label] for label in labels),
            f"unverified correction {row}",
        )
    for r in read_csv(BUNDLE / "withdraw.csv"):
        bad = fact(r)
        require(
            all(identity(v) != identity(bad) for v in facts["538"])
            and any(
                v[0] == bad[0]
                and v[2:] == bad[2:]
                and abs((date.fromisoformat(v[1]) - date.fromisoformat(bad[1])).days)
                == 1
                for v in corrections
            ),
            "unproven date withdrawal",
        )
    if args.final_csv:
        values = [fact(r) for r in read_csv(args.final_csv)]
    else:
        baseline = args.repo / "reports/nba-phase1/modern-archive/agreed-finals.csv"
        expected = load(BUNDLE / "counts.json")["baseline_source_sha256"][
            "agreed-finals.csv"
        ]
        require(
            hashlib.sha256(baseline.read_bytes()).hexdigest() == expected,
            "English baseline digest differs",
        )
        values = [fact(r) for r in read_csv(baseline)]
        merged = {identity(v): v for v in values}
        require(len(merged) == len(values), "duplicate English baseline game")
        for r in read_csv(BUNDLE / "withdraw.csv"):
            require(
                merged.pop(identity(fact(r))) == fact(r), "withdrawn baseline differs"
            )
        merged.update((identity(v), v) for v in corrections)
        values = list(merged.values())
    check_full(values, corrections)
    print(
        f"Verified {len(corrections)} corrections using {len(pins)} pinned Wikipedia pages and FiveThirtyEight v7.2."
    )


if __name__ == "__main__":
    main()
