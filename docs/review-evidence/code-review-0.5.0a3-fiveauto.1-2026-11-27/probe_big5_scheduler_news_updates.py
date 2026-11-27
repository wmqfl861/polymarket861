#!/usr/bin/env python3
"""四国研究调度（big5_scheduler）里，“新闻更新”封存是怎么被触发的，以及调度停了以后状态页显示什么。

一、新闻更新。_changes 把名单、GDELT 链接、球员状态三份摘要合成一个“证据指纹”，指纹和上一份
预览或新闻更新不一样就再封一份 news_update。四国的名单快照 facts 恒为空、
both_teams_observed 恒为 False，API-Football 没配密钥时球员状态也是空的，所以概率不会变。
这里用真实的 _news、真实的 league_press.collect 和 fixture_news_as_of，给它喂自己合成的 GDELT
索引（每 15 分钟一期，按 --links-per-day 的速度出现提到两家俱乐部之一的新链接），
从开球前约 4.8 天走到赛后收到比分，记录：封了多少份新闻更新、其中有几份概率和预览不同、
每份多大、收到比分后写了多少条评分。名单库是空的，所以每份封存的体积不含两队名单
（真实部署里每份还要多带两队约 60 名球员的名单）。

二、状态。调度正常跑完上面这一段后，把时钟往后拨 3 天且不再调度，读 big5_scheduler.status，
看它报什么状态、有没有“多久没跑了”的字段。

三、轮询异常。run(--repeat) 把名单和球员状态轮询交给线程池，返回的 future 没有人读。
让名单采集抛一个 _poll 没有接住的异常（TypeError，例如来源返回了意料之外的结构），
跑三轮调度，看状态里有没有任何报错。

用法：
  python -I probe_big5_scheduler_news_updates.py --src <仓库>/src <空的临时目录> \
      [--links-per-day 0,2,6,24]

不联网，不读写仓库，不触碰任何真实数据目录。合成链接的域名是 example.com。
"""

import argparse
import hashlib
import io
import json
import sqlite3
import sys
import zipfile
from contextlib import ExitStack, closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("scratch")
parser.add_argument("--links-per-day", default="0,2,6,24")
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import big5_scheduler as scheduler  # noqa: E402
from polymarket861 import football, league_press, league_rosters, market_provider, store  # noqa: E402

START = store.instant("2026-11-27T00:00:00Z")
KICKOFF = store.instant("2026-12-01T19:30:00Z")
HOME, AWAY = "FC Bayern München", "SV Werder Bremen"
QUARTER = timedelta(minutes=15)


def match(home, away, kick, known):
    fid = football.fixture_id("2026-27", home, away, league="bun")
    return football.Fixture(
        fid, "2026-27", 12, kick, home, away,
        schedule_known_at=known, schedule_versions=((known, kick),), schedule_grades=("observed_schedule",),
    )


def edition_zip(edition, urls):
    lines = []
    for n, url in enumerate(urls):
        fields = [f"{edition}-{n}", edition, "1", "example.com", url, *[""] * 8, "", ""]
        lines.append("\t".join(fields))
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(edition + ".gkg.csv", "\n".join(lines) + ("\n" if lines else ""))
    return raw.getvalue()


