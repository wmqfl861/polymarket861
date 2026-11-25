"""Premier League fixture history, point-in-time forecasts, grading and CLI."""

from __future__ import annotations

import bisect
import collections
import csv
import hashlib
import json
import math
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import numpy as np

from .football_model import (
    DECAY_PER_DAY,
    DEFAULT_LINES,
    ITEM_KEYS,
    PROB_KEYS,
    RIDGE,
    _goals_matrix,
    fit_team_model,
    half_line,
    probabilities,
    score_probabilities,
)
from .football_model import (
    apply_context as apply_context,
)

SOURCE_COMMIT = "e6744429ee395bc86f247348c6184bb08d4eb361"
SOURCE_BLOBS = {
    "2010-11": "9fe7c43945ed5bb7249ae2488397262d0d72ca88",
    "2011-12": "72c0c8ca26ce53bdafb71a3d12ea4e51fe8790a3",
    "2012-13": "b9fba456fccee350f06ced85600229dfe8d895d4",
    "2013-14": "f8f06a0494c657638a6ad055effdf65c33c164c3",
    "2014-15": "4ac8e36323109731fb60fc3db678a0b358074e12",
    "2015-16": "d7384b9c162eea51ea0da500b8f6c73a170ee0bd",
    "2016-17": "b09de9d55b17571e6c9c64fefe83522dfc02ce6f",
    "2017-18": "c0ccb9c85503d1e24482be7c5117f6277c5a0e2f",
    "2018-19": "334909dfb533959f15a6d2bf08a096adcfa7d21f",
    "2019-20": "f1af09cf49fb2ac69d17acfe76c030c67ae9c1dc",
    "2020-21": "081509058af75c0c012d64e980dc4fa53720d933",
    "2021-22": "5eadd9d91c71d45634f3969192ad908653681b23",
    "2022-23": "0b6230e60d8cd7402810a5f515f6762d8b385b74",
    "2023-24": "f62b11dfaf06cffd1236c2777caebb880a0cfea1",
    "2024-25": "504f2e732fbc3c9caeb98e4382b13b094453b840",
    "2025-26": "41e223ee5954e6b634beb4bc70d7188b3183c665",
    "2026-27": "60e281f0fcc51cfca1a901353666a688cf62a8ba",
}
LONDON = ZoneInfo("Europe/London")
MODEL_VERSION = "football-dc-1"
SEASON_PATTERN = re.compile(r"^(\d{4})-(\d{2})$")
REPAIR_2019 = {
    "season": "2019-20",
    "count": 66,
    "attribution": "FiveThirtyEight, Club soccer predictions match file (spi_matches.csv), via Item Response Warehouse, irw_competitions v7.1, DOI 10.57761/3r1w-t660",
    "independent_source": "https://itemresponsewarehouse.org/tables/fivethirtyeight_soccer_2016_2023/",
    "csv": "https://redivis.com/api/v1/tables/datapages.irw_competitions:v7_1.fivethirtyeight_soccer_2016_2023/rows?format=csv",
    "license": "https://creativecommons.org/licenses/by/4.0/",
    "upstream_license_declaration": "https://github.com/fivethirtyeight/data",
    "retrieved_csv_sha256": "ae408d93dae0322ea98bac8d2f392e3637b6563824c46822e44dae91f6ccd8da",
    "matched_rows_sha256": "3b9ab89545cdcec1af35560154e2beda9bbe769db84ac0c102d27109de831ef7",
    "fields": ["home", "away", "corrected_local_date", "ft_home", "ft_away"],
    "change": "Year 2019 to 2020 in the 66 July fixtures of the pinned source blob",
}


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("supply an ISO-8601 timestamp with an explicit UTC offset")
    return value.astimezone(UTC)


def iso(value):
    return utc(value).isoformat().replace("+00:00", "Z")


@lru_cache(maxsize=512)
def team_key(name):
    words = (
        unicodedata.normalize("NFKD", name.casefold())
        .encode("ascii", "ignore")
        .decode()
    )
    parts = re.findall(r"[a-z0-9]+", words)
    return "-".join(p for p in parts if p not in {"afc", "fc"})


def fixture_id(season, home, away):
    return f"epl/{season}/{team_key(home)}/{team_key(away)}"


def season_year(value):
    m = SEASON_PATTERN.fullmatch(value)
    if not m or (int(m[1]) + 1) % 100 != int(m[2]):
        raise ValueError(f"invalid season: {value}")
    return int(m[1])


