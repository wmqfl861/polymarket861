"""Test the real seal, archived input, and independent scorer boundaries."""

import json
import tempfile
import unittest
from contextlib import ExitStack, closing, contextmanager
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import (
    datasets,
    football,
    input_manifest,
    jobs_research as research,
    paired_proof as proof,
    pipeline,
    runner,
    store,
)


@contextmanager
def at(when):
    when = store.iso(when)
    with ExitStack() as stack:
        for mod in (datasets, input_manifest, pipeline, store):
            stack.enter_context(patch.object(mod, "now_utc", return_value=when))
        yield


def season_data(year, finished):
    teams = [f"Club {i:02d}" for i in range(20)]
    first = []
    for _ in range(19):
        first.append(list(zip(teams[:10], reversed(teams[10:]))))
        teams = [teams[0], teams[-1], *teams[1:-1]]
    matches = []
    for n, round_ in enumerate(first + first):
        for j, (home, away) in enumerate(round_):
            if n >= 19:
                home, away = away, home
            h, a = (n + j) % 4, (n + 2 * j) % 3
            matches.append(
                {
                    "round": f"Matchday {n + 1}",
                    "date": (date(year, 8, 12) + timedelta(days=7 * n)).isoformat(),
                    "time": "15:00",
                    "team1": home,
                    "team2": away,
                    "score": {"ft": [h, a]} if finished else {},
                }
            )
    return {"name": "English Premier League", "matches": matches}


class PairProofTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.db = self.base / "runtime"
        self.root = self.base / "source"
        self.season = "2023-24"
        self.final = season_data(2023, True)
        self.ids = [
            football.fixture_id(self.season, r["team1"], r["team2"])
            for r in self.final["matches"][:20]
        ]
        for y in (2020, 2021, 2022):
            self.write(f"{y}-{(y + 1) % 100:02}", season_data(y, True))
        self.write(self.season, season_data(2023, False))
        self.registered = {
            "B0": pipeline.register_recipe(self.db, "B0", "B0")["recipe_digest"],
            "copy": pipeline.register_recipe(self.db, "copy", "B0")["recipe_digest"],
        }

    def write(self, season, body):
        path = self.root / season / "en.1.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body))

    def observe(self, revision, when):
        blobs = {
            p.parent.name: football.blob_sha(p.read_bytes())
            for p in self.root.glob("*/en.1.json")
        }
        with at(when):
            # Simulate the downloader's public Git metadata; raw providers are
            # not contacted by this deterministic offline integration test.
            proof.attest_provider_files(self.db, revision, blobs)
            return datasets.observe(
                self.db, self.root, revision=revision, expected=blobs
            )

    def corpus(self):
        self.observe("a" * 40, "2023-08-02T09:00Z")
        self.write(self.season, self.final)
        return self.observe("b" * 40, "2024-07-02T09:00Z")

    def score(self, mode="replay", index=None, version="copy"):
        if index is None:
            index = [
                f
                for f in store.eligible_fixtures(self.db)
                if f["home_goals"] is not None
            ]
        return research._paired_evidence(
            self.db,
            {
                "version": version,
                "baseline": "B0",
                "manifest": {
                    "comparison_policy": proof.POLICY,
                    "recipe_digest": self.registered[version],
                    "baseline_recipe_digest": self.registered["B0"],
                },
            },
            mode,
            index,
            1,
        )

    def register_fixture(self, raw):
        kick = store.iso(raw["date"] + "T14:00Z")
        return store.register_fixture(
            self.db,
            {
                "fixture_id": football.fixture_id(
                    self.season, raw["team1"], raw["team2"]
                ),
                "season": self.season,
                "round_id": int(raw["round"].split()[-1]),
                "home": raw["team1"],
                "away": raw["team2"],
                "kickoff": kick,
                "known_at": "2023-08-02T12:00Z",
                "source": "test independent registry",
                "locator": "fixture schedule",
            },
        )

    def test_verified_common_historical_archive_and_tampered_cache(self):
        manifest = self.corpus()
        # Historical receipts, schedules and labels traverse the same paths as production.
        for raw in [self.final["matches"][0], self.final["matches"][10]]:
            f = self.register_fixture(raw)
            h, a = raw["score"]["ft"]
            store.record_result(
                self.db,
                dict(
                    fixture_id=f["fixture_id"],
                    home_goals=h,
                    away_goals=a,
                    source="league",
                    locator="final",
                    finalized_at="2024-07-03T15:00Z",
                    published_at="2024-07-03T15:00Z",
                    received_at="2024-07-03T15:00Z",
                ),
            )
            for version in ("B0", "copy"):
                p = pipeline.make_forecast(
                    self.db,
                    f["fixture_id"],
                    self.root,
                    mode="replay",
                    version=version,
                    evidence_path=manifest["evidence_path"],
                )
                self.assertTrue(p["comparison_proof"]["context_timely"])
                self.assertEqual(
                    p["input_manifest_sha256"], p["comparison_proof"]["snapshot"]
                )
        report = self.score()
        self.assertEqual(report["paired_count"], 2, report)
        self.assertEqual(report["intervals"]["overall"]["difference"], 0)
        fid = self.ids[0]
        candidate = next(
            r
            for r in store.evaluation_rows(self.db, "copy", mode="replay")
            if r["fixture_id"] == fid
        )
        baseline = next(
            r
            for r in store.evaluation_rows(self.db, "B0", mode="replay")
            if r["fixture_id"] == fid
        )
        official = next(
            f for f in store.eligible_fixtures(self.db) if f["fixture_id"] == fid
        )
        different = dict(
            candidate, probabilities=candidate["probabilities"] | {"draw": 0.123456}
        )
        self.assertEqual(
            proof.PairValidator(self.db).check(different, baseline, official, "replay"),
            "identical_recipe_disagrees",
        )
        first = self.ids[0]
        before = pipeline.make_forecast(
            self.db,
            first,
            self.root,
            mode="replay",
            version="copy",
            evidence_path=manifest["evidence_path"],
        )
        self.assertEqual(
            before["comparison_proof"]["last_result"], "2023-08-02T09:00:00Z"
        )
        old = self.root / "2022-23" / "en.1.json"
        changed = json.loads(old.read_bytes())
        changed["matches"][0]["score"] = {"ft": [8, 8]}
        old.write_text(json.dumps(changed))
        with self.assertRaisesRegex(proof.SealedInputConflict, "already sealed"):
            pipeline.replay_round(
                self.db, self.root, self.season, 1, version="copy", archive_lag_hours=24
            )
        self.assertEqual(
            self.score()["paired_count"], 2
        )  # previous immutable artifacts remain available

    def test_altered_inputs_and_sensitivity_stay_out_of_promotion(self):
        manifest = self.corpus()
        raw = self.final["matches"][0]
        f = self.register_fixture(raw)
        h, a = raw["score"]["ft"]
        store.record_result(
            self.db,
            dict(
                fixture_id=f["fixture_id"],
                home_goals=h,
                away_goals=a,
                source="league",
                locator="final",
                finalized_at="2024-07-03T15:00Z",
                published_at="2024-07-03T15:00Z",
                received_at="2024-07-03T15:00Z",
            ),
        )
        pipeline.make_forecast(
            self.db,
            f["fixture_id"],
            self.root,
            mode="replay",
            version="B0",
            evidence_path=manifest["evidence_path"],
        )
        previous = self.root / "2022-23" / "en.1.json"
        content = json.loads(previous.read_bytes())
        content["matches"][1]["score"]["ft"] = [7, 0]
        previous.write_text(json.dumps(content))
        pipeline.make_forecast(
            self.db,
            f["fixture_id"],
            self.root,
            mode="replay",
            version="copy",
            archive_lag_hours=24,
        )
        result = self.score()
        self.assertEqual(result["paired_count"], 0)
        self.assertEqual(
            result["comparison_rejections"][f["fixture_id"]],
            "different_or_unproved_inputs",
        )
        self.assertNotIn("intervals", result)

    def test_live_clock_and_evidence_shared_after_restart(self):
        m = self.observe("a" * 40, "2023-08-02T09:00Z")
        raw = self.final["matches"][0]
        with at("2023-08-02T12:00Z"):
            f = self.register_fixture(raw)
        active_at = store.iso("2023-08-10T10:00Z")
        with closing(store.connect(self.db)) as db, db:
            db.execute(
                "INSERT INTO trial_activations VALUES (?,?,?,?)",
                (store.PROTOCOL, active_at, active_at, "{}"),
            )
        with at("2023-08-12T12:56:00.001001Z"):
            first = runner.run_due(
                self.db,
                m["fixture_root"],
                evidence_path=m["evidence_path"],
                versions=["B0"],
                workers=1,
            )
        self.assertIn("done", first["counts"])
        with at("2023-08-12T12:57:45.953102Z"):
            second = runner.run_due(
                self.db,
                m["fixture_root"],
                evidence_path=m["evidence_path"],
                versions=["copy"],
                workers=2,
            )
        self.assertIn("done", second["counts"])
        with closing(store.connect(self.db)) as db:
            sealed = {
                r["model_version"]: json.loads(r["data_json"])
                for r in db.execute(
                    "SELECT model_version,data_json FROM forecast_seals WHERE horizon='T-60m'"
                )
            }
            frames = db.execute(
                "SELECT count(*) FROM comparison_frames WHERE horizon='T-60m'"
            ).fetchone()[0]
        self.assertEqual(frames, 1)
        self.assertNotEqual(sealed["B0"]["sealed_at"], sealed["copy"]["sealed_at"])
        self.assertEqual(sealed["B0"]["input_cutoff"], sealed["copy"]["input_cutoff"])
        self.assertEqual(
            sealed["B0"]["comparison_proof"], sealed["copy"]["comparison_proof"]
        )
        self.assertEqual(sealed["B0"]["probabilities"], sealed["copy"]["probabilities"])
        with at("2023-08-12T18:00Z"):
            h, a = raw["score"]["ft"]
            store.record_result(
                self.db,
                dict(
                    fixture_id=f["fixture_id"],
                    home_goals=h,
                    away_goals=a,
                    source="league",
                    locator="official final",
                    finalized_at="2023-08-12T17:00Z",
                    published_at="2023-08-12T17:00Z",
                    received_at="2023-08-12T18:00Z",
                ),
            )
        adjudicated = self.score(mode="formal")
        self.assertEqual(adjudicated["paired_count"], 1, adjudicated)

    def test_same_unvetted_sidecar_cannot_certify_either_arm(self):
        m = self.corpus()
        unknown = self.base / "user-supplied.jsonl"
        # Same valid records in an unobserved serialization: the claimed historical
        # clock must still be authenticated independently of either model.
        unknown.write_bytes(Path(m["evidence_path"]).read_bytes() + b"\n")
        raw = self.final["matches"][0]
        f = self.register_fixture(raw)
        h, a = raw["score"]["ft"]
        store.record_result(
            self.db,
            dict(
                fixture_id=f["fixture_id"],
                home_goals=h,
                away_goals=a,
                source="league",
                locator="final",
                finalized_at="2024-07-03T15:00Z",
                published_at="2024-07-03T15:00Z",
                received_at="2024-07-03T15:00Z",
            ),
        )
        for version in self.registered:
            pipeline.make_forecast(
                self.db,
                f["fixture_id"],
                self.root,
                evidence_path=unknown,
                mode="replay",
                version=version,
            )
        r = self.score()
        self.assertEqual(r["paired_count"], 0)
        self.assertEqual(
            r["comparison_rejections"][f["fixture_id"]], "unverified_historical_fixture"
        )

    def test_legacy_or_identical_recipe_mismatch_refused(self):
        validator = proof.PairValidator(self.db)
        self.assertEqual(
            validator.check({}, {}, {}, "formal"), "different_or_unproved_inputs"
        )
