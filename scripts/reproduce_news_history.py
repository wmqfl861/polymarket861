"""Reproduce a retrospective, paired EPL player-news study without changing production state."""

from __future__ import annotations

import argparse
import gzip
import json
import math
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from polymarket861 import experiment_statistics, football, news_ai, news_model, store

if __package__:
    from .news_history_archive import PANELS, PLAN, SEASON, capture, cohort, text_sha
else:
    from news_history_archive import PANELS, PLAN, SEASON, capture, cohort, text_sha

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUTS = (
    ROOT / "reports" / "research" / "news-wiki-2025-26-rounds-01-03.json.gz"
)


PUBLISHED_INPUTS = "85326e6eeefe3960895a46071cad1305401f98dba777f3310e8326e2441a23bc"


def load_scores_archive(history):
    games, hashes = [], {}
    for year in range(2022, 2026):
        season = f"{year}-{(year + 1) % 100:02}"
        path = Path(history) / season / "en.1.json"
        hashes[season] = football.blob_sha(path.read_bytes())
        if hashes[season] != football.SOURCE_BLOBS[season]:
            raise ValueError(
                "the score archive does not match the published OpenFootball object"
            )
        games.extend(football.load_fixtures(path, archive_lag_hours=24))
    return games, hashes


def _proof(index, ident, cutoff):
    item = index[str(ident)]
    if (
        item["id"] != ident
        or not 0 < ident
        or item["published_at"] > cutoff
        or store.instant(item["published_at"]) > store.instant(item["received_at"])
        or item["url"] != f"https://en.wikipedia.org/w/index.php?oldid={ident}"
        or len(bytes.fromhex(item["sha256"])) != 32
    ):
        raise ValueError("invalid historical source proof")
    return item


def extract_side(side, revisions, fixture, extracted_at):
    cutoff = store.iso(fixture.kickoff - timedelta(hours=1))
    if (
        side["fixture_id"] != fixture.fixture_id
        or side["cutoff"] != cutoff
        or side["team"] not in (fixture.home, fixture.away)
    ):
        raise ValueError("historical team or cutoff mismatch")
    roster = side["roster"]
    roster_ref = side["roster_revision"]
    roster_proof = _proof(revisions, roster_ref, cutoff) if roster_ref else None
    if roster and not roster_proof:
        raise ValueError("missing dated roster for identified players")
    refs = {}
    for source in side["sources"]:
        last = _proof(revisions, source["closing"], cutoff)
        if last["title"] != source["title"]:
            raise ValueError("source identity mismatch")
        if source.get("opening"):
            first = _proof(revisions, source["opening"], cutoff)
            if (
                first["title"] != last["title"]
                or first["published_at"] > last["published_at"]
            ):
                raise ValueError("source window mismatch")
        refs[last["title"]] = source
    found = []
    for story in side["stories"]:
        origin = _proof(revisions, story["origin"], cutoff)
        ref = refs.get(origin["title"])
        previous = (
            _proof(revisions, story["previous"], cutoff) if story["previous"] else None
        )
        if (
            origin["parentid"] != story["previous"]
            or not ref
            or ref["kind"] != story["known_from"]
            or origin["id"] > ref["closing"]
            or (
                previous
                and (
                    previous["title"] != origin["title"]
                    or previous["published_at"] > origin["published_at"]
                )
            )
            or text_sha(story["text"]) != story["sentence_sha256"]
        ):
            raise ValueError("news passage or revision ancestry mismatch")
        for item in news_ai.extract(
            story["text"], roster, authority="secondary", team=side["team"]
        ):
            if not item["quote"] or item["quote"] not in story["text"]:
                raise ValueError("unanchored historical extractor output")
            received = max(origin["received_at"], roster_proof["received_at"])
            if store.instant(extracted_at) < store.instant(received):
                raise ValueError(
                    "research extraction precedes its actual source receipt"
                )
            found.append(
                item
                | {
                    "id": store.digest(
                        [
                            origin["id"],
                            story["sentence_sha256"],
                            item["player_id"],
                            item["event"],
                            item["quote"],
                        ]
                    ),
                    "origin": "en.wikipedia.org",
                    "team": side["team"],
                    "authority": "secondary",
                    "published_at": origin["published_at"],
                    "received_at": received,
                    "known_at": extracted_at,
                    "historical_revision": {
                        k: origin[k] for k in ("id", "url", "sha256")
                    },
                    "url": origin["url"],
                    "layer": story["known_from"],
                    "fixture_id": fixture.fixture_id,
                    "kickoff": store.iso(fixture.kickoff),
                    "roster_seen_at": roster_proof["received_at"],
                }
            )
    return found


