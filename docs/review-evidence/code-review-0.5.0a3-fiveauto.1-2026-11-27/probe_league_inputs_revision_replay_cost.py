#!/usr/bin/env python3
"""四国研究调度每一轮都要把本地存下来的每一份维基百科赛果页修订重新解压、验哈希、解析一遍。

league_live_inputs.load_inputs 每次调用都会走 _wiki_history：把 wiki_receipts 表里这个联赛、这个赛季的
全部修订逐份 zlib 解压、算 SHA-256、再用 _wiki_scores 解析成比分表，没有缓存。
调度服务每 30 秒对每个联赛调用一次 load_inputs，维基百科每小时轮询一次，页面有新修订就多存一份。
这个探针只量这一步的耗时随修订份数怎么涨：用和 tests/test_league_live_inputs.py 同样结构的合成页面
（真实俱乐部名单、完整的主客对阵格子；意甲那一页是整篇条目，这里给它另外垫上 --article-kb 千字节的正文），
往一张临时的 wiki_receipts 表里放 N 份修订，量 _wiki_history 走一遍要多久。

用法：
  python -I probe_league_inputs_revision_replay_cost.py --src <仓库>/src <空的临时目录> \
      [--counts 10,100,500,2000] [--article-kb 150]

不联网，不读写仓库，不触碰任何真实数据目录。耗时与机器有关，看的是随份数线性增长这一点。
"""

import argparse
import hashlib
import itertools
import json
import sqlite3
import sys
import time
import zlib
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("scratch")
parser.add_argument("--counts", default="10,100,500,2000")
parser.add_argument("--article-kb", type=int, default=150)
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import league_live_inputs as live  # noqa: E402
from polymarket861 import league_registry as registry  # noqa: E402

SEASON = "2026-27"
SEEN = "2026-11-27T00:00:00.000000Z"
FILLER = ("Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20 + "\n")


def page(league, revision, played):
    clubs = registry.members(league, SEASON)
    names = [f"|name_T{i}=[[{team}]]" for i, team in enumerate(clubs)]
    cells = [f"| match_T{h}_T{a} = " + ("1–0" if n < played else "")
             for n, (h, a) in enumerate(itertools.permutations(range(len(clubs)), 2))]
    lines = names + cells
    if league == "sea":
        lines = [FILLER * (args.article_kb * 1024 // len(FILLER))] + lines
    rev = {"revid": revision, "timestamp": SEEN, "slots": {"main": {"content": "\n".join(lines)}}}
    body = {"ns": 0 if league == "sea" else 10, "title": live._wiki_title(league, SEASON), "revisions": [rev]}
    return json.dumps({"query": {"pages": [body], "rightsinfo": {"url": live.wiki.LICENSE}}}).encode()


def measure(league, count):
    path = Path(args.scratch) / f"{league}-{count}.sqlite3"
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(live._SCHEMA)
    raw_bytes = 0
    with db:
        for n in range(count):
            raw = page(league, 1000 + n, 120 + n % 60)
            raw_bytes += len(raw)
            db.execute("INSERT INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)",
                       (league, SEASON, 1000 + n, SEEN, SEEN, live._wiki_title(league, SEASON),
                        hashlib.sha256(raw).hexdigest(), zlib.compress(raw)))
    began = time.perf_counter()
    revisions, _ = live._wiki_history(db, league, SEASON, "2026-11-28T00:00:00.000000Z")
    seconds = time.perf_counter() - began
    db.close()
    assert len(revisions) == count
    return {"stored_revisions": count, "seconds_per_load": round(seconds, 3),
            "milliseconds_per_revision": round(1000 * seconds / count, 2),
            "average_revision_kb": round(raw_bytes / count / 1024, 1)}


print(json.dumps(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "scheduler_tick_seconds": 30,
        "wikipedia_poll": "hourly; one stored row per new revision",
        "leagues": {league: [measure(league, int(n)) for n in args.counts.split(",")] for league in ("bun", "sea")},
    },
    indent=1, ensure_ascii=False,
))
