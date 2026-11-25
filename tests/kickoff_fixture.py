"""Generate test-only, digest-pinned open publisher releases; never adds a runtime trust root."""

import hashlib
from datetime import timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

from polymarket861 import kickoff_proof as K
from polymarket861 import store


def open_release(root, fx, event, actual, *, key=None):
    event = str(event)
    key = key or "fixture-only-" + event
    folder = Path(root) / "test-fixture-release"
    folder.mkdir(exist_ok=True)
    first = store.iso(actual)
    xml = ET.Element("Games")
    ET.SubElement(
        xml,
        "Game",
        id=event,
        competition_id="8",
        competition_name="English Premier League",
        home_team_name=fx["home"],
        away_team_name=fx["away"],
        home_team_id="100",
        away_team_id="200",
        game_date=store.iso(fx["kickoff"]),
        period_1_start=first,
        period_2_end=store.iso(store.instant(first) + timedelta(minutes=100)),
    )
    data = ET.tostring(xml, encoding="utf-8")
    terms = b"Fixture-only test grant CC0-1.0."
    spec = {
        "format": "opta-f24",
        "licence": "CC0-1.0",
        "source_sha256": hashlib.sha256(data).hexdigest(),
        "licence_sha256": hashlib.sha256(terms).hexdigest(),
        "source_url": "https://open.example.test/" + key + ".xml",
        "licence_url": "https://open.example.test/cc0",
    }
    name = folder / (spec["source_sha256"] + ".xml")
    name.write_bytes(data)
    licence = folder / "licence.txt"
    licence.write_bytes(terms)
    return key, spec, name, licence


def capture_example(root, fx, event, actual):
    key, spec, file, licence = open_release(root, fx, event, actual)
    K.OPEN_RELEASES[key] = spec
    return K.import_open(
        root,
        fx["fixture_id"],
        str(event),
        key,
        file,
        licence,
        schedule_version=fx["version"],
    )
