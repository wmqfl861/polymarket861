"""Frozen design sensitivity shipped with the prospective EPL endpoint declaration."""

import gzip
import json
from importlib.resources import files


ASSET = "news-endpoint-power-v2.json.gz"


def planning():
    """Return an independent copy so a local declaration can seal the full design."""
    native = files(__package__).joinpath("assets", ASSET).read_bytes()
    return json.loads(gzip.decompress(native))
