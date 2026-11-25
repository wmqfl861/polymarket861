import unittest
from unittest.mock import patch
from polymarket861 import evidence, jobs, store
from polymarket861 import evidence_sources


class SourceRolloutTests(unittest.TestCase):
    def test_only_new_licensed_lineups_are_promoted_and_rollback_is_dated(self):
        from test_jobs import ResearchTests

        case = ResearchTests(
            "test_field_source_promoted_with_paired_independent_real_facts"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        register = evidence.register_source

        def propose(root, manifest, **kw):
            if manifest["source_id"] == "new":
                manifest = dict(
                    manifest,
                    status="candidate",
                    fields={
                        **manifest["fields"],
                        "news": {
                            "uses": ["trial", "prediction"],
                            "methods": ["manual"],
                        },
                    },
                )
            return register(root, manifest, **kw)

        with patch.object(evidence, "register_source", side_effect=propose):
            case.test_field_source_promoted_with_paired_independent_real_facts()
        when = "2031-06-03T15:00:00.000000Z"
        row = {
            "source_id": "new",
            "fixture_id": "after",
            "field": "lineup",
            "team": "Home",
            "method": "manual",
            "published_at": when,
            "received_at": when,
            "locator": "test://lineup",
            "value": {
                "confirmed": True,
                "starters": [f"Player {i}" for i in range(11)],
            },
        }

        def confirmed(at):
            return evidence.as_of_context(
                case.root,
                "after",
                "Home",
                "Away",
                at,
                purpose="prediction",
                prospective=True,
            )["confirmed"]["home"]

        with (
            patch.object(evidence, "now_utc", return_value=when),
            patch.object(evidence_sources, "now_utc", return_value=when),
        ):
            evidence.import_evidence(case.root, [row])
            self.assertTrue(confirmed(when))
            self.assertEqual(
                evidence.source_report(case.root, "new")["field_status"],
                {"lineup": "active", "news": "candidate"},
            )
        after = "2031-06-04T15:00:00.000000Z"
        with (
            patch.object(store, "now_utc", return_value=after),
            patch.object(evidence, "now_utc", return_value=after),
        ):
            jobs.rollback_candidate(
                case.root,
                "new-lineups",
                reviewer="source duty officer",
                reason="incomplete source semantics",
            )
            self.assertFalse(confirmed(after))
            self.assertTrue(confirmed(when))
