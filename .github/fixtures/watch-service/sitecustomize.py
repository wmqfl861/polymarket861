"""Public Gamma response fixture for the Linux service acceptance job only."""

import json
import os
from email.utils import formatdate
from pathlib import Path
from urllib.parse import urlsplit

from polymarket861 import market_http, store

SPORTS = [
    {"sport": sport, "series": series, "tags": tag, "primaryTagId": primary}
    for sport, series, tag, primary in (
        ("lal", "10193", "780", 780),
        ("bun", "10194", "1494", 1494),
        ("sea", "10203", "101962", 100136),
        ("fl1", "10195", "102070", 102070),
    )
]


def fixture_read(method, url, headers, content, timeout):
    uri = urlsplit(url)
    if method != "GET" or uri.netloc != "gamma-api.polymarket.com":
        raise AssertionError(f"unexpected CI request {method} {uri.netloc}{uri.path}")
    if Path(os.environ["PM861_CI_GAMMA_OFFLINE"]).exists():
        raise OSError("simulated temporary Gamma outage")
    if uri.path == "/sports":
        payload = SPORTS
    elif uri.path == "/events":
        payload = []
    else:
        raise AssertionError(f"unexpected CI route {uri.path}")
    now = store.now_utc()
    return json.dumps(payload).encode(), now, now, {"date": formatdate(usegmt=True)}, url


market_http.read = fixture_read
