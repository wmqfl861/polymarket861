"""Print the digest of the built-in B1 recipe and of its parts under the running interpreter.

Run it with several Python minor versions against the same checkout and the same
NumPy/SciPy versions; a part that differs between interpreters depends on the interpreter.

Usage: python -I check_recipe_digest.py --src <repo>/src
"""

import argparse
import json
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", required=True)
    args = parser.parse_args()
    sys.path.insert(0, args.src)
    import numpy
    import scipy

    import polymarket861
    from polymarket861 import pipeline, store

    recipe = pipeline._build_recipe("B1", (0.5, 1.5, 2.5, 3.5))
    parts = {}
    for key, value in sorted(recipe.items()):
        text = json.dumps(value, sort_keys=True, default=str)
        parts[key] = value if len(text) <= 80 else store.digest(value)
    print(json.dumps({
        "package": polymarket861.__version__,
        "python": sys.version.split()[0], "numpy": numpy.__version__, "scipy": scipy.__version__,
        "recipe_sha256": store.digest(recipe),
        "parts": parts,
    }, indent=1, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