def blob_sha(raw):
    return hashlib.sha1(b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()


def _score(pair):
    if (
        not isinstance(pair, (list, tuple))
        or len(pair) != 2
        or any(type(v) is not int or not 0 <= v <= 50 for v in pair)
    ):
        raise ValueError(
            "full-time goals must be a pair of nonnegative integer counts (0–50)"
        )
    return tuple(pair)


@dataclass(frozen=True)
class ResultVersion:
    available: datetime
    score: tuple | None
    grade: str
    provenance: str
    actual_kickoff: datetime | None = None


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    season: str
    round: int
    kickoff: datetime
    home: str
    away: str
    home_goals: int | None = None
    away_goals: int | None = None
    result_versions: tuple = ()
    kickoff_precision: str = "minute"
    schedule_known_at: datetime | None = None
    schedule_versions: tuple = ()
    source_blob: str = ""
    status: str = "scheduled"
    schedule_grades: tuple = ()


def read_json_rows(path):
    """Read JSON lines, a JSON array, or a single JSON object."""
    if path is None:
        return
    with Path(path).open(encoding="utf-8") as stream:
        first = next((v for v in stream if v.strip()), None)
        if first is None:
            return
        if first.lstrip().startswith("["):
            yield from json.loads(first + stream.read())
            return
        try:
            value = json.loads(first)
        except json.JSONDecodeError:
            value = json.loads(first + stream.read())
            yield value
            return
        yield value
        for line in stream:
            if line.strip():
                yield json.loads(line)


def _evidence_map(path):
    grouped = collections.defaultdict(list)
    for obj in read_json_rows(path):
        if not isinstance(obj, dict):
            raise ValueError("evidence records must be objects")
        fid = obj.get("fixture_id") or fixture_id(
            obj["season"], obj["home"], obj["away"]
        )
        grouped[fid].append(obj)
    return grouped


def _parsed_version(row, kickoff):
    upper_bound = "finalized_by" in row or "published_by" in row
    if upper_bound:
        if (
            not row.get("archive_at")
            or not row.get("source")
            or "finalized_at" in row
            or "published_at" in row
        ):
            raise ValueError(
                "result upper bounds require archive_at and source, without exact-time claims"
            )
        finalized, published = utc(row["finalized_by"]), utc(row["published_by"])
        received, grade = utc(row["archive_at"]), "archived_upper_bound"
    else:
        finalized, published = utc(row["finalized_at"]), utc(row["published_at"])
    if not upper_bound and row.get("received_at"):
        received = utc(row["received_at"])
        grade = "observed"
    elif not upper_bound and row.get("archive_at") and row.get("source"):
        received = utc(row["archive_at"])
        grade = "archived"
    elif not upper_bound:
        raise ValueError(
            "result evidence needs received_at or archive_at with a source"
        )
    actual = utc(row["result_kickoff"]) if row.get("result_kickoff") else None
    if finalized < (actual or kickoff) or published < finalized or received < published:
        raise ValueError("inconsistent kickoff/finalization/publication/receipt order")
    score = (
        None
        if row.get("retracted")
        else _score(row.get("score", [row.get("home_goals"), row.get("away_goals")]))
    )
    return ResultVersion(
        received, score, grade, str(row.get("source", "local-observation")), actual
    )


def verify_2019_repair(matches, comparison_csv=None):
    """Fail closed unless every repaired row matches the independent five-field manifest."""
    suspect = [r for r in matches if r["date"].startswith("2019-07-")]
    aliases = {
        "brighton-hove-albion": "brighton-and-hove-albion",
        "newcastle-united": "newcastle",
        "wolverhampton-wanderers": "wolverhampton",
    }
    comparison = {}
    if comparison_csv is not None:
        with Path(comparison_csv).open(encoding="utf-8-sig", newline="") as stream:
            for r in csv.DictReader(stream):
                if r["season"] != "2019" or r["league"] != "Barclays Premier League":
                    continue
                key = team_key(r["agent_a"]), team_key(r["agent_b"])
                if key in comparison:
                    raise ValueError("duplicate independent fixture")
                day = datetime.fromtimestamp(int(r["date"]), UTC).date().isoformat()
                comparison[key] = (day, int(r["score_a"]), int(r["score_b"]))
    lines = []
    for row in suspect:
        h, a = team_key(row["team1"]), team_key(row["team2"])
        values = ("2020" + row["date"][4:], *_score(row["score"]["ft"]))
        if (
            comparison_csv is not None
            and comparison.get((aliases.get(h, h), aliases.get(a, a))) != values
        ):
            raise ValueError(f"unverified date repair: {h} v {a}")
        lines.append("|".join(map(str, (h, a, *values))))
    digest = hashlib.sha256(("\n".join(sorted(lines)) + "\n").encode()).hexdigest()
    if (
        len(lines) != REPAIR_2019["count"]
        or digest != REPAIR_2019["matched_rows_sha256"]
    ):
        raise ValueError("unverified 2019-20 date-repair manifest")
    return dict(REPAIR_2019)


def _parse_match(season, row, evidence, lag_hours, source_blob):
    home, away = row["team1"], row["team2"]
    fid = fixture_id(season, home, away)
    ld = date.fromisoformat(row["date"])
    # A single documented, source-versioned repair. Never shift July for other seasons.
    if (
        season == "2019-20"
        and ld.year == 2019
        and ld.month == 7
        and source_blob == SOURCE_BLOBS.get(season)
    ):
        ld = ld.replace(year=2020)
    hh = row.get("time")
    wall = time.fromisoformat(hh) if hh else time(23, 59, 59)
    kickoff = datetime.combine(ld, wall, LONDON).astimezone(UTC)
    rnd_match = re.search(r"(\d+)$", str(row["round"]))
    if not rnd_match or not 1 <= int(rnd_match[1]) <= 38:
        raise ValueError(f"{fid}: invalid Premier League round")
    rnd = int(rnd_match[1])
    y = season_year(season)
    if not date(y, 8, 1) <= ld < date(y + 1, 8, 1):
        return Fixture(
            fid,
            season,
            rnd,
            kickoff,
            home,
            away,
            source_blob=source_blob,
            status="quarantined",
        )
    raw_score = row.get("score")
    if isinstance(raw_score, dict):
        raw_score = raw_score.get("ft")
    final = _score(raw_score) if raw_score is not None else None
    status = (
        "completed"
        if final is not None
        else str(row.get("status", "scheduled")).casefold()
    )
    observations = []
    schedules = []
    schedule_grades = {}
    for entry in evidence:
        if "schedule_published_at" in entry:
            pub, rec = (
                utc(entry["schedule_published_at"]),
                utc(entry["schedule_received_at"]),
            )
            if rec < pub:
                raise ValueError(f"{fid}: schedule received before publication")
            schedules.append((rec, utc(entry["kickoff"])))
            schedule_grades[rec] = "observed_schedule"
        elif "schedule_observed_at" in entry:
            rec = utc(entry["schedule_observed_at"])
            schedules.append((rec, utc(entry["kickoff"])))
            schedule_grades.setdefault(rec, "archived_schedule_upper_bound")
        if (
            "finalized_at" in entry
            or "finalized_by" in entry
            or "published_by" in entry
        ):
            observations.append(_parsed_version(entry, kickoff))
    if final is not None and not observations and lag_hours is not None:
        observations.append(
            ResultVersion(
                kickoff + timedelta(hours=lag_hours),
                final,
                "lag_assumption",
                f"openfootball:{source_blob}",
            )
        )

    def sorted_unique(items, stamp):
        by_time = {}
        for item in items:
            t = stamp(item)
            if t in by_time and by_time[t] != item:
                raise ValueError(f"{fid}: conflicting versions at {iso(t)}")
            by_time[t] = item
        return tuple(by_time[t] for t in sorted(by_time))

    versions = sorted_unique(observations, lambda x: x.available)
    schedules = sorted_unique(schedules, lambda x: x[0])
    if versions and versions[-1].grade != "lag_assumption":
        final = versions[-1].score
        status = "completed" if final is not None else "pending"
    return Fixture(
        fid,
        season,
        rnd,
        kickoff,
        home,
        away,
        *(final or (None, None)),
        versions,
        "minute" if hh else "date",
        schedules[-1][0] if schedules else None,
        schedules,
        source_blob,
        status,
        tuple(schedule_grades[t] for t, _ in schedules),
    )


def load_fixtures(root: Path, *, evidence_path=None, archive_lag_hours=None):
    """One season is decoded at a time. Strict mode uses only supplied result observations.

    A positive archive_lag_hours explicitly enables a publication-delay sensitivity
    proxy. OpenFootball's final files contain neither publication time nor local receipt.
    """
    if archive_lag_hours is not None and (
        not math.isfinite(archive_lag_hours) or archive_lag_hours < 3
    ):
        raise ValueError(
            "archive delay study requires a finite lag of at least 3 hours"
        )
    root = Path(root)
    paths = [root] if root.is_file() else sorted(root.glob("????-??/en.1.json"))
    if not paths:
        raise ValueError(f"no season/en.1.json files found in {root}")
    evidence = _evidence_map(evidence_path)
    fixtures = []
    seen = set()
    for file in paths:
        season = file.parent.name
        season_year(season)
        raw = file.read_bytes()
        sha = blob_sha(raw)
        payload = json.loads(raw)
        matches = payload["matches"] if isinstance(payload, dict) else payload
        if not isinstance(matches, list):
            raise ValueError(f"{file}: invalid fixture array")
        if season == "2019-20" and sha == SOURCE_BLOBS[season]:
            verify_2019_repair(matches)
        for row in matches:
            fid = fixture_id(season, row["team1"], row["team2"])
            if fid in seen:
                raise ValueError(f"duplicate fixture key {fid}")
            if team_key(row["team1"]) == team_key(row["team2"]):
                raise ValueError(f"same team on both sides: {fid}")
            seen.add(fid)
            fixtures.append(
                _parse_match(season, row, evidence.get(fid, ()), archive_lag_hours, sha)
            )
    unknown = evidence.keys() - seen
    if unknown:
        raise ValueError("unknown fixture in evidence file: " + min(unknown))
    return fixtures


def fetch_history(destination, first, last):
    first_year, last_year = season_year(first), season_year(last)
    if first_year > last_year:
        raise ValueError("from-season follows to-season")
    result = []
    for year in range(first_year, last_year + 1):
        season = f"{year}-{(year + 1) % 100:02}"
        if season not in SOURCE_BLOBS:
            raise ValueError(f"{season} outside pinned data")
        target = Path(destination) / season / "en.1.json"
        if target.exists():
            if blob_sha(target.read_bytes()) != SOURCE_BLOBS[season]:
                raise ValueError(f"existing data differs from pinned input: {target}")
        else:
            address = f"https://raw.githubusercontent.com/openfootball/football.json/{SOURCE_COMMIT}/{season}/en.1.json"
            with urlopen(
                Request(address, headers={"User-Agent": "polymarket861/football"}),
                timeout=30,
            ) as response:
                raw = response.read(4_000_001)
            if blob_sha(raw) != SOURCE_BLOBS[season]:
                raise ValueError(f"unexpected upstream blob for {season}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
                tmp.write(raw)
                name = tmp.name
            os.replace(name, target)
        result.append(season)
    return {
        "repository": "openfootball/football.json",
        "commit": SOURCE_COMMIT,
        "seasons": result,
        "destination": str(destination),
    }


@dataclass
class TrainingFit:
    baseline: np.ndarray
    b1: object
    evidence: dict


class FootballEngine:
    """As-of index, including result restatements and retractions."""

    def __init__(self, fixtures):
        self.fixtures = list(fixtures)
        self.seasons = collections.defaultdict(list)
        for f in self.fixtures:
            self.seasons[f.season].append(f)
        self.indexes = {}
        self.last_fit = {}
        self.by_key = {f.fixture_id: f for f in self.fixtures}

    def _index(self, season):
        if season not in self.indexes:
            y = season_year(season)
            selected = [
                f
                for s, ff in self.seasons.items()
                if y - 3 <= season_year(s) <= y
                for f in ff
            ]
            events = []
            for f in selected:
                for version in f.result_versions:
                    event_start = version.actual_kickoff or f.kickoff
                    if version.available < event_start:
                        raise ValueError("result available before kickoff")
                    source = f"{f.fixture_id}|{iso(event_start)}|{iso(version.available)}|{version.score}|{version.grade}|{version.provenance}"
                    events.append(
                        (
                            version.available,
                            f.fixture_id,
                            f,
                            version,
                            hashlib.sha256(source.encode()).digest(),
                        )
                    )
            events.sort(key=lambda x: (x[0], x[1]))
            self.indexes[season] = ([v[0] for v in events], events)
        return self.indexes[season]

    def fit(self, season, cutoff, exclude_id):
        times, events = self._index(season)
        n = bisect.bisect_right(times, cutoff)
        if not n:
            raise ValueError("no eligible historical results at cutoff")
        # Normally the target cannot have a result before its own kickoff. Keep
        # the explicit exclusion for corrected fixture/schedule identities.
        f = self.by_key.get(exclude_id)
        relevant_exclusion = exclude_id if f and any(v.available <= cutoff for v in f.result_versions) else None
        cache_key = (n, relevant_exclusion)
        old = self.last_fit.get(season)
        if old and old[0] == cache_key:
            return old[1]
        latest = {}
        for _, fid, ff, v, digest in events[:n]:
            if fid != relevant_exclusion:
                latest[fid] = (ff, v, digest)
        eligible = sorted(
            (r for r in latest.values() if r[1].score is not None),
            key=lambda v: v[0].fixture_id,
        )
        if not eligible:
            raise ValueError("no eligible historical results at cutoff")
        count = collections.Counter(r[0].season for r in eligible)
        complete = {
            s
            for s, nn in count.items()
            if s < season and nn == len(self.seasons[s]) and nn == 380
        }
        reference = (
            [r for r in eligible if r[0].season in complete] if complete else eligible
        )
        grid = np.zeros((max(max(v.score) for _, v, _ in reference) + 1,) * 2)
        for _, v, _ in reference:
            grid[v.score] += 1
        grid /= len(reference)
        records = [
            (team_key(ff.home), team_key(ff.away), *v.score, (v.actual_kickoff or ff.kickoff).timestamp())
            for ff, v, _ in eligible
        ]
        fitted = fit_team_model(records)
        evidence = {
            "training_matches": len(eligible),
            "b0_matches": len(reference),
            "b0_scope": "prior_complete_seasons" if complete else "available_warmup",
            "result_grades": dict(collections.Counter(v.grade for _, v, _ in eligible)),
            "latest_result_available_at": iso(max(v.available for _, v, _ in eligible)),
            "input_sha256": hashlib.sha256(
                b"".join(x[2] for x in eligible)
            ).hexdigest(),
        }
        result = TrainingFit(grid, fitted, evidence)
        self.last_fit[season] = (cache_key, result)
        return result

    def forecast(
        self,
        home,
        away,
        kickoff,
        cutoff,
        *,
        season=None,
        lines=DEFAULT_LINES,
        baseline=False,
    ):
        kickoff, cutoff = utc(kickoff), utc(cutoff)
        if cutoff >= kickoff:
            raise ValueError("cutoff must be before kickoff")
        hk, ak = team_key(home), team_key(away)
        if not hk or not ak or hk == ak:
            raise ValueError("home and away must be different identified teams")
        if season is None:
            local = kickoff.astimezone(LONDON)
            y = local.year if local.month >= 8 else local.year - 1
            season = f"{y}-{(y + 1) % 100:02}"
        fid = fixture_id(season, home, away)
        target = self.by_key.get(fid)
        schedule_grade = "supplied_or_final_archive_schedule"
        if target and target.schedule_versions:
            known = [entry for entry in target.schedule_versions if entry[0] <= cutoff]
            if not known or known[-1][1] != kickoff:
                raise ValueError("fixture schedule not known at cutoff")
            schedule_grade = (
                target.schedule_grades[len(known) - 1]
                if target.schedule_grades
                else "observed_schedule"
            )
        training = self.fit(season, cutoff, fid)
        model = training.b1
        rates = None
        tail = 0.0
        fallback = None
        if baseline:
            fallback = "baseline_requested"
        elif model is None:
            fallback = "insufficient_training_or_fit_failed"
        elif hk not in model.positions or ak not in model.positions:
            fallback = "unseen_team"
        if fallback is None:
            h, a, r = model.rates(hk, ak)
            try:
                matrix, tail = _goals_matrix(h, a, r)
                rates = {"home": h, "away": a, "rho": r}
            except ValueError:
                fallback = "invalid_dc_probability"
        if fallback:
            matrix = training.baseline
        p, totals = probabilities(matrix, lines)
        return {
            "fixture_id": fid,
            "season": season,
            "round": target.round if target else None,
            "home": home,
            "away": away,
            "kickoff": iso(kickoff),
            "cutoff": iso(cutoff),
            "model": "B0" if fallback else "B1",
            "model_version": MODEL_VERSION,
            "rates": rates,
            "fallback_reason": fallback,
            "probabilities": p,
            "totals": totals,
            "matrix": matrix.tolist(),
            "tail_bound": tail,
            "evidence": dict(training.evidence, schedule_grade=schedule_grade),
        }


def predict(fixtures, home, away, kickoff, cutoff, *, season=None, lines=DEFAULT_LINES):
    return FootballEngine(fixtures).forecast(
        home, away, kickoff, cutoff, season=season, lines=lines
    )


def _average(numbers):
    return math.fsum(numbers) / len(numbers) if numbers else None


def history_kickoff(fixture, cutoff):
    """Latest schedule or verified event time actually known at the feature cutoff."""
    for version in reversed(fixture.result_versions):
        if version.available <= cutoff:
            if version.score is not None and version.actual_kickoff:
                return version.actual_kickoff
            break
    for received, kickoff in reversed(fixture.schedule_versions):
        if received <= cutoff:
            return kickoff
    return fixture.kickoff


def _result_at(fixture, as_of):
    if as_of is None:
        return (
            (fixture.home_goals, fixture.away_goals)
            if fixture.home_goals is not None
            else None
        )
    versions = [v for v in fixture.result_versions if v.available <= as_of]
    return max(versions, key=lambda v: v.available).score if versions else None


def _per_line(totals, hg, ag):
    result = {}
    for side, goals in (("home", hg), ("away", ag), ("total", hg + ag)):
        for line, directions in totals.get(side, {}).items():
            line_value = half_line(line)
            prob = float(directions["over"])
            if not math.isfinite(prob) or not 0 <= prob <= 1:
                raise ValueError("invalid total probability")
            observed = int(goals > line_value)
            result[f"{side}_{line}"] = {
                "brier": (prob - observed) ** 2,
                "log_loss": -math.log(max(prob if observed else 1 - prob, 1e-15)),
            }
    return result


def score_round(
    fixtures,
    predictions,
    *,
    season=None,
    round_number=None,
    cutoff_minutes=60,
    as_of=None,
):
    """Grade one official round on the actual kickoff version. Missing markets never block grading."""
    if cutoff_minutes <= 0:
        raise ValueError("a positive cutoff is required")
    if as_of is not None:
        as_of = utc(as_of)
    selected = [
        f
        for f in fixtures
        if (season is None or f.season == season)
        and (round_number is None or f.round == round_number)
    ]
    if len({(f.season, f.round) for f in selected}) > 1:
        raise ValueError("select one season and official round")
    if len({f.fixture_id for f in selected}) != len(selected):
        raise ValueError("duplicate fixture in round")
    if len(selected) > 10:
        raise ValueError("an EPL round cannot contain more than ten fixtures")
    if selected:
        season, round_number = selected[0].season, selected[0].round
    by_id = collections.defaultdict(list)
    if isinstance(predictions, dict):
        for fid, pred in predictions.items():
            by_id[fid].append(pred)
    else:
        for pred in predictions:
            by_id[pred["fixture_id"]].append(pred)
    rows = []
    points = []
    losses = collections.defaultdict(list)
    log_losses = collections.defaultdict(list)
    extra_lines = collections.defaultdict(list)
    baseline_points = []
    completed = 0
    cancelled = 0
    baseline_model_points = []
    for f in sorted(selected, key=lambda f: (f.kickoff, f.fixture_id)):
        if as_of is not None and f.schedule_versions:
            published = [
                (known, kick) for known, kick in f.schedule_versions if known <= as_of
            ]
            active_kickoff = published[-1][1] if published else None
        else:
            active_kickoff = f.kickoff
        cutoff = (
            active_kickoff - timedelta(minutes=cutoff_minutes)
            if active_kickoff is not None
            else None
        )
        row = {
            "fixture_id": f.fixture_id,
            "home": f.home,
            "away": f.away,
            "kickoff": iso(active_kickoff) if active_kickoff else None,
        }
        rows.append(row)
        truth = _result_at(f, as_of)
        if truth is None:
            cancelled_now = as_of is None and f.status in {
                "cancelled",
                "canceled",
                "void",
            }
            row["status"] = "cancelled" if cancelled_now else "pending"
            cancelled += int(cancelled_now)
            continue
        completed += 1
        row["score"] = list(truth)
        matching = []
        for pred in by_id[f.fixture_id]:
            if cutoff is None or (pred.get("cutoff") and utc(pred["cutoff"]) != cutoff):
                continue
            if pred.get("kickoff") and utc(pred["kickoff"]) != active_kickoff:
                continue
            if (
                pred.get("cutoff_minutes") is not None
                and int(pred["cutoff_minutes"]) != cutoff_minutes
            ):
                continue
            if pred.get("sealed_at") and utc(pred["sealed_at"]) > cutoff:
                continue
            # A bare vector is supported for a caller which already bound this
            # round. A reschedule needs an explicitly bound new decision.
            if len(f.schedule_versions) > 1 and not (
                pred.get("kickoff") and pred.get("cutoff")
            ):
                continue
            matching.append(pred)
        if not matching:
            row["status"] = "missing"
            continue
        if len(matching) > 1 and any(p != matching[0] for p in matching[1:]):
            raise ValueError(
                f"multiple distinct predictions for one cutoff: {f.fixture_id}"
            )
        pred = matching[0]
        grade = score_probabilities(
            pred.get("probabilities", pred.get("model_probabilities", pred)), *truth
        )
        points.append(grade["score"])
        for k in ITEM_KEYS:
            losses[k].append(grade["losses"][k])
            log_losses[k].append(grade["log_losses"][k])
        for k, extra in _per_line(pred.get("totals", {}), *truth).items():
            extra_lines[k].append(extra)
        if pred.get("baseline_probabilities"):
            b = score_probabilities(pred["baseline_probabilities"], *truth)
            baseline_points.append(b["score"])
            baseline_model_points.append(grade["score"])
            row["baseline_score"] = b["score"]
        row.update(
            status="scored",
            model_score=grade["score"],
            item_scores=grade["item_scores"],
        )
    missing = completed - len(points)
    unknown_schedule = 10 - len(selected)
    pending = len(selected) - completed - cancelled + unknown_schedule
    total = math.fsum(points)
    return {
        "season": season,
        "round": round_number,
        "cutoff_minutes": cutoff_minutes,
        "scheduled": 10,
        "registered": len(selected),
        "unregistered": unknown_schedule,
        "completed": completed,
        "cancelled": cancelled,
        "pending": pending,
        "missing": missing,
        "scored": len(points),
        "mean_score": _average(points),
        "item_scores": {k: 100 * (1 - _average(v)) for k, v in losses.items()},
        "losses": {k: _average(v) for k, v in losses.items()},
        "log_losses": {k: _average(v) for k, v in log_losses.items()},
        "baseline": {
            "paired_n": len(baseline_points),
            "score": _average(baseline_points),
            "model_score": _average(baseline_model_points),
        },
        "additional_lines": {
            k: {m: _average([r[m] for r in v]) for m in ("brier", "log_loss")}
            for k, v in extra_lines.items()
        },
        "score_range": [total / completed, (total + 100 * missing) / completed]
        if completed
        else None,
        "status": "preliminary"
        if pending or not selected
        else "incomplete"
        if missing
        else "final",
        "fixtures": rows,
    }


def _truth_vector(h, a):
    return (h > a, h == a, h < a, h + a >= 3, h >= 2, a >= 2)


def _calibration(records, field, bins=5):
    output = {}
    for index, key in enumerate(PROB_KEYS):
        sums = np.zeros((bins, 3))
        for row in records:
            p = row[field][key]
            observed = row["observed"][index]
            sums[min(int(p * bins), bins - 1)] += (1, p, observed)
        output[key] = [
            {
                "n": int(n),
                "mean_p": round(p / n, 6) if n else None,
                "observed": round(y / n, 6) if n else None,
            }
            for n, p, y in sums
        ]
    return output


def _cluster_interval(rows, samples, seed):
    groups = collections.defaultdict(list)
    for r in rows:
        groups[(r["season"], r["round"])].append(r["model_score"] - r["baseline_score"])
    if len(groups) < 2 or samples <= 0:
        return None
    groups = [groups[k] for k in sorted(groups)]
    counts = np.array(list(map(len, groups)))
    totals = np.array(list(map(math.fsum, groups)))
    rng = np.random.default_rng(seed)
    boot = []
    # Cap temporary allocations when evaluating long histories.
    for start in range(0, samples, 128):
        indices = rng.integers(
            len(groups), size=(min(128, samples - start), len(groups))
        )
        boot.extend(totals[indices].sum(axis=1) / counts[indices].sum(axis=1))
    return [float(v) for v in np.percentile(boot, [2.5, 97.5])]


def _metrics(rows, scheduled, bootstrap, seed, *, calibration):
    def means_for(field):
        return {k: _average([r[field][k] for r in rows]) for k in ITEM_KEYS}

    n = len(rows)
    result = {
        "n": n,
        "coverage": n / scheduled if scheduled else None,
        "eligible_completed": scheduled,
        "verified_cutoff_n": sum(
            bool(r["evidence"]["result_grades"])
            and set(r["evidence"]["result_grades"]) <= {"archived_upper_bound"}
            and r["evidence"]["schedule_grade"] == "archived_schedule_upper_bound"
            for r in rows
        ),
        "rounds": len({(r["season"], r["round"]) for r in rows}),
        "b0_score": _average([r["baseline_score"] for r in rows]),
        "model_score": _average([r["model_score"] for r in rows]),
        "paired_delta": _average(
            [r["model_score"] - r["baseline_score"] for r in rows]
        ),
        "paired_round_ci95": _cluster_interval(rows, bootstrap, seed),
        "model_counts": dict(collections.Counter(r["model"] for r in rows)),
        "fallback_reasons": dict(
            collections.Counter(
                r["fallback_reason"] for r in rows if r["fallback_reason"]
            )
        ),
        "result_timing": dict(
            collections.Counter(
                "assumed_lag"
                if "lag_assumption" in r["evidence"]["result_grades"]
                else "unverified_input"
                if "unverified_input" in r["evidence"]["result_grades"]
                else "evidenced"
                for r in rows
            )
        ),
        "schedule_timing": dict(
            collections.Counter(r["evidence"]["schedule_grade"] for r in rows)
        ),
        "model_losses": means_for("model_losses"),
        "baseline_losses": means_for("baseline_losses"),
        "model_log_losses": means_for("model_log_losses"),
        "baseline_log_losses": means_for("baseline_log_losses"),
    }
    if calibration:
        result["calibration"] = {
            "bins": [0, 0.2, 0.4, 0.6, 0.8, 1],
            "model": _calibration(rows, "model_probabilities"),
            "baseline": _calibration(rows, "baseline_probabilities"),
        }
    return result


def backtest(
    fixtures,
    from_season,
    to_season,
    *,
    horizons=(1440, 60),
    bootstrap=2000,
    seed=861,
    archive_lag_hours=None,
    details_sink=None,
    input_provenance=None,
):
    """Walk each kickoff frontier. The scorer only sees a match after both forecasts."""
    import platform

    import scipy

    lo, hi = season_year(from_season), season_year(to_season)
    if lo > hi:
        raise ValueError("from-season follows to-season")
    if (
        not horizons
        or any(not isinstance(h, int) or h <= 0 for h in horizons)
        or len(set(horizons)) != len(horizons)
    ):
        raise ValueError("horizons must be distinct positive integer minutes")
    if not 0 <= bootstrap <= 100000:
        raise ValueError("bootstrap resamples must be between 0 and 100000")
    from . import historical_inputs

    provenance = input_provenance or {
        "status": "research_only",
        "reason": "unattested_in_memory_inputs",
        "timing_authenticated": False,
    }
    fixtures = list(fixtures)
    if not provenance["timing_authenticated"]:
        fixtures = historical_inputs.label_research_inputs(fixtures)
    selected = [f for f in fixtures if lo <= season_year(f.season) <= hi]
    if not selected:
        raise ValueError("no fixtures in requested season range")
    seasons = sorted({f.season for f in selected})
    finished = [
        f for f in selected if f.home_goals is not None and f.away_goals is not None
    ]
    counts = collections.Counter(f.season for f in finished)
    grades = collections.Counter(v.grade for f in fixtures for v in f.result_versions)
    blobs = {f.season: f.source_blob for f in fixtures}
    pinned = all(v and SOURCE_BLOBS.get(k) == v for k, v in blobs.items())
    events = sorted(
        (
            (f.kickoff - timedelta(minutes=h), f.fixture_id, h, f)
            for f in finished
            for h in horizons
        ),
        key=lambda r: r[:3],
    )
    engine = FootballEngine(fixtures)
    results = collections.defaultdict(list)
    omissions = collections.defaultdict(collections.Counter)
    for cutoff, fid, horizon, f in events:
        if f.kickoff_precision != "minute":
            omissions[horizon]["kickoff_time_unknown"] += 1
            continue
        try:
            p = engine.forecast(
                f.home, f.away, f.kickoff, cutoff, season=f.season, lines=()
            )
            b = engine.forecast(
                f.home,
                f.away,
                f.kickoff,
                cutoff,
                season=f.season,
                lines=(),
                baseline=True,
            )
        except ValueError as exc:
            if str(exc) not in {
                "fixture schedule not known at cutoff",
                "no eligible historical results at cutoff",
            }:
                raise
            omissions[horizon][str(exc)] += 1
            continue
        pp, bp = p["probabilities"], b["probabilities"]
        observed = (f.home_goals, f.away_goals)
        model = score_probabilities(pp, *observed)
        baseline = score_probabilities(bp, *observed)
        row = {
            "fixture_id": fid,
            "season": f.season,
            "round": f.round,
            "kickoff": iso(f.kickoff),
            "cutoff": iso(cutoff),
            "cutoff_minutes": horizon,
            "home": f.home,
            "away": f.away,
            "score": list(observed),
            "model": p["model"],
            "baseline_model": "B0",
            "model_score": model["score"],
            "baseline_score": baseline["score"],
            "model_items": model["item_scores"],
            "baseline_items": baseline["item_scores"],
            "model_losses": model["losses"],
            "baseline_losses": baseline["losses"],
            "model_log_losses": model["log_losses"],
            "baseline_log_losses": baseline["log_losses"],
            "observed": _truth_vector(*observed),
            "model_probabilities": pp,
            "baseline_probabilities": bp,
            "fallback_reason": p["fallback_reason"],
            "evidence": p["evidence"],
        }
        if details_sink:
            details_sink(row)
        results[horizon].append(row)
    stats = {}
    for h in horizons:
        rows = results[h]
        stats[f"T-{h}"] = {
            "summary": _metrics(rows, len(finished), bootstrap, seed, calibration=True),
            "omissions": dict(omissions[h]),
            "by_season": {
                s: _metrics(
                    [r for r in rows if r["season"] == s],
                    counts[s],
                    bootstrap,
                    seed,
                    calibration=True,
                )
                for s in seasons
            },
        }
    return {
        "kind": "historical_football_backtest",
        "input_provenance": provenance,
        "model_version": MODEL_VERSION,
        "source": {
            "repository": "openfootball/football.json",
            "commit": SOURCE_COMMIT if pinned else provenance.get("source_revision"),
            "blobs": blobs,
            "all_files_verified": bool(
                pinned or provenance.get("source_authenticated")
            ),
            "timing_evidence_status": provenance["status"],
            "result_version_grades": dict(grades),
            "repairs": [dict(REPAIR_2019)]
            if blobs.get("2019-20") == SOURCE_BLOBS["2019-20"]
            else [],
        },
        "protocol": {
            "time_zone": "Europe/London",
            "horizons_minutes": list(horizons),
            "score": "100*(1-mean(normalized 1X2 Brier, O2.5 Brier, home O1.5 Brier, away O1.5 Brier))",
            "training": "current season to cutoff plus three preceding seasons; retain latest eligible version per fixture",
            "b0": "joint empirical scores of prior complete observed seasons in window; eligible data during warmup",
            "b1": "regularized time-decayed Dixon–Coles; hyperparameters fixed, shared across seasons",
            "decay_per_day": DECAY_PER_DAY,
            "ridge": RIDGE,
            "minimum_training_matches": 40,
            "archive_lag_hours": archive_lag_hours,
            "bootstrap_resamples": bootstrap,
            "bootstrap_unit": "season and official round; match-weighted paired score percentile interval",
            "bootstrap_seed": seed,
            "calibration": "fixed 0.2-wide bins; clamped final bin includes 1",
            "environment": {
                "python": platform.python_version(),
                "numpy": np.__version__,
                "scipy": scipy.__version__,
            },
        },
        "fixtures": {
            "unique": len(selected),
            "completed": len(finished),
            "unfinished": len(selected) - len(finished),
            "quarantined": sum(f.status == "quarantined" for f in selected),
            "start": iso(min(f.kickoff for f in selected)),
            "end": iso(max(f.kickoff for f in selected)),
        },
        "seasons": seasons,
        "horizons": stats,
        "limitations": [
            "The final OpenFootball schedule does not reconstruct earlier schedule announcements or postponements without sidecar observations.",
            "Publication and local receipt are unknown for results marked lag_assumption. Their delay from kickoff is a sensitivity assumption.",
            "Historical football-score study only; 2025-26 was previously reviewed. No prospective blind result, personnel/news performance, executable prices, fees or ROI is measured.",
        ],
    }


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, delete=False, encoding="utf-8"
    ) as stream:
        tmp = Path(stream.name)
        try:
            json.dump(payload, stream, allow_nan=False, ensure_ascii=False, indent=2)
            stream.write("\n")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, path)