def run(rate, index):
    root = Path(args.scratch) / f"rate-{index}"
    root.mkdir(parents=True)
    sim = SimpleNamespace(now=START, upcoming=True)
    fixture = match(HOME, AWAY, KICKOFF, START - timedelta(days=3))
    past = match(AWAY, HOME, START - timedelta(days=40), START - timedelta(days=60))
    previous = replace(past, status="completed", result_versions=(
        football.ResultVersion(past.kickoff + timedelta(hours=3), (2, 1), "observed", "archive:past"),))
    state = {"fixture": fixture}
    editions = {}

    def inputs(_root, code, history, market_root):
        current = state["fixture"]
        binding = {"slug": "bun-bay-svw", "event_id": "11", "home_id": "8", "away_id": "9",
                   "kickoff": store.iso(KICKOFF), "receipt": store.iso(START - timedelta(days=3)), "sha256": "a" * 64}
        return SimpleNamespace(
            fixtures=(previous, current), upcoming=(current,) if sim.upcoming else (),
            provenance={"market_fixtures": {current.fixture_id: binding}},
        )

    def get(url, params=None, **kwargs):
        now = store.iso(sim.now)
        if url.endswith("lastupdate.txt"):
            edition = max(editions)
            body = editions[edition]
            text = (f"{len(body)} {hashlib.md5(body, usedforsecurity=False).hexdigest()} "
                    f"http://data.gdeltproject.org/gdeltv2/{edition}.gkg.csv.zip\n")
            return market_provider.Reply(text.encode(), now, now, {}, url)
        edition = url.rsplit("/", 1)[1].split(".")[0]
        return market_provider.Reply(editions[edition], now, now, {}, url)

    out = {"links_per_day_for_the_two_clubs": rate}
    every = None if not rate else max(1, round(96 / rate))
    published = 0
    step = 0
    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler, "_clock", side_effect=lambda: sim.now))
        stack.enter_context(patch.object(scheduler, "_load", side_effect=inputs))
        stack.enter_context(patch.object(scheduler, "_pregame", side_effect=lambda *a: True))
        stack.enter_context(patch.object(store, "now_utc", side_effect=lambda: store.iso(sim.now)))
        while sim.now < KICKOFF - timedelta(minutes=10):
            urls = []
            if every and step % every == 0:
                club = ("bayern-munich", "sv-werder-bremen")[published % 2]
                urls.append(f"https://example.com/football/{club}-story-{published}")
                published += 1
            edition = (sim.now - timedelta(minutes=sim.now.minute % 15, seconds=sim.now.second)).strftime("%Y%m%d%H%M%S")
            editions[edition] = edition_zip(edition, urls)
            report = league_press.collect(root, quarters=1, get=get)
            assert report["state"] == "ok", report
            result = scheduler.tick(root, leagues=["bun"], market_state_dir=root / "market", collect_news=False)
            assert result["leagues"]["bun"]["state"] == "ok", result
            # 固定窗口只有 5 分钟，窗口里按服务的 30 秒间隔多调度几次。
            for horizon in (timedelta(hours=24), timedelta(hours=1)):
                opens = KICKOFF - horizon - scheduler.WINDOW
                if sim.now < opens < sim.now + QUARTER:
                    saved = sim.now
                    sim.now = opens + timedelta(seconds=30)
                    scheduler.tick(root, leagues=["bun"], market_state_dir=root / "market", collect_news=False)
                    sim.now = saved
            sim.now += QUARTER
            step += 1
        # 赛后收到比分，调度给每一份封存各写一条评分。
        sim.now = KICKOFF + timedelta(hours=3)
        sim.upcoming = False
        state["fixture"] = replace(fixture, status="completed", result_versions=(
            football.ResultVersion(sim.now, (3, 1), "observed", "archive:final"),))
        scheduler.tick(root, leagues=["bun"], market_state_dir=root / "market", collect_news=False)
        with closing(sqlite3.connect(root / "forecast-scheduler" / "bun" / "runtime.sqlite3")) as db:
            rows = db.execute("SELECT horizon,state,sealed,payload FROM domestic_jobs ORDER BY sealed").fetchall()
            scores = dict(db.execute("SELECT horizon,COUNT(*) FROM domestic_scores GROUP BY horizon"))
        sealed = [(h, json.loads(p)) for h, s, at, p in rows if at]
        vectors = {store.digest(p["pair"]["with_news"]["probabilities"]) for _, p in sealed}
        updates = [p for h, p in sealed if h == "news_update"]
        out.update(
            synthetic_links_published=published,
            sealed_by_horizon={h: sum(1 for x, _ in sealed if x == h) for h in sorted({x for x, _ in sealed})},
            news_updates_sealed=len(updates),
            news_updates_whose_probabilities_differ_from_the_preview=sum(
                p["pair"]["with_news"]["probabilities"] != sealed[0][1]["pair"]["no_news"]["probabilities"]
                for p in updates
            ),
            news_updates_with_pair_changed=sum(bool(p["pair"]["changed"]) for p in updates),
            news_updates_with_player_scenario_changed=sum(bool(p["player_scenario"]["changed"]) for p in updates),
            distinct_probability_vectors_across_all_seals=len(vectors),
            pair_status=sorted({p["pair"]["status"] for _, p in sealed}),
            roster_state=sorted({p["news"]["state"] for _, p in sealed}),
            player_status_configured=sorted({p["news"]["player_status"]["configured"] for _, p in sealed}),
            payload_bytes_total=sum(len(p) for _, _, at, p in rows if at),
            payload_bytes_largest=max(len(p) for _, _, at, p in rows if at),
            score_rows_by_horizon=scores,
        )
        if index == 0:
            # 二、调度停了 3 天以后的状态。
            sim.now += timedelta(days=3)
            status = scheduler.status(root, leagues=["bun"])["leagues"]["bun"]
            out["status_three_days_after_the_last_cycle"] = {
                "state": status["state"],
                "last_cycle_finished": status["last_cycle"]["finished"],
                "clock_now": store.iso(sim.now),
                "fields": sorted(status),
            }
    return out


