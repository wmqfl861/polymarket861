import json
import tempfile
import unittest
from concurrent.futures import Future
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlencode

import test_big5_market as fixture
from polymarket861 import big5_market as big5, market_archive as archive, market_provider as provider, store
from polymarket861 import market_watch_health as health


CUTOFF = provider.moment(fixture.DAY + "T13:00:00Z")


class TestCatalogRecovery(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name)
        self.offset = -900
        self.calls = []
        self.change = None
        stack = ExitStack()
        self.addCleanup(stack.close)
        for module in (store, big5, archive, fixture):
            stack.enter_context(patch.object(module, "now_utc", side_effect=self.now))
        stack.enter_context(patch.object(provider, "get", side_effect=self.upstream))

    def now(self):
        return store.iso(CUTOFF + timedelta(seconds=self.offset))

    def upstream(self, url, params=None, **kwargs):
        self.calls.append((self.offset, url))
        reply = fixture.gamma(url, params, **kwargs)
        return self.change(url, reply) if self.change else reply

    def catalog(self):
        big5.catalog(self.data, ["sea"], fixture.START, fixture.END)
        self.calls.clear()

    def watch(self, offset):
        self.offset = offset
        return big5.watch(self.data, ["sea"], fixture.START, fixture.END, discover=False)

    def weekly(self):
        self.offset = 60
        return big5.weekly_report(
            self.data,
            store.iso(CUTOFF - timedelta(seconds=30)),
            store.iso(CUTOFF + timedelta(seconds=15)),
        )

    def book_calls(self):
        return [offset for offset, url in self.calls if url.endswith("/books")]

    def test_stale_main_and_sibling_renewed_without_broad_discovery(self):
        self.catalog()
        self.assertEqual(self.watch(-20)["states"], {"complete": 1})
        self.assertEqual(self.watch(5)["states"], {"complete": 1})
        self.assertEqual(
            [url.rsplit("/", 1)[-1] for _, url in self.calls],
            [fixture.BASE, fixture.BASE + "-more-markets", "books", "books"],
        )
        self.assertEqual(self.weekly()["fixed_window_gaps"], [])

    def test_outage_deferrals_spare_three_book_attempts_and_keep_all_causes(self):
        self.catalog()

        def outages(url, reply):
            if self.offset <= -18:
                raise OSError("Gamma offline during decision")
            if url.endswith("/books"):
                raise OSError("CLOB rejected batch")
            return reply

        self.change = outages
        for offset in range(-24, -6, 2):
            self.watch(offset)
        self.assertEqual(self.book_calls(), [-16, -14, -12])
        weekly = self.weekly()
        self.assertEqual(weekly["by_league"]["sea"]["fixed_windows_due"], 2)
        decision, execution = weekly["fixed_window_gaps"]
        self.assertEqual(len(decision["attempts"]), 7)
        self.assertEqual(decision["required_core_tokens"], 12)
        self.assertTrue(any("older than ten minutes" in r for r in decision["refresh_triggers"]))
        self.assertTrue(any("Gamma offline" in r for r in decision["reasons"]))
        self.assertTrue(any("CLOB rejected batch" in r for r in decision["reasons"]))
        self.assertEqual(execution["reasons"], ["no capture attempt recorded"])

    def test_stale_pair_in_sustained_outage_yields_no_books_in_either_window(self):
        self.catalog()

        def offline(url, reply):
            if "/events/slug/" in url:
                raise OSError("no route to Gamma")
            return reply

        self.change = offline
        for offset in (-24, -22, -20, -18, -16, -14, -1, 0, 5, 7, 9, 10):
            self.watch(offset)
        self.assertEqual(self.book_calls(), [])
        gaps = self.weekly()["fixed_window_gaps"]
        self.assertEqual([len(g["attempts"]) for g in gaps], [7, 3])
        self.assertTrue(all(g["required_core_tokens"] == 12 for g in gaps))
        self.assertTrue(all(any("no route" in r for r in g["reasons"]) for g in gaps))

    def test_claim_revalidates_hard_deadline_after_slow_live_refresh(self):
        self.catalog()

        def slow_auxiliary(url, reply):
            if url.endswith("-more-markets"):
                self.offset = 0
            return reply

        self.change = slow_auxiliary
        leg = self.watch(-1)["last_jobs"][0]
        self.assertIn("deadline elapsed", leg["error"])
        self.assertEqual(self.book_calls(), [])
        decision = self.weekly()["fixed_window_gaps"][0]
        self.assertIn("fixture catalogue refresh completed at", decision["reasons"][0])
        self.assertNotIn("late_book_receipts", decision["attempts"][0])
        self.change = None
        self.assertEqual(self.watch(5)["states"], {"complete": 1})

    def test_native_book_arrival_on_both_sides_of_each_fixed_deadline(self):
        base = self.data
        for hours in (1, 24):
            target = (1 - hours) * 3600
            for micros in (-1, 0, 1):
                with self.subTest(hours=hours, micros=micros):
                    self.data = base / f"{hours}_{micros}"
                    self.offset, self.change = target - 810, None
                    self.catalog()

                    def network(url, reply):
                        if url.endswith("/books"):
                            self.offset += 1 + micros / 1_000_000
                            return replace(reply, received_at=self.now())
                        return reply

                    self.change = network
                    legs = [self.watch(target + offset)["last_jobs"][0] for offset in (-1, 9)]
                    self.offset = target + 30
                    weekly = big5.weekly_report(
                        self.data, store.iso(CUTOFF + timedelta(seconds=target - 25)), self.now()
                    )
                    counts = weekly["by_league"]["sea"]
                    self.assertEqual(counts["fixed_windows_complete"], 2 if micros < 0 else 0)
                    self.assertEqual(counts[f"t{hours}_pairs_complete"], int(micros < 0))
                    gaps = weekly["fixed_window_gaps"]
                    if micros < 0:
                        self.assertEqual(gaps, [])
                        continue
                    self.assertTrue(gaps[0]["refresh_triggers"])
                    self.assertEqual(gaps[1]["refresh_triggers"], [])
                    for leg, gap in zip(legs, gaps, strict=True):
                        late, = gap["attempts"][0]["late_book_receipts"]
                        self.assertEqual(leg["late_book_receipts"], [late])
                        self.assertEqual(late["received_tokens"], 14)
                        self.assertEqual(late["cutoff_exclusive"], gap["window"][1])
                        self.assertEqual(late["late_by_microseconds"], micros)
                        self.assertEqual(gap["reasons"], [late["reason"]])
                        self.assertIn("14 native CLOB token books arrived late", late["reason"])

    def test_missing_native_batch_never_uses_a_cached_late_slot_diagnosis(self):
        self.catalog()

        def slow(url, reply):
            if url.endswith("/books"):
                self.offset = 0
                return replace(reply, received_at=self.now())
            return reply

        self.change = slow
        self.assertTrue(self.watch(-1)["last_jobs"][0]["late_book_receipts"])
        with closing(big5._db(big5.research_root(self.data))) as db, db:
            db.execute("DELETE FROM ma_requests WHERE operation='books'")
        decision = self.weekly()["fixed_window_gaps"][0]
        self.assertNotIn("late_book_receipts", decision["attempts"][0])
        self.assertFalse(any("arrived late" in reason for reason in decision["reasons"]))
        self.assertTrue(decision["refresh_triggers"])

    def test_auxiliary_failure_rechecks_both_even_if_one_old_receipt_is_fresh(self):
        self.catalog()
        self.offset = -40
        # Refresh only the auxiliary receipt through a genuine call to its native endpoint.
        root = big5.research_root(self.data)
        slug = fixture.BASE + "-more-markets"
        obj, reply, sha = archive._fetch(root, "league_event", slug, provider.GAMMA + "/events/slug/" + slug)
        with closing(big5._db(root)) as db, db:
            big5._record(db, obj, big5._identity(obj, "sea"), reply, sha)
        self.calls.clear()

        def mismatch(url, reply):
            if url.endswith("-more-markets"):
                obj = json.loads(reply.body)
                obj["id"] = "5432"
                return replace(reply, body=json.dumps(obj).encode())
            return reply

        self.change = mismatch
        self.watch(-22)
        self.watch(-20)
        self.assertEqual(self.book_calls(), [])
        self.change = None
        self.assertEqual(self.watch(-18)["states"], {"complete": 1})
        self.assertEqual(sum(url.endswith("-more-markets") for _, url in self.calls), 3)

    def test_stale_http_or_changed_domestic_participant_never_reaches_books(self):
        from polymarket861 import league_live_inputs

        self.catalog()

        def invalid(url, reply):
            if "/events/slug/" in url:
                if self.offset == -20:
                    return replace(reply, headers=dict(reply.headers, age="601"))
                raw = json.loads(reply.body)
                raw["teams"][0]["id"] = 999
                return replace(reply, body=json.dumps(raw).encode())
            return reply

        self.change = invalid
        self.assertIn("fresh Gamma", self.watch(-20)["last_jobs"][0]["error"])
        self.assertIn("participant IDs changed", self.watch(-18)["last_jobs"][0]["error"])
        self.assertEqual(self.book_calls(), [])
        root = big5.research_root(self.data)
        with closing(big5._db(root)) as db:
            self.assertTrue(league_live_inputs._identity_conflicted(db, fixture.BASE, self.now()))
        self.offset = -16
        self.change = None
        self.catalog()
        with closing(big5._db(root)) as db:
            self.assertFalse(league_live_inputs._identity_conflicted(db, fixture.BASE, self.now()))

    def test_main_and_auxiliary_schedule_changes_do_not_capture_old_kickoff(self):
        base = self.data
        for side in ("main", "auxiliary", "both"):
            with self.subTest(side=side):
                self.data = base / side
                self.offset, self.change = -900, None
                self.catalog()

                def move(url, reply):
                    auxiliary = url.endswith("-more-markets")
                    if "/events/slug/" in url and (side == "both" or auxiliary == (side == "auxiliary")):
                        raw = json.loads(reply.body)
                        raw["startTime"] = fixture.DAY + "T15:00:00Z"
                        for market in raw["markets"]:
                            market["gameStartTime"] = raw["startTime"]
                        return replace(reply, body=json.dumps(raw).encode())
                    return reply

                self.change = move
                leg = self.watch(-15)["last_jobs"][0]
                self.assertEqual(leg["state"], "partial")
                self.assertEqual(self.book_calls(), [])
                self.assertTrue("kickoff" in leg["error"] or "schedule" in leg["error"])
                if side == "both":
                    self.assertEqual(self.watch(3600 - 15)["states"], {"complete": 1})

    def test_contract_change_between_selection_and_dispatch_blocks_the_batch(self):
        self.catalog()
        capture = archive.capture

        def revised(root, fixture_slug, **kwargs):
            def changed_market(url, reply):
                if "/events/slug/" in url:
                    obj = json.loads(reply.body)
                    obj["markets"][0]["description"] = "different native settlement terms"
                    return replace(reply, body=json.dumps(obj).encode())
                return reply

            self.offset += 1
            self.change = changed_market
            big5._refresh(root, fixture_slug)
            return capture(root, fixture_slug, **kwargs)

        with patch.object(archive, "capture", revised):
            leg = self.watch(-20)["last_jobs"][0]
        self.assertTrue(leg["catalog_deferred"])
        self.assertIn("changed before book request", leg["error"])
        self.assertEqual(self.book_calls(), [])

    def test_older_slot_summary_recovers_the_specific_cause_from_original_runs(self):
        self.catalog()

        def offline(url, reply):
            raise OSError("Gamma unavailable")

        self.change = offline
        self.watch(-18)
        root = big5.research_root(self.data)
        with closing(big5._db(root)) as db, db:
            row = db.execute("SELECT id,report FROM big5_runs").fetchone()
            run = json.loads(row["report"])
            run["event_gaps"] = run.pop("refresh_trigger")
            run["error"] = "unverified main event or observed live/ended"
            del run["claimed_kickoff"], run["catalog_deferred"]
            db.execute("UPDATE big5_runs SET report=? WHERE id=?", (store.canonical(run), row["id"]))
            db.execute("UPDATE big5_slots SET report=?", (store.canonical({"error": run["error"]}),))
            db.execute("DROP TABLE big5_catalog_deferrals")
            db.execute("DELETE FROM runtime_schema_versions WHERE component='five-league-market-v1'")
        first, _ = self.weekly()["fixed_window_gaps"]
        self.assertTrue(any("older than ten minutes" in reason for reason in first["reasons"]))
        self.assertEqual(first["attempts"][0]["requested_tokens"], 0)
        self.assertEqual(first["required_core_tokens"], 12)

    def test_real_retry_after_always_takes_precedence_over_a_two_second_retry(self):
        self.catalog()

        def denied(url, reply):
            if "/events/slug/" in url:
                raise HTTPError(url, 429, "rate limited", {"Retry-After": "90"}, None)
            return reply

        self.change = denied
        leg = self.watch(-24)["last_jobs"][0]
        self.assertTrue(leg["catalog_deferred"])
        self.assertIn("Retry-After", leg["error"])
        self.assertEqual(self.watch(-22)["states"], {})
        self.assertEqual(self.watch(5)["states"], {"partial": 1})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.book_calls(), [])

    def test_stale_middle_page_keeps_previous_country_success_and_unaffected_peer(self):
        tags = {tag: sport for sport, (_, tag) in big5.MARKETS.items()}

        def upstream(url, params=None, **kwargs):
            if url.endswith("/sports"):
                reply = fixture.gamma(url)
                raw = [{"sport": s, "series": big5.MARKETS[s][0], "tags": big5.MARKETS[s][1]}
                       for s in ("sea", "bun")]
                return replace(reply, body=json.dumps(raw).encode(), headers=dict(reply.headers, age="586"))
            if url.endswith("/events"):
                sport = tags[params["tag_id"]]
                offset = params["offset"]
                reply = fixture.gamma(url, {**params, "tag_id": big5.MARKETS["sea"][1]})
                raw = [fixture.native(league=sport), fixture.native(True, league=sport)][offset:offset+1]
                headers = dict(reply.headers)
                if self.offset == -20 and offset == 1 and sport == "sea":
                    headers["age"] = "3600"
                return replace(reply, body=json.dumps(raw).encode(), headers=headers, url=url+"?"+urlencode(params))
            return fixture.gamma(url, params, **kwargs)

        leagues = ["sea", "bun"]
        scope = (leagues, store.iso(fixture.START), store.iso(fixture.END))
        root = big5.research_root(self.data)
        with (patch.object(provider, "get", side_effect=upstream),
              health.monitor(root, *scope, continuous=True, discover=True) as tick):
            initial = big5.catalog(self.data, *scope, page_size=1)
            old = self.now()
            tick(1, initial)
            self.offset = -20
            result = big5.catalog(self.data, *scope, page_size=1)
            tick(2, result)
            status = health.status(root, *scope)
        sea, bun = result["leagues"]["sea"], result["leagues"]["bun"]
        self.assertTrue(sea["reached_end"])
        self.assertFalse(sea["page_walk_complete"])
        self.assertEqual([page["http_fresh"] for page in sea["pages"]], [True, False, True])
        self.assertEqual((sea["gaps"][0]["offset"], sea["next_offset"]), (1, 1))
        self.assertTrue(bun["page_walk_complete"])
        self.assertEqual(status["catalog_by_league"]["sea"]["last_success_at"], old)
        self.assertEqual(status["catalog_by_league"]["bun"]["last_success_at"], self.now())
        self.assertEqual(self.watch(-19.9)["states"], {"complete": 1})
        self.assertEqual([url.rsplit("/",1)[-1] for _,url in self.calls],
                         [fixture.BASE, fixture.BASE+"-more-markets", "books"])

    def test_undated_legacy_catalog_and_undated_refresh_cannot_authorize_a_book(self):
        self.offset = -45
        self.catalog()
        with closing(big5._db(big5.research_root(self.data))) as db, db:
            db.execute("DELETE FROM big5_catalog_http")
            db.execute("UPDATE observations SET metadata=json_remove(metadata,'$.http_date') "
                       "WHERE json_extract(metadata,'$.operation')='league_catalog'")
        self.change = lambda url, reply: replace(reply, headers={}) if "/events/slug/" in url else reply
        leg = self.watch(-20)["last_jobs"][0]
        self.assertTrue(leg["catalog_deferred"])
        self.assertIn("fresh Gamma", leg["error"])
        self.assertTrue(any("unverifiable" in item["error"] for item in leg["refresh_trigger"]))
        self.assertEqual(self.book_calls(), [])

    def test_twenty_four_hour_and_nondefault_execution_delay(self):
        self.offset = -23 * 3600 - 1200

        def eight_seconds(url, reply):
            if "/events" in url:
                data = json.loads(reply.body)
                for event in (data if isinstance(data, list) else [data]):
                    for market in event["markets"]:
                        market["secondsDelay"] = 8
                return replace(reply, body=json.dumps(data).encode())
            return reply

        self.change = eight_seconds
        self.catalog()
        leg = self.watch(-23 * 3600 - 20)["last_jobs"][0]
        self.assertEqual((leg["phase"], leg["state"]), ("t24_decision", "complete"))
        leg = self.watch(-23 * 3600 + 8)["last_jobs"][0]
        self.assertEqual((leg["phase"], leg["state"]), ("t24_execution_8s", "complete"))

    def test_refresh_completion_drives_retry_after_30_and_success_after_300(self):
        done = []

        def refresh(*args, **kwargs):
            started = self.offset
            self.offset += 7
            complete = len(done) != 1
            done.append((started, self.offset, complete))
            return {"leagues": {"sea": {"page_walk_complete": complete, "gaps": []}}}

        class Pool:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def submit(self, func, *args, **kwargs):
                task = Future()
                task.set_result(func(*args, **kwargs))
                return task

        def sleep(seconds):
            self.assertGreater(seconds, 0)
            self.offset += seconds

        with (patch.object(big5, "catalog", refresh),
              patch.object(big5, "ThreadPoolExecutor", Pool),
              patch.object(big5, "time", SimpleNamespace(sleep=sleep))):
            big5.watch(self.data, ["sea"], fixture.START, fixture.END, interval=60, cycles=18)
        self.assertGreaterEqual(len(done), 4)
        self.assertEqual([done[i+1][0] - done[i][1] for i in range(3)], [300, 30, 300])