def _cli_load(args):
    from . import historical_inputs

    return historical_inputs.load_for_study(
        args.data_dir, args.path, args.evidence, args.archive_lag_hours
    )


def _cli_fetch(args):
    return fetch_history(
        args.destination or args.data_dir / "openfootball",
        args.from_season,
        args.to_season,
    )


def _cli_backtest(args):
    from contextlib import closing
    from . import historical_inputs, store

    fixtures, provenance = _cli_load(args)
    stream = None
    tmp = None
    try:
        if args.details:
            args.details.parent.mkdir(parents=True, exist_ok=True)
            stream = tempfile.NamedTemporaryFile(
                "w", dir=args.details.parent, delete=False, encoding="utf-8"
            )
            tmp = Path(stream.name)

        def sink(row):
            stream.write(json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n")

        result = backtest(
            fixtures,
            args.from_season,
            args.to_season,
            horizons=tuple(args.horizons),
            bootstrap=args.bootstrap,
            seed=args.seed,
            archive_lag_hours=args.archive_lag_hours,
            details_sink=sink if stream else None,
            input_provenance=provenance,
        )
        if stream:
            stream.close()
            os.replace(tmp, args.details)
    finally:
        if stream:
            stream.close()
        if tmp:
            tmp.unlink(missing_ok=True)
    parts = [
        "pm861 --data-dir RUNTIME football backtest INPUT",
        f"--from-season {args.from_season}",
        f"--to-season {args.to_season}",
        "--horizons " + " ".join(map(str, args.horizons)),
        f"--bootstrap {args.bootstrap}",
        f"--seed {args.seed}",
    ]
    if args.archive_lag_hours is not None:
        parts.append(f"--archive-lag-hours {args.archive_lag_hours:g}")
    if args.evidence:
        parts.append("--evidence EVIDENCE.jsonl")
    result["reproduce"] = {
        "input": "For a pinned data run, use football fetch INPUT --from-season FIRST --to-season LAST, using the input blob range above.",
        "command": " ".join(parts)
        + " --summary-output SUMMARY.json --details MATCHES.jsonl",
        "source_code_sha256": {
            name: hashlib.sha256(
                Path(__file__).with_name(name).read_bytes()
            ).hexdigest()
            for name in ("football.py", "football_model.py", "historical_inputs.py")
        },
    }
    if args.summary_output:
        _write_json(args.summary_output, result)
    key = hashlib.sha256(
        json.dumps(result, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()[:16]
    with closing(store.connect(args.data_dir)) as db, db:
        historical_inputs.record(db, result)
        store.put_report(
            db, "backtest", f"{args.from_season}_{args.to_season}_{key}", result
        )
    return result


def _cli_predict(args):
    from .store import save_report

    from . import historical_inputs

    fixtures, provenance = _cli_load(args)
    if not provenance["timing_authenticated"]:
        fixtures = historical_inputs.label_research_inputs(fixtures)
    kickoff = utc(args.kickoff)
    cutoff = (
        utc(args.cutoff)
        if args.cutoff
        else kickoff - timedelta(minutes=args.cutoff_minutes)
    )
    p = FootballEngine(fixtures).forecast(
        args.home,
        args.away,
        kickoff,
        cutoff,
        season=args.season,
        lines=args.lines,
        baseline=args.baseline,
    )
    p["mode"] = "computed_forecast"
    p["input_provenance"] = provenance
    save_report(
        args.data_dir, "match", f"{p['fixture_id']}@{p['cutoff']}@{p['model']}", p
    )
    return p


def _cli_round(args):
    from .store import save_report

    fixtures, provenance = _cli_load(args)
    predictions = read_json_rows(args.predictions)
    payload = score_round(
        fixtures,
        predictions,
        season=args.season,
        round_number=args.round,
        cutoff_minutes=args.cutoff_minutes,
        as_of=args.as_of,
    )
    payload["mode"] = "imported_research"
    payload["input_provenance"] = provenance
    payload["forecast_file_sha256"] = hashlib.sha256(
        args.predictions.read_bytes()
    ).hexdigest()
    save_report(
        args.data_dir,
        "round",
        f"{args.season}_{args.round}_T-{args.cutoff_minutes}",
        payload,
    )
    return payload


def _cli_verify_repair(args):
    raw = Path(args.path).read_bytes()
    if blob_sha(raw) != SOURCE_BLOBS["2019-20"]:
        raise ValueError("requires pinned 2019-20 OpenFootball file")
    data = json.loads(raw)["matches"]
    return verify_2019_repair(data, args.comparison_csv)


def register(subparsers):
    base = subparsers.add_parser(
        "football", help="EPL score distributions, rolling backtests and round scores"
    )
    subs = base.add_subparsers(dest="football_command", required=True)
    fetch = subs.add_parser("fetch", help="download and verify pinned EPL season files")
    fetch.add_argument("destination", type=Path, nargs="?")
    fetch.add_argument("--from-season", default="2010-11")
    fetch.add_argument("--to-season", default="2026-27")
    fetch.set_defaults(func=_cli_fetch)
    repair = subs.add_parser(
        "verify-repair", help="audit the pinned 2019/20 July date correction"
    )
    repair.add_argument("path", type=Path, help="the pinned 2019-20/en.1.json")
    repair.add_argument(
        "comparison_csv", type=Path, help="independent season data file"
    )
    repair.set_defaults(func=_cli_verify_repair)

    def inputs(p):
        p.add_argument(
            "path",
            type=Path,
            help="directory with SEASON/en.1.json or one such season file",
        )
        p.add_argument(
            "--evidence",
            type=Path,
            help="NDJSON cutoff evidence with observations and schedule revisions",
        )
        p.add_argument(
            "--archive-lag-hours",
            type=float,
            help="explicit delay-from-kickoff sensitivity assumption",
        )

    bt = subs.add_parser(
        "backtest", help="evaluate fixed forecasts at each historical cutoff"
    )
    inputs(bt)
    bt.add_argument("--from-season", default="2021-22")
    bt.add_argument("--to-season", default="2025-26")
    bt.add_argument(
        "--horizons", type=int, nargs="+", default=[1440, 60], metavar="MINUTES"
    )
    bt.add_argument("--bootstrap", type=int, default=2000)
    bt.add_argument("--seed", type=int, default=861)
    bt.add_argument("--summary-output", type=Path)
    bt.add_argument(
        "--details",
        type=Path,
        help="write per-match score, forecast and evidence provenance as NDJSON",
    )
    bt.set_defaults(func=_cli_backtest)
    pred = subs.add_parser(
        "predict", help="compute one fixture's cutoff-bound score probabilities"
    )
    inputs(pred)
    pred.add_argument("--home", required=True)
    pred.add_argument("--away", required=True)
    pred.add_argument("--kickoff", required=True)
    pred.add_argument("--season")
    window = pred.add_mutually_exclusive_group()
    window.add_argument("--cutoff", help="ISO instant with explicit offset")
    window.add_argument("--cutoff-minutes", type=int, default=60)
    pred.add_argument("--lines", nargs="+", type=float, default=DEFAULT_LINES)
    pred.add_argument("--baseline", action="store_true")
    pred.set_defaults(func=_cli_predict)
    rnd = subs.add_parser(
        "round", help="grade one official round including missing and postponed matches"
    )
    inputs(rnd)
    rnd.add_argument("--season", required=True)
    rnd.add_argument("--round", type=int, required=True)
    rnd.add_argument("--cutoff-minutes", type=int, default=60)
    rnd.add_argument(
        "--as-of", help="only grade results whose availability is verified by this time"
    )
    rnd.add_argument(
        "--predictions",
        type=Path,
        required=True,
        help="cutoff-bound forecast JSON objects or NDJSON",
    )
    rnd.set_defaults(func=_cli_round)
