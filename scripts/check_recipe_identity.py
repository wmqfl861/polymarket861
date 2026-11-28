"""Verify released football recipe identities and compare supported CPython minors."""

import argparse
import json
import platform
from pathlib import Path

from polymarket861 import football, model_study, pipeline, store


def capture():
    recipes = {}
    for family in pipeline.FAMILIES:
        primary = pipeline._build_recipe(family, football.DEFAULT_LINES)
        paired = pipeline._study_recipe(primary)
        recipes[family] = {
            "primary": store.digest(primary),
            "paired": store.digest(paired),
            "probability_source": store.digest(paired["news_probability_source"]),
            "frozen_feature_source": store.digest(paired["news_frozen_feature_source"]),
        }
    return {
        "python": platform.python_version(),
        "numeric_runtime": model_study.numeric_runtime(),
        "package_files_sha256": store.digest(
            model_study.release_provenance()["package_files"]
        ),
        "recipes": recipes,
    }


def verify_baseline(recipes, baseline):
    expected = baseline["recipes"]
    families = {"B0", "B1", "B2", "B3"}
    if set(recipes) != families or set(expected) != families:
        raise ValueError("football baseline requires families B0, B1, B2, B3")
    changed = []
    for family in sorted(families):
        if set(expected[family]) != {"primary", "paired"}:
            raise ValueError(f"invalid football baseline for {family}")
        for kind in ("primary", "paired"):
            if recipes[family].get(kind) != expected[family][kind]:
                changed.append(f"{family}/{kind}")
    if changed:
        raise ValueError("football recipes differ from released baseline: " + ", ".join(changed))


def compare(files):
    rows = sorted((json.loads(p.read_text()) for p in files), key=lambda r: r["python"])
    if len(rows) != 3 or {".".join(r["python"].split(".")[:2]) for r in rows} != {
        "3.11",
        "3.12",
        "3.13",
    }:
        raise ValueError("expected one result from each of CPython 3.11, 3.12 and 3.13")
    for field in ("numeric_runtime", "package_files_sha256", "recipes"):
        if any(row[field] != rows[0][field] for row in rows[1:]):
            raise ValueError("different " + field + " across interpreters")
    return {
        "reference_python": model_study.REFERENCE_PYTHON,
        "interpreters": [r["python"] for r in rows],
        "numeric_runtime": rows[0]["numeric_runtime"],
        "package_files_sha256": rows[0]["package_files_sha256"],
        "recipes": rows[0]["recipes"],
        "parts_that_differ_between_interpreters": [],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compare", nargs="+", type=Path)
    parser.add_argument("--approved", type=Path, help="pinned football release recipe digests")
    args = parser.parse_args()
    result = compare(args.compare) if args.compare else capture()
    if args.approved:
        verify_baseline(result["recipes"], json.loads(args.approved.read_text()))
    print(json.dumps(result, indent=2))
