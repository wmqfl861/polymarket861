"""Replay immutable GDELT index ZIPs with the selected source checkout; fetch no article pages."""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.request import urlopen

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--src", type=Path, required=True, help="Source checkout's src directory")
parser.add_argument("--archives", type=Path, required=True, help="Local directory for the 20 GKG archives")
parser.add_argument("--download", action="store_true", help="Fetch missing GDELT index ZIPs only")
args = parser.parse_args()
sys.path.insert(0, str(args.src.resolve()))
from polymarket861 import league_press, league_registry  # noqa: E402

manifest = json.loads(Path(__file__).with_name("press-index-audit.json").read_text())
names = league_press._clubs(manifest["season"])
result = {"label_count": len(names), "single_word_labels": sum(" " not in name for name in names), "samples": {}}
for sample, records in manifest["archive_sha256"].items():
    scanned = 0
    pairs = {}
    matched_checks = defaultdict(set)
    for edition, sha in records.items():
        filename = edition + ".gkg.csv.zip"
        local = args.archives / filename
        if not local.is_file() and args.download:
            with urlopen("https://data.gdeltproject.org/gdeltv2/" + filename, timeout=30) as response:
                content = response.read(20 * 1024 * 1024 + 1)
            if len(content) > 20 * 1024 * 1024 or hashlib.sha256(content).hexdigest() != sha:
                raise ValueError("download disagrees with pinned archive: " + filename)
            local.parent.mkdir(exist_ok=True, parents=True)
            local.write_bytes(content)
        content = local.read_bytes()
        if hashlib.sha256(content).hexdigest() != sha:
            raise ValueError("archive disagrees with pinned sample: " + filename)
        # A replay clock for the parser; this is not evidence of historical local receipt.
        edition_time = datetime.strptime(edition, "%Y%m%d%H%M%S").replace(tzinfo=UTC).isoformat()
        count, links = league_press._parse(content, edition, edition_time, names)
        scanned += count
        for key, doc in links.items():
            pairs[key] = bool(doc.get("title")) or pairs.get(key, False)
            if doc["record_id"] in manifest["spot_checks"]:
                matched_checks[doc["record_id"]].add(key[1])
    result["samples"][sample] = {
        "editions": len(records), "scanned_rows": scanned,
        "distinct_club_url_pairs": len(pairs),
        "pairs_with_indexed_title": sum(pairs.values()),
        "distinct_urls": len({url for _, _, url in pairs}),
        "by_league": {lg: {
            "club_url_pairs": sum(league == lg for league, _, _ in pairs),
            "clubs": len({club for league, club, _ in pairs if league == lg}),
        } for lg in league_registry.PROFILES},
        "matched_spot_checks": {record: sorted(clubs) for record, clubs in sorted(matched_checks.items())},
    }
print(json.dumps(result, indent=2, ensure_ascii=False))
