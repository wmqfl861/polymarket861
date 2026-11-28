#!/usr/bin/env python3
"""0.5.0a4 给四国调度的维基百科修订重放加了进程内缓存（V2-3）。这个探针量三件事：

  冷：进程里第一次读（每份修订都要解压、验哈希、解析）；
  热：库没有变化时再读一次（每 30 秒一轮的常态）；
  追加一份新修订之后再读一次（每小时最多一次）。

上一份审核的探针 probe_league_inputs_revision_replay_cost.py 每个库只读一次，用的是普通的 sqlite3 连接，
量到的永远是“冷”的那一次，所以对着 0.5.0a4 重跑看不出缓存的效果。这里按产品的方式开库
（league_wiki_cache.connect，每次读都新开一个连接，和 load_inputs 一样），页面的合成方法与上次相同。

用法：
  python -I probe_wiki_replay_cache_hot_path.py --src <仓库>/src <空的临时目录> [--count 2000] [--article-kb 150]

不联网，不读写仓库，不触碰任何真实数据目录。耗时与机器有关。
"""

import argparse
import hashlib
import itertools
import json
import sys
import time
import zlib
from contextlib import closing
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("scratch")
parser.add_argument("--count", type=int, default=2000)
parser.add_argument("--article-kb", type=int, default=150)
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import league_live_inputs as live  # noqa: E402
from polymarket861 import league_registry as registry  # noqa: E402
from polymarket861 import league_wiki_cache as cache  # noqa: E402

SEASON = "2026-27"
SEEN = "2026-11-27T00:00:00.000000Z"
AT = "2026-11-28T00:00:00.000000Z"
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


def insert(db, league, revision, played):
    raw = page(league, revision, played)
    db.execute("INSERT INTO wiki_receipts VALUES(?,?,?,?,?,?,?,?)",
               (league, SEASON, revision, SEEN, SEEN, live._wiki_title(league, SEASON),
                hashlib.sha256(raw).hexdigest(), zlib.compress(raw)))


def read(path, league):
    with closing(cache.connect(path)) as db:
        began = time.perf_counter()
        revisions, _ = live._wiki_history(db, league, SEASON, AT)
        return time.perf_counter() - began, len(revisions), revisions


def digest(revisions):
    h = hashlib.sha256()
    for seen, source, locator, published, scores in revisions:
        h.update(repr((seen, source, locator, published, sorted((k, scores[k]) for k in scores))).encode())
    return h.hexdigest()


def measure(league):
    path = Path(args.scratch) / f"{league}.sqlite3"
    with closing(cache.connect(path)) as db:
        db.executescript(live._SCHEMA)
        with db:
            for n in range(args.count):
                insert(db, league, 1000 + n, 120 + n % 60)
    cold, n_cold, first = read(path, league)
    hot = [read(path, league) for _ in range(5)]
    with closing(cache.connect(path)) as db, db:
        insert(db, league, 1000 + args.count, 150)
    appended, n_app, after = read(path, league)
    hot_again, _, again = read(path, league)
    return {
        "stored_revisions": args.count,
        "cold_seconds": round(cold, 4),
        "hot_seconds_median_of_5": round(sorted(h[0] for h in hot)[2], 4),
        "after_one_appended_revision_seconds": round(appended, 4),
        "hot_again_seconds": round(hot_again, 4),
        "revisions_returned": [n_cold, hot[0][1], n_app],
        "hot_result_equals_cold_result": digest(hot[0][2]) == digest(first),
        "appended_result_extends_cold_result": digest(after[:-1]) == digest(first) and digest(again) == digest(after),
    }


print(json.dumps(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "scheduler_tick_seconds": 30,
        "leagues": {league: measure(league) for league in ("bun", "sea")},
    },
    indent=1, ensure_ascii=False,
))
