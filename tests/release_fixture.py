"""Small deterministic, locally retained benchmark for release-gate integration tests."""

from contextlib import closing, contextmanager
from unittest.mock import patch
import gzip

from polymarket861 import model_study, store


@contextmanager
def compared(root):
    before, old, after, new = model_study._release_versions(root)
    sample = ("2022-23",)
    with patch.object(model_study, "BENCHMARK", sample):
        for digest in (old, new):
            rows = [
                [
                    f"game-{i}",
                    sample[0],
                    1 + i // 10,
                    "2023-01-01T14:00Z",
                    [1, 0],
                    "same-input",
                    [0.4, 0.3, 0.3, 0.5, 0.5, 0.5],
                    "all-lines",
                ]
                for i in range(380)
            ]
            doc = {
                "format": 1,
                "method": model_study.METHOD,
                "seasons": list(sample),
                "inputs": {sample[0]: "controlled test"},
                "recipe": digest,
                "rows": rows,
            }
            raw = gzip.compress(store.canonical(doc).encode(), mtime=0)
            sha = store.archive_bytes(root, "test-study", raw)["sha256"]
            with closing(model_study._db(root)) as db, db:
                db.execute(
                    "INSERT OR IGNORE INTO probability_studies VALUES(?,?,?)",
                    (digest, store.digest(sample), sha),
                )
        with patch.object(model_study, "measure"), patch.object(model_study, "require_reference_python"):
            report = model_study.release_report(root, None)
        yield report


def approved(root):
    with compared(root) as report:
        return model_study.approve_release(
            root,
            report["report_sha256"],
            "release steward",
            "Verified the same five scored endpoints for both builds",
        )["version"]
