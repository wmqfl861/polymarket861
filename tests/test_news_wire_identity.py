import unittest
from datetime import timedelta

from polymarket861 import news, news_sources, news_study_plan, news_wire_identity, store
import test_news_importance as data


class DirectoryIdentityTests(unittest.TestCase):
    setUp = data.UsageHistoryTests.setUp
    schedule = data.UsageHistoryTests.schedule

    def sync(self, names, directory=None, *, wire_fields=None):
        rows = [
            {
                "player_id": i,
                "display_name": name,
                "team": "Chelsea",
                "status": "injured",
                "category": "injury",
                "chance_of_playing": 0,
                **(wire_fields or {}).get(i, {}),
            }
            for i, name in names
        ]
        injury = data.reply(data.envelope(rows, store.now_utc()))
        out = news.sync(
            self.root,
            team=data.TEAM,
            season=data.SEASON,
            force=True,
            defaults=lambda t, s: [news_sources.default_sources(t, s)[0]],
            roster_refresh=lambda *_: {"status": "cached"},
            poller=lambda src, t, s, cur: news_sources.poll(
                src, t, s, cur, getter=injury
            ),
            importance_getter=data.reply(data.envelope(directory, store.now_utc()))
            if directory
            else None,
        )
        return out["streams"][0]

    def at(self, delta=None):
        self.time += timedelta(minutes=delta or 0)
        return store.iso(self.time)

    def snapshot(self, at=None):
        return news.snapshot(self.root, "next", at or self.at())

    def test_verified_native_id_gets_position_minutes_and_distinct_full_and_study_coverage(
        self,
    ):
        directory = data.usage_rows()
        directory[1].update(name="New Member", display_name="New Member")
        status = self.sync([(1, "Morgan Smith"), (2, "New Member")], directory)
        self.assertEqual(
            (
                status["state"],
                status["unmatched_players"],
                status["unresolved_players"],
            ),
            ("partial", 1, 0),
        )
        sample = self.snapshot()
        (source,) = sample["coverage"]["home"]["sources"]
        self.assertFalse(source["availability_observed"])
        self.assertTrue(news_study_plan._study_source(source))
        self.assertFalse(sample["coverage"]["home"]["observed"])
        self.assertTrue(sample["coverage"]["home"]["study_qualified"])
        (synthetic,) = [
            f for f in sample["facts"] if f["player_id"].startswith("touchline:")
        ]
        self.assertEqual(synthetic["player_id"], "touchline:epl:2")
        self.assertEqual(synthetic["position"], "goalkeeper")
        self.assertEqual(synthetic["player_importance"]["minutes"], 10)
        self.assertEqual(
            synthetic["provider_identity"]["identity_basis"], "touchline_directory_id"
        )
        self.assertEqual(
            synthetic["provider_identity"]["original_sha256"],
            status["player_usage"]["original_sha256"],
        )
        self.assertIn("New Member", synthetic["quote"])
        self.assertFalse(
            news_study_plan.timely_coverage(
                {"coverage": sample["coverage"], "input_cutoff": self.at()}
            )
        )
        # Both sides need a qualified source. A partial source cannot be made
        # eligible solely by toggling an overall counter.
        coverage = dict(
            sample["coverage"],
            away=sample["coverage"]["home"],
            both_teams_study_qualified=True,
        )
        self.assertTrue(
            news_study_plan.timely_coverage(
                {"coverage": coverage, "input_cutoff": self.at()}
            )
        )
        self.assertFalse(
            news_study_plan.timely_coverage(
                {"coverage": coverage, "input_cutoff": self.at(361)}
            )
        )

    def test_zero_starts_and_minutes_alone_create_a_claim_explicit_zero_can_exempt(
        self,
    ):
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                directory = data.usage_rows()
                directory[2].update(name="Young Player", display_name="Young Player")
                if explicit:
                    directory[2]["appearances"] = 0
                    self.at(60 * 24)
                self.sync([(3, "Young Player")], directory)
                sample = self.snapshot()
                side = sample["coverage"]["home"]
                self.assertTrue(side["study_qualified"], side)
                self.assertEqual(bool(sample["facts"]), not explicit)
                (evidence,) = side["sources"][0]["provider_identity_reviews"]
                self.assertEqual(evidence["zero_appearances"], explicit)

    def test_unknown_conflicts_and_future_directory_never_qualify_the_past(self):
        directory = data.usage_rows()
        directory[1].update(name="New Member", display_name="New Member")
        self.sync([(2, "New Member")])
        before = self.at()
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(25)
        self.sync([(2, "New Member")], directory)
        self.assertTrue(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.assertFalse(self.snapshot(before)["facts"])
        self.assertFalse(self.snapshot(before)["coverage"]["home"]["study_qualified"])
        self.at(25)
        self.sync([(2, "Wrong Person"), (999, "Unknown Person")])
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.assertFalse(self.snapshot()["facts"])

    def test_exact_directory_alias_and_named_operator_crosswalk_with_asof_withdrawal(
        self,
    ):
        directory = data.usage_rows()
        directory[1]["display_name"] = "Second Keeper"
        directory[2].update(name="Young Player", display_name="Young Player")
        self.sync([(2, "Second Keeper"), (3, "Young Player")], directory)
        first = self.at()
        initial = {f["player_id"]: f for f in self.snapshot()["facts"]}
        self.assertEqual(
            initial["enwiki:101"]["provider_identity"]["identity_basis"],
            "touchline_id_to_roster",
        )
        self.assertIn("touchline:epl:3", initial)
        self.at(25)
        with self.assertRaises(ValueError):
            news_wire_identity.register_alias(
                self.root, data.TEAM, data.SEASON, 2, "enwiki:102", "QA", "same number"
            )
        alias = news_wire_identity.register_alias(
            self.root,
            data.TEAM,
            data.SEASON,
            3,
            "enwiki:102",
            "QA",
            "verified local originals",
        )
        self.sync([(2, "Second Keeper"), (3, "Young Player")])
        after = self.at()
        later = {f["player_id"]: f for f in self.snapshot()["facts"]}
        self.assertEqual(
            later["enwiki:102"]["provider_identity"]["alias_receipt"], alias["digest"]
        )
        self.assertEqual(
            later["enwiki:102"]["player_importance"]["provider_player_id"], 3
        )
        self.assertIn(
            "touchline:epl:3", {f["player_id"] for f in self.snapshot(first)["facts"]}
        )
        self.at(25)
        news_wire_identity.register_alias(
            self.root,
            data.TEAM,
            data.SEASON,
            3,
            "enwiki:102",
            "QA",
            "withdrawn",
            withdraw=True,
        )
        self.assertNotIn(
            "enwiki:102", {f["player_id"] for f in self.snapshot()["facts"]}
        )
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.assertIn(
            "enwiki:102", {f["player_id"] for f in self.snapshot(after)["facts"]}
        )

    def test_direct_and_zero_evidence_do_not_mask_unknown_or_cross_club_ownership(self):
        from polymarket861 import news_minutes

        directory = data.usage_rows()
        directory[2].update(
            name="Young Player", display_name="Young Player", appearances=0
        )
        self.sync([(3, "Young Player"), (999, "Unknown Member")], directory)
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(25)
        self.sync([(3, "Young Player")])
        first = self.at()
        self.assertTrue(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(25)
        # Native IDs are global within this publisher. A simultaneous
        # fresh directory claiming the same ID for Everton invalidates the join.
        other = [
            dict(p, club="Everton", id=p["id"] if p["id"] == 3 else p["id"] + 80)
            for p in directory
        ]
        self.assertEqual(
            news_minutes.refresh(
                self.root,
                data.AWAY,
                data.SEASON,
                getter=data.reply(data.envelope(other, self.at())),
            )["state"],
            "captured",
        )
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.assertTrue(self.snapshot(first)["coverage"]["home"]["study_qualified"])

    def test_zero_exemption_is_rechecked_when_new_duration_or_play_is_observed(self):
        directory = data.usage_rows()
        directory[2].update(
            name="Young Player", display_name="Young Player", appearances=0
        )
        self.sync([(3, "Young Player")], directory)
        old = self.at()
        self.assertTrue(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(60 * 12 + 1)
        # Freshen just the wire, leaving the older explicit zero available as of this moment.
        self.sync([(3, "Young Player")])
        before = self.at()
        self.assertTrue(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(1)
        directory[2]["appearances"] = 1  # a sub may have zero recorded minutes
        self.assertEqual(
            data.news_minutes.refresh(
                self.root,
                data.TEAM,
                data.SEASON,
                getter=data.reply(data.envelope(directory, self.at())),
            )["state"],
            "captured",
        )
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.assertTrue(self.snapshot(before)["coverage"]["home"]["study_qualified"])
        self.assertTrue(self.snapshot(old)["coverage"]["home"]["study_qualified"])

    def test_reversed_names_with_two_live_absences_are_held_until_review(self):
        directory = data.usage_rows()
        directory[0].update(name="Smith Morgan", display_name="Smith Morgan")
        self.sync([(1, "Smith Morgan")], directory)
        self.assertEqual(
            [f["player_id"] for f in self.snapshot()["facts"]], ["touchline:epl:1"]
        )
        self.at(1)
        # A separately grounded bulletin is for the potential wiki identity.
        source = {
            "id": "club-bulletin",
            "kind": "feed",
            "license": "CC BY 4.0",
            "origin": "club.example",
            "authority": "official",
            "attribution": "Club staff",
        }
        quote = "Morgan Smith is ruled out of the next game."
        story = {
            "external_id": "report",
            "revision": "revision-1",
            "published_at": self.at(),
            "url": "https://club.example/news/injury",
            "title": "Squad news",
            "text": quote,
            "attribution": "Club",
        }

        def extract(text, players, **kwargs):
            return [
                {
                    "player_id": "enwiki:100",
                    "event": "injury",
                    "status": "confirmed",
                    "availability": "out",
                    "start": 0,
                    "end": len(text),
                    "quote": text,
                    "model_version": "fixture",
                }
            ]

        news.sync(
            self.root,
            team=data.TEAM,
            force=True,
            defaults=lambda *args: [source],
            roster_refresh=lambda *args: {"status": "cached"},
            extractor=extract,
            poller=lambda *args: {"articles": [story], "cursor": {}},
        )
        sample = self.snapshot()
        self.assertEqual([f["player_id"] for f in sample["facts"]], ["enwiki:100"])
        self.assertFalse(sample["coverage"]["home"]["study_qualified"])
        self.assertEqual(
            sample["coverage"]["home"]["roster"]["possible_duplicate_claims"],
            [{"provider_player_id": 1, "unresolved_roster_ids": ["enwiki:100"]}],
        )

    def test_new_directory_cannot_repair_lost_original_identity_receipt(self):
        from polymarket861.blob_storage import blob_path

        directory = data.usage_rows()
        directory[1].update(name="New Member", display_name="New Member")
        initial = self.sync([(2, "New Member")], directory)
        old_sha = initial["player_usage"]["original_sha256"]
        self.at(60 * 12 + 1)
        directory[0]["minutes"] -= 1
        latest = self.sync([(2, "New Member")], directory)
        self.assertNotEqual(old_sha, latest["player_usage"]["original_sha256"])
        self.assertTrue(self.snapshot()["coverage"]["home"]["study_qualified"])
        blob_path(self.root, old_sha).unlink()
        snap = self.snapshot()
        self.assertFalse(snap["facts"])
        self.assertFalse(snap["coverage"]["home"]["study_qualified"])

    def test_old_directory_is_not_current_membership_when_new_snapshot_does_not_reconcile(
        self,
    ):
        directory = data.usage_rows()
        directory[2].update(
            name="Young Player", display_name="Young Player", appearances=0
        )
        self.sync([(3, "Young Player")], directory)
        first = self.at()
        self.at(60 * 12 + 1)
        broken = [dict(r, starts=0, minutes=0) for r in directory]
        self.sync([(3, "Young Player")], broken)
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.assertTrue(self.snapshot(first)["coverage"]["home"]["study_qualified"])

    def test_competing_membership_in_incumbent_normalized_directory_blocks_new_numeric_claim(
        self,
    ):
        directory = data.usage_rows()
        directory[1].update(name="New Member", display_name="New Member")
        self.sync([(2, "New Member")], directory)
        original = self.at()
        self.assertTrue(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(25)
        # 0.4.1 saved the original (which can include appearances) and
        # a digest over its projection without the appearances field.
        other = [
            dict(
                p,
                club="Everton",
                id=p["id"] if p["id"] == 2 else p["id"] + 80,
                appearances=max(p["starts"], 1),
            )
            for p in directory
        ]
        receipt = data.news_minutes.refresh(
            self.root,
            data.AWAY,
            data.SEASON,
            getter=data.reply(data.envelope(other, self.at())),
        )
        self.assertEqual(receipt["state"], "captured")
        with news._db(self.root, write=True) as db:
            row = data.news_minutes.latest(db, data.SEASON, "everton", self.at())
            raw = store.read_blob(self.root, row["original_sha256"])
            _, projection = data.news_minutes.parse(
                raw, data.AWAY, data.SEASON, row["received_at"]
            )
            legacy = [
                {k: v for k, v in p.items() if k != "appearances"} for p in projection
            ]
            self.assertNotEqual(store.digest(legacy), row["usage_sha256"])
            db.execute(
                "UPDATE news_usage_history SET usage_sha256=? WHERE season=? AND club=? AND known_at=?",
                (store.digest(legacy), data.SEASON, "everton", row["known_at"]),
            )
        current = self.snapshot()
        self.assertFalse(current["coverage"]["home"]["study_qualified"])
        self.assertFalse(current["facts"])
        self.assertTrue(self.snapshot(original)["coverage"]["home"]["study_qualified"])

    def test_disagreeing_complete_wire_names_cannot_be_rescued_by_a_shared_display_name(
        self,
    ):
        directory = data.usage_rows()
        # These would otherwise resolve to a roster player, a provider-only
        # identity, and a zero-appearance exemption, respectively.
        directory[1].update(name="River Stone", display_name="Stone")
        directory[6].update(name="Outsider Ridge", display_name="Ridge")
        directory[2].update(name="Youth Keeper", display_name="Keeper", appearances=0)
        by_id = {2: "Stone", 7: "Ridge", 3: "Keeper"}
        fields = {pid: {"name": "Other " + value} for pid, value in by_id.items()}
        stream = self.sync(list(by_id.items()), directory, wire_fields=fields)
        self.assertEqual(stream["unresolved_players"], 3)
        self.assertFalse(stream["provider_identity_reviews"])
        self.assertFalse(self.snapshot()["facts"])
        self.assertFalse(self.snapshot()["coverage"]["home"]["study_qualified"])
        self.at(25)
        # The normal provider row sometimes includes only its official mononym.
        resolved = self.sync([(2, "Stone")])
        self.assertEqual(resolved["unresolved_players"], 0)
        self.assertEqual(self.snapshot()["facts"][0]["player_id"], "enwiki:101")

    def test_shared_multitoken_display_is_not_proof_when_native_full_names_contradict(
        self,
    ):
        directory = data.usage_rows()
        directory[1].update(name="River Stone", display_name="Team Captain")
        status = self.sync(
            [(2, "Team Captain")], directory, wire_fields={2: {"name": "Other Person"}}
        )
        self.assertEqual(status["unresolved_players"], 1)
        self.assertFalse(self.snapshot()["facts"])
