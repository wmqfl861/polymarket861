"""Check the published retrospective news report, allowing CPython summation roundoff."""

import argparse
import hashlib
import json
import math
from pathlib import Path

# Frozen 0.4.2 scores use a 100-point scale. Integers, evidence hashes, schema,
# booleans and strings must agree exactly. No tolerance is used for live seals.
ABSOLUTE_TOLERANCE = 2e-14


def compare(reference, reproduced):
    paths = []
    leaves = 0
    largest = 0.0

    def walk(a, b, path):
        nonlocal leaves, largest
        if type(a) is not type(b):
            raise ValueError(f"different types at {path}")
        if isinstance(a, dict):
            if a.keys() != b.keys():
                raise ValueError(f"different keys at {path}")
            for key in a:
                walk(a[key], b[key], f"{path}/{key}")
        elif isinstance(a, list):
            if len(a) != len(b):
                raise ValueError(f"different length at {path}")
            for index, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{path}/{index}")
        else:
            leaves += 1
            if isinstance(a, float) and not all(map(math.isfinite, (a, b))):
                raise ValueError(f"nonfinite float at {path}")
            if a != b:
                if not isinstance(a, float) or abs(a - b) > ABSOLUTE_TOLERANCE:
                    raise ValueError(f"different value at {path}: {a!r}, {b!r}")
                largest = max(largest, abs(a - b))
                paths.append(path)

    left, right = reference.read_bytes(), reproduced.read_bytes()
    walk(json.loads(left), json.loads(right), "")
    return {
        "reference_sha256": hashlib.sha256(left).hexdigest(),
        "reproduced_sha256": hashlib.sha256(right).hexdigest(),
        "byte_identical": left == right,
        "absolute_tolerance": ABSOLUTE_TOLERANCE,
        "leaf_values": leaves,
        "differing_float_values": len(paths),
        "largest_absolute_float_difference": largest,
        "differing_paths": paths,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("reproduced", type=Path)
    args = parser.parse_args()
    print(json.dumps(compare(args.reference, args.reproduced), indent=2))