def swallowed_poll_failure():
    root = Path(args.scratch) / "poll-failure"
    root.mkdir(parents=True)
    sim = SimpleNamespace(now=START, sleeps=0)
    fixture = match(HOME, AWAY, KICKOFF, START - timedelta(days=3))
    past = match(AWAY, HOME, START - timedelta(days=40), START - timedelta(days=60))
    previous = replace(past, status="completed", result_versions=(
        football.ResultVersion(past.kickoff + timedelta(hours=3), (2, 1), "observed", "archive:past"),))

    def inputs(_root, code, history, market_root):
        binding = {"slug": "bun-bay-svw", "event_id": "11", "home_id": "8", "away_id": "9",
                   "kickoff": store.iso(KICKOFF), "receipt": store.iso(START - timedelta(days=3)), "sha256": "a" * 64}
        return SimpleNamespace(fixtures=(previous, fixture), upcoming=(fixture,),
                               provenance={"market_fixtures": {fixture.fixture_id: binding}})

    class Stop(BaseException):
        pass

    def sleep(_seconds):
        sim.sleeps += 1
        sim.now += timedelta(minutes=6)
        if sim.sleeps >= 3:
            raise Stop

    calls = {"roster_collect": 0}

    def broken(*a, **k):
        calls["roster_collect"] += 1
        raise TypeError("unexpected source structure")

    with ExitStack() as stack:
        stack.enter_context(patch.object(scheduler, "_clock", side_effect=lambda: sim.now))
        stack.enter_context(patch.object(scheduler, "_load", side_effect=inputs))
        stack.enter_context(patch.object(scheduler, "_pregame", side_effect=lambda *a: True))
        stack.enter_context(patch.object(store, "now_utc", side_effect=lambda: store.iso(sim.now)))
        stack.enter_context(patch.object(league_rosters, "collect_fixture_rosters", side_effect=broken))
        stack.enter_context(patch.object(scheduler, "time", SimpleNamespace(monotonic=lambda: 0.0, sleep=sleep)))
        raised = None
        try:
            scheduler.run(root, leagues=["bun"], market_state_dir=root / "market", repeat=True, interval=30)
        except Stop:
            pass
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}: {exc}"
        status = scheduler.status(root, leagues=["bun"])["leagues"]["bun"]
    return {
        "cycles_run": sim.sleeps,
        "roster_collect_calls_that_raised": calls["roster_collect"],
        "run_loop_raised": raised,
        "status_state": status["state"],
        "status_last_cycle_error": status["last_cycle"].get("error"),
        "status_news_polls": status["news_polls"],
    }


rates = [float(v) for v in args.links_per_day.split(",")]
print(json.dumps(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "fixture": f"{HOME} v {AWAY}",
        "simulated_span": f"{store.iso(START)} to {store.iso(KICKOFF + timedelta(hours=3))}, one tick per 15 minutes",
        "news_update_seals": [run(rate, i) for i, rate in enumerate(rates)],
        "poll_exception": swallowed_poll_failure(),
    },
    indent=1, ensure_ascii=False,
))