def _score_arm(arm, fixture):
    return football.score_probabilities(
        arm["probabilities"], fixture.home_goals, fixture.away_goals
    )


def compare(panel, history):
    fixtures, objects = load_scores_archive(history)
    selected = cohort(fixtures)
    if panel["plan"] != PLAN or len(panel["snapshot"]) != len(selected) * 2:
        raise ValueError("historical evidence panel uses another scope")
    rows = {(i["fixture_id"], i["team"]): i for i in panel["snapshot"]}
    if len(rows) != 60:
        raise ValueError("duplicate or missing historical team observations")
    engine = football.FootballEngine(fixtures)
    extracted_at = store.now_utc()
    frozen = []
    n_sides = Counter({"club_dated_stories": 0, "biography_dated_stories": 0})
    strict_source_pairs = 0
    for fixture in selected:
        cutoff = fixture.kickoff - timedelta(hours=1)
        baseline = engine.forecast(
            fixture.home, fixture.away, fixture.kickoff, cutoff, season=SEASON
        )
        if store.instant(baseline["evidence"]["latest_result_available_at"]) > cutoff:
            raise ValueError("historical results future leakage")
        both = [rows[fixture.fixture_id, name] for name in (fixture.home, fixture.away)]
        strict_source_pairs += all(
            side["roster_revision"]
            and store.instant(
                panel["revisions"][str(side["roster_revision"])]["received_at"]
            )
            <= cutoff
            and any(
                source["kind"] == "club"
                and store.instant(
                    panel["revisions"][str(source["closing"])]["received_at"]
                )
                <= cutoff
                for source in side["sources"]
            )
            for side in both
        )
        facts = [
            fact
            for side in both
            for fact in extract_side(side, panel["revisions"], fixture, extracted_at)
        ]
        for side in both:
            n_sides["club_pages_acquired"] += any(
                s["kind"] == "club" for s in side["sources"]
            )
            n_sides["rosters_parsed"] += side["roster_count"] > 0
            n_sides["selected_biographies"] += side["selected_biography_count"]
            n_sides["biographies_acquired"] += sum(
                s["kind"] == "biography" for s in side["sources"]
            )
            n_sides["source_gaps"] += len(side["gaps"])
            n_sides["examined_dated_sentences"] += len(side["stories"])
            for s in side["stories"]:
                n_sides[s["known_from"] + "_dated_stories"] += 1
        published_club_coverage = [
            side["roster_count"] > 0
            and any(row["kind"] == "club" for row in side["sources"])
            for side in both
        ]
        coverage = {
            "both_teams_observed": all(published_club_coverage),
            "as_of": "publisher_revision",
            "fixture_id": fixture.fixture_id,
        }
        arms = {}
        for mode in PANELS:
            candidates = (
                facts
                if mode == PANELS[1]
                else [x for x in facts if x["layer"] == "club"]
            )
            arms[mode] = news_model.make_pair(
                baseline,
                candidates,
                home=fixture.home,
                away=fixture.away,
                cutoff=cutoff,
                coverage=coverage,
                availability_basis=news_model.HISTORICAL,
                lines=football.DEFAULT_LINES,
            )
        # Carry both actual receipt clocks. Only the independently labelled reconstruction may use these revisions.
        strict = news_model.make_pair(
            baseline,
            facts,
            home=fixture.home,
            away=fixture.away,
            cutoff=cutoff,
            availability_basis=news_model.LOCAL,
            lines=football.DEFAULT_LINES,
        )
        frozen.append((fixture, baseline, facts, arms, strict))
    # All predictions are constructed before the target labels are scored.
    output, detail = {}, {}
    for panel_name in PANELS:
        accumulator = defaultdict(list)
        changed_deltas, matches, counters = [], [], Counter()
        round_groups = defaultdict(list)
        for fixture, baseline, facts, arms, strict in frozen:
            pair = arms[panel_name]
            candidates = (
                facts
                if panel_name == PANELS[1]
                else [f for f in facts if f["layer"] == "club"]
            )
            before, after = [
                _score_arm(pair[a], fixture) for a in ("no_news", "with_news")
            ]
            delta = after["score"] - before["score"]
            round_groups[fixture.round].append([delta])
            counters["matches_with_extracted_claims"] += bool(candidates)
            counters["extracted_claims"] += len(candidates)
            counters["accepted_player_impacts"] += len(pair["applied"])
            counters["changed_matches"] += pair["changed"]
            if pair["changed"]:
                changed_deltas.append(delta)
            for market in football.ITEM_KEYS:
                for arm, result in (("no_news", before), ("with_news", after)):
                    accumulator[(market, arm)].append(result["item_scores"][market])
            for arm, result in (("no_news", before), ("with_news", after)):
                accumulator[("overall", arm)].append(result["score"])
            matches.append(
                {
                    "fixture_id": fixture.fixture_id,
                    "round": fixture.round,
                    "home": fixture.home,
                    "away": fixture.away,
                    "cutoff": store.iso(fixture.kickoff - timedelta(hours=1)),
                    "score_90m": [fixture.home_goals, fixture.away_goals],
                    "club_coverage": [
                        any(
                            s["kind"] == "club"
                            for s in rows[fixture.fixture_id, team]["sources"]
                        )
                        for team in (fixture.home, fixture.away)
                    ],
                    "training_matches": baseline["evidence"]["training_matches"],
                    "eligible_news_claims": len(candidates),
                    "applied": [c["id"] for c in pair["applied"]],
                    "no_news_probabilities": pair["no_news"]["probabilities"],
                    "with_news_probabilities": pair["with_news"]["probabilities"],
                    "changed": pair["changed"],
                    "score_without_news": before["score"],
                    "score_with_news": after["score"],
                    "delta": delta,
                }
            )
        score_summary = {}
        for market in ("overall", *football.ITEM_KEYS):
            v = {
                key: math.fsum(accumulator[(market, key)]) / len(selected)
                for key in ("no_news", "with_news")
            }
            score_summary[market] = v | {"difference": v["with_news"] - v["no_news"]}
        interval = (
            experiment_statistics.clustered_t(list(round_groups.values()), len(PANELS))
            if changed_deltas
            else None
        )
        output[panel_name] = {
            "matches_scored": len(matches),
            **dict(counters),
            "scores": score_summary,
            "mean_delta_when_changed": math.fsum(changed_deltas) / len(changed_deltas)
            if changed_deltas
            else None,
            "paired_round_clustered_interval": interval,
            "verdict": "insufficient_historical_evidence",
        }
        detail[panel_name] = matches
    claims = [
        {k: v for k, v in fact.items() if k != "known_at"}
        | {"fixture_id": fixture.fixture_id}
        for fixture, _, facts, _, _ in frozen
        for fact in facts
    ]
    dates = [r["received_at"] for r in panel["revisions"].values()]
    return {
        "protocol": "epl-publication-time-news-retrospective-v1",
        "plan": PLAN,
        "scope": "retrospective_descriptive",
        "availability_basis": news_model.HISTORICAL,
        "study_inputs_sha256": store.digest(panel),
        "news_extractor": news_ai.MODEL_VERSION,
        "news_prior": news_model.POLICY_SHA256,
        "openfootball_commit": football.SOURCE_COMMIT,
        "score_source_objects": objects,
        "source_receipts_range": [min(dates), max(dates)] if dates else [],
        "strict_contemporaneous_news_receipt_matches": strict_source_pairs,
        "strict_changed_matches": sum(
            strict["changed"] for _, _, _, _, strict in frozen
        ),
        "prospectively_sealed_matches": 0,
        "expected_team_observations": 60,
        "source_coverage": dict(n_sides),
        "panels": output,
        "claims": claims,
        "matches": detail,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "history",
        type=Path,
        help="pinned OpenFootball YYYY-YY/en.1.json files from 2022-23 through 2025-26",
    )
    parser.add_argument(
        "--inputs",
        type=Path,
        help="a published projection, or a new projection path with --capture",
    )
    parser.add_argument(
        "--capture",
        type=Path,
        metavar="CACHE",
        help="rebuild the Wikipedia input projection using retained raw receipts",
    )
    parser.add_argument(
        "--offline-cache",
        action="store_true",
        help="read archived originals only during capture",
    )
    parser.add_argument("--workers", type=int, default=2, choices=(1, 2, 3))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    path = args.inputs or (
        args.capture / "study-inputs.json.gz" if args.capture else DEFAULT_INPUTS
    )
    if args.capture:
        games, _ = load_scores_archive(args.history)
        panel = capture(games, args.capture, args.workers, offline=args.offline_cache)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            gzip.compress(store.canonical(panel).encode(), compresslevel=9, mtime=0)
        )
    else:
        panel = json.loads(gzip.decompress(path.read_bytes()))
        if (
            path.resolve() == DEFAULT_INPUTS.resolve()
            and store.digest(panel) != PUBLISHED_INPUTS
        ):
            raise ValueError("bundled historical projection digest mismatch")
    report = (
        json.dumps(
            compare(panel, args.history), indent=2, ensure_ascii=False, allow_nan=False
        )
        + "\n"
    )
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")


if __name__ == "__main__":
    main()
