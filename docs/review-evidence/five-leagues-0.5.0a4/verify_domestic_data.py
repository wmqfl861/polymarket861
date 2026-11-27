"""Offline, read-only release audit. Run from the candidate checkout with PYTHONPATH=src:tests."""
import argparse
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import types
import zlib
from contextlib import closing
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from polymarket861 import evidence, football, league_live_inputs as live, league_player_status as player
from polymarket861 import league_catalog as catalog, league_press as press, league_registry as reg, league_wiki_cache as wiki, store
from polymarket861 import market_provider
from test_domestic_player_evidence import DomesticPlayerEvidenceTest
from test_league_press import DomesticPressTests
from test_league_live_inputs import LiveFootballInputsTests, SEEN as MARKET_SEEN
from test_league_wiki_replay import VerifiedWikiReplayTests, at, revision


def with_fixture(case, func):
    t = case()
    t.setUp()
    try:
        return func(t)
    finally:
        t.doCleanups()


def api_registry(t):
    with patch.dict(os.environ, {"PM861_API_FOOTBALL_KEY": "test-marker"}):
        evidence.register_source(t.root, dict(t.grant, source_id=player.SOURCE, upstream="API-Football",
                fields={"injury": {"uses": ["prediction"], "methods": ["manual"]}}))
        try:
            t.record("injury", "Henrik Sommer", {"availability": "out", "certainty": "confirmed"}, source_id=player.SOURCE)
        except ValueError as e:
            return {"rejected_at_ingest": str(e)}
        status, scenario = t.run_scenario()
        return {"provider": status["api_football_state"], "changed": scenario["changed"],
                "applied_sources": [p["source_id"] for p in scenario["applied"]]}


def rights_at_receipt(t):
    t.clock = t.cutoff - timedelta(hours=2)
    evidence.register_source(t.root, dict(t.grant, status="disabled", free=False))
    t.clock = t.cutoff - timedelta(minutes=10)
    evidence.register_source(t.root, t.grant)
    t.clock = t.cutoff
    received = t.cutoff - timedelta(hours=1)
    try:
        t.record("injury", "Henrik Sommer", {"availability": "out", "certainty": "confirmed"}, published=received)
    except ValueError as exc:
        return {"rejected_at_ingest":str(exc)}
    with evidence.db(t.root) as db:
        stamp = evidence.utc(received)
        at_receipt = evidence.source_at(db,"first-hand",stamp,recorded_by=stamp)["status"]
    return {"source_at_reported_receipt":at_receipt, "source_at_import":"active",
           "changed":t.run_scenario()[1]["changed"]}


def in_current_squad(t):
    t.absent("Jonas Reuter")
    t.record("lineup", "official team sheet", {"confirmed": True, "starters": t.starters, "bench": ["Jonas Reuter"]})
    _, result = t.run_scenario()
    return {"changed": result["changed"], "applied": [p["name"] for p in result["applied"]],
            "excluded": [p["reason"] for p in result["excluded"]]}


def keeper_majority(t):
    for i, game in enumerate(t.prior[:3]):
        t.clock = game.kickoff - timedelta(minutes=60)
        starters = t.starters[:] if i < 2 else ["Casimir Lund", *t.starters[1:]]
        bench = ["Casimir Lund"] if i < 2 else ["Anton Keller"]
        t.record("lineup", "official", {"confirmed": True, "starters": starters, "bench": bench}, game=game)
    t.clock = t.cutoff
    t.absent("Anton Keller")
    _, scenario = t.run_scenario()
    item, = scenario["applied"] + scenario["excluded"]
    counts = item["player_importance"]
    return {"changed": scenario["changed"], "starts": counts["starts"], "observed_matches": counts["matches"],
            "scheduled": counts["scheduled_matches"], "reason": item.get("reason")}


def continued_episode(t):
    t.clock = t.kick - timedelta(days=7)
    original = store.iso(t.clock)
    t.absent("Henrik Sommer")
    t.clock = t.cutoff
    t.record("injury", "Henrik Sommer", {"availability": "out", "certainty": "reported"}, revision=2)
    _, scenario = t.run_scenario()
    row, = scenario["applied"]
    first = store.iso(row["onset_at"])
    return {"first": original, "computed": first, "same": first == original,
            "duration_multiplier": row["duration_multiplier"]}


def equal_receipt_adjudication(t):
    t.record("injury", "Henrik Sommer", {"availability": "out", "certainty": "reported"})
    t.directory()
    outputs = []
    prior = list(t.prior)
    g = prior[0]
    first, = g.result_versions
    void = replace(first, score=None, provenance="second ruling")
    for simultaneous in ((first, void), (void, first)):
        prior[0] = replace(g, result_versions=simultaneous)
        t.engine = football.FootballEngine([*prior, t.game], league="bun", allow_archive_schedule=False)
        outcome = t.run_scenario()[1]["applied"][0]
        outputs.append({"latest_score": simultaneous[-1].score, "basis": outcome["player_importance"]["basis"],
                        "multiplier": outcome["importance_multiplier"]})
    return outputs


def native_roles_and_cutoffs(t):
    answers = {}
    for league, position in (("bun", "Abwehr"), ("sea", "D"), ("lal", "DEF"), ("fl1", "Déf")):
        home, away = reg.members(league, "2026-27")[:2]
        fid = football.fixture_id("2026-27", home, away, league=league)
        fixture = dict(t.fixture, league=league, fixture_id=fid, home=home, away=away)
        base = dict(t.base, **fixture)
        cutoff = t.cutoff - timedelta(seconds=15)
        name = "Franco Muller"
        observed = store.iso(t.clock)
        available = store.iso(cutoff - timedelta(seconds=5))
        evidence.import_evidence(t.root, {"source_id":"first-hand", "method":"manual", "fixture_id":fid,
            "team":home, "field":"suspension", "subject":name, "locator":"local:match-report",
            "published_at":available, "received_at":available,
            "value":{"competition":league,"season":"2026-27", "kickoff":fixture['kickoff'],
                     "availability":"out","certainty":"confirmed"}})
        roster = {"players":[{"name":name,"source_position":position}],
                  "ready_at":available, "evidence":[{"source_sha256":"a"*64}]}
        outcome = []
        for time in (cutoff, observed, cutoff):
            status=player.as_of(t.root,fixture,time)
            snap=dict(fixture,teams={"home":{"team":home,"roster":roster}},player_status=status)
            scen=player.absence_scenario(base,snap,cutoff=time)
            outcome.append([p["role"] for p in scen["applied"]])
        answers[league] = outcome
    return answers


def press_titles(t):
    root = t.root / "team-press"
    root.mkdir()
    url = "https://press.example/829"
    with closing(sqlite3.connect(root / "index.sqlite3")) as db, db:
        db.executescript(press.SCHEMA)
        for ed, time, title in (("20261127120000", "2026-11-27T12:05:00Z", "Sevilla FC coach on the match"),
                                ("20261127121500", "2026-11-27T12:20:00Z", None)):
            story = {"url": url, "org": "Sevilla FC", "theme": "TAX_FNCACT_FOOTBALLER,82;"}
            if title:
                story["extras"] = "<PAGE_TITLE>" + title + "</PAGE_TITLE>"
            _, rows = press._parse(t.archive(ed, stories=[story]), ed, store.iso(time), press._clubs("2026-27"))
            db.execute("INSERT INTO press_releases VALUES (?,?,?,?,?,?)",
                       (ed, "checksum", store.iso(time), "2026-27", '["lal"]', 1))
            db.executemany("INSERT INTO press_clubs VALUES (?,?,?,?,?)",
                           [(ed, *key, store.canonical(doc)) for key, doc in rows.items()])
    results = []
    for cutoff in ("2026-11-27T12:01:00Z", "2026-11-27T12:10:00Z", "2026-11-27T12:25:00Z"):
        snap = press.fixture_news_as_of(t.root, dict(t.fixture, home="Sevilla FC"), cutoff)
        results.append({"cutoff": cutoff, "titles": [{k:x.get(k) for k in ("title", "record_id", "received_at",
                        "title_record_id", "title_received_at")} for x in snap["teams"]["home"]["items"]],
                        "digest": snap["evidence_sha256"]})
    when = store.iso("2026-11-27T12:25:00Z")
    def search(o):
        if isinstance(o,dict):
            if isinstance(o.get('latest_link'),dict):
                return o['latest_link']
            for v in o.values():
                if link:=search(v):
                    return link
        elif isinstance(o,list):
            for v in o:
                if link:=search(v):
                    return link
    with patch.object(store,'now_utc',return_value=when):
        latest = search(catalog.local_source_health(t.root,"2026-27",["lal"]))
    results.append({"health_latest_link": {k:latest.get(k) for k in ("title","title_received_at","title_record_id")}})
    return results


def wiki_after_move():
    league,season = "bun","2026-27"
    home,away = reg.members(league,season)[:2]
    fid = football.fixture_id(season,home,away,league=league)
    match = football.Fixture(fid,season,1,football.utc(at("08:00")),home,away)
    schedule = ((football.utc(at("10:20")),football.utc(at("14:00"))),)
    wiki_rows = [(at(t),"wikipedia",label,at(t),{fid:(1,0)}) for t,label in (("10:00","wiki-before"),("16:00","wiki-after"))]
    of = [(at("16:05"),"openfootball","alternate",at("16:05"),replace(match,home_goals=2,away_goals=1,status="completed"))]
    direct=[(s,source,loc,pub,row[fid]) for s,source,loc,pub,row in wiki_rows]
    cached=wiki.score_observations(league,season,live._wiki_scores,wiki_rows,{fid:match},{fid:schedule},at("07:00"))[fid]
    values={}
    for label, part in (("full",direct),("cached",cached)):
        versions,conflict=live._score_versions(match,sorted(list(part)+of,key=lambda r:r[0]),schedule)
        values[label]={"status":versions[-1].score,"conflict":conflict,"kept":len(part)}
    return values


def legacy_press(t):
    # Produce an old database through the original public collector, then reopen the exact file with both versions.
    legacy_source = subprocess.check_output(["git","show","ac64dabe:src/polymarket861/league_press.py"])
    old = types.ModuleType("polymarket861.press_v1_for_audit")
    old.__package__ = "polymarket861"
    exec(compile(legacy_source,"ac64dabe-league-press","exec"),old.__dict__)
    real = "https://publisher.example/football/lille-osc-vs-le-mans-team-news"
    motor = "https://publisher.example/motorsport/24-hours-of-le-mans-qualifying"
    archive = t.archive(t.edition, stories=[{"url":real,"extras":"<PAGE_TITLE>Original football title</PAGE_TITLE>"},
                                            {"url":motor}])
    md5 = hashlib.md5(archive,usedforsecurity=False).hexdigest()
    def get(url,*,timeout):
        data=(f"{len(archive)} {md5} {old.BASE}{t.edition}.gkg.csv.zip".encode() if url.endswith("lastupdate.txt") else archive)
        return market_provider.Reply(data,t.received,t.received,{},url)
    with patch.object(store,"now_utc",return_value=t.received):
        old.collect(t.root,quarters=1,get=get)
    f=dict(t.fixture,league="fl1",home="Le Mans FC",away="Lille OSC")
    path=t.root/"team-press"/"index.sqlite3"
    raw=path.read_bytes()
    def urls(mod,at):
        return mod.fixture_news_as_of(t.root,f,at)["teams"]["home"]["items"]
    before=urls(old,t.received)
    current=urls(press,t.received)
    return {"old":sorted(i['url'] for i in before), "before_receipt":len(urls(press,"2026-11-27T12:01:00Z")),
            "current":[{'url':i['url'],'title':i.get('title'),'rechecked':i['legacy_rechecked']} for i in current],
            "unchanged_database_bytes":raw==path.read_bytes()}


def moved_upcoming(t):
    league, season = "bun", "2026-27"
    root=t.state/"sport"
    path=root/season/reg.profile(league).source_file
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"matches":[{"round":"Matchday 1","date":"2026-11-20","time":"08:00",
                     "team1":t.match.home,"team2":t.match.away}]}))
    def schedules(db,store_root,league,season,latest,cutoff,pin_seen,*,replay):
        timeline=[(football.utc(at('07:00')),football.utc(at('08:00')))]
        if cutoff>=at('10:20'):
            timeline.append((football.utc(at('10:20')),football.utc(at('14:00'))))
        return {t.fid:timeline},{t.fid:{"kickoff":store.iso(timeline[-1][1]),"fixture_id":t.fid}},{},{"state":"available"}
    kwargs={"league":league,"football_root":root,"market_state_dir":t.state/"market"}
    result=[]
    with (patch.object(reg,"source_blob",return_value=reg.git_blob(path.read_bytes())),
          patch.object(live.store,"now_utc",return_value=at('07:00')),
          patch.object(live.league_openfootball_refresh,"refresh",return_value={"state":"ok"}),
          patch.object(live,"_poll_wiki"),patch.object(live,"_market_schedules",schedules)):
        live.load_inputs(t.state,**kwargs)
        t.insert(7,at('10:00'),score='1–0')
        t.insert(8,at('10:45'),score='')
        for cutoff in ('10:10','10:21','10:46'):
            inputs=live.load_inputs(t.state,as_of=at(cutoff),**kwargs)
            game,=inputs.fixtures
            result.append({"cutoff":cutoff, "game_status":game.status, "kickoff":store.iso(game.kickoff),
                           "upcoming":len(inputs.upcoming), "withheld":inputs.provenance.get("withheld_fixtures"),
                           "versions":[[store.iso(v.actual_kickoff),v.score] for v in game.result_versions]})
    return result


def confirmed_gamma_move():
    outputs=[]
    for receipt in ("2026-11-27T19:50:00.000000Z", "2026-11-27T20:10:00.000000Z"):
        case=LiveFootballInputsTests()
        case.setUp()
        try:
            case.record_event(case.event(),MARKET_SEEN)
            original,=case.inputs().upcoming
            case.record_event(case.event()|{"startTime":"2026-11-29T20:00:00.000000Z"},receipt)
            inputs=case.inputs(receipt)
            binding=inputs.provenance["market_fixtures"].get(original.fixture_id)
            outputs.append({"original_kickoff":store.iso(original.kickoff),"replacement_received":receipt,
                            "upcoming":len(inputs.upcoming),"seal_check":live.still_pregame(case.market,binding,receipt) if binding else None,
                            "withheld":inputs.provenance["withheld_fixtures"]})
        finally:
            case.doCleanups()
    return outputs


def wiki_replay(seed=861):
    rng = random.Random(seed)
    anchor = store.instant("2026-11-20T08:00:00Z")
    stamp = lambda n: store.iso(anchor + timedelta(minutes=n))
    league, season = "bun", "2026-27"
    home, away = reg.members(league, season)[:2]
    fid = football.fixture_id(season, home, away, league=league)
    f = football.Fixture(fid, season, 1, store.instant(stamp(0)), home, away)
    src = "openfootball"
    count = 0
    for case_no in range(180):
        pin = stamp(rng.randrange(0, 280))
        schedules = tuple((store.instant(stamp(t)), store.instant(stamp(rng.randrange(0, 200))))
                          for t in sorted(rng.sample(range(0, 225), 3)))
        history = []
        originals = []
        for i in range(20):
            seen = i * 15
            value = rng.choice([(1, 0), (2, 1), None])
            published = stamp(seen - rng.randrange(0, 70))
            history.append((stamp(seen), "wikipedia", f"wiki/{case_no}/{i}/{value}", published, {fid: value}))
            if rng.random() < .6:
                received = stamp(seen + rng.randrange(0, 20))
                value = rng.choice([(1, 0), (2, 1), None])
                fx = (replace(f, status="completed", home_goals=value[0], away_goals=value[1])
                      if value else replace(f, status="quarantined", quarantine_reason="canceled"))
                originals.append((received, src, f"of/{case_no}/{i}", published, fx))
        for cutoff in (7, 17, 3, 17, 20, 8):
            rows = history[:cutoff]
            visible = [r for r in originals if r[0] <= rows[-1][0]]
            full = [(max(pin, s), src, loc, pub, m[fid]) for s, src, loc, pub, m in rows]
            delta = wiki.score_observations(league, season, live._wiki_scores, rows, {fid: f}, {fid:schedules}, pin)
            direct = live._score_versions(f, sorted(visible + full, key=lambda r:r[0]), schedules)
            actual = live._score_versions(f, sorted(visible + list(delta.get(fid, ())), key=lambda r:r[0]), schedules)
            assert direct == actual, (case_no, cutoff, actual, direct)
            count += 1
    return {"seed": seed, "generation_schedules": 180, "exact_full_replay_comparisons": count}


def wiki_bytes(t):
    t.insert(7, at("11:00"), score="1–0")
    t.insert(11, at("14:00"), score="2–0")
    def read(cutoff):
        with closing(t.connect()) as conn:
            rows, _ = t.history(cutoff, db=conn)
            return [x[-1][t.fid] for x in rows]
    initial = read("16:00")
    # Byte mismatches with a still-valid, otherwise coherent revision must not select a previously cached score.
    altered = revision(11, at("14:00"), score="9–1")
    with t.db:
        t.db.execute("UPDATE wiki_receipts SET body=? WHERE revision=11", (zlib.compress(altered),))
    early = read("13:59")
    try:
        read("14:00")
        mismatch = False
    except ValueError as exc:
        mismatch = "hash" in str(exc).lower()
    return {"warm": initial, "before_changed_original": early, "visible_mismatched_blob_rejected": mismatch}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--extra-replays", action="store_true",
                        help="reproduce the ten additional deterministic cache replay runs")
    if parser.parse_args().extra_replays:
        seeds = [17, 51, 97, 433, 928, 1597, 2468, 2861, 7949, 11329]
        compared = sum(wiki_replay(seed)["exact_full_replay_comparisons"] for seed in seeds)
        ref = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        print(json.dumps({"seeds": seeds, "additional_full_replay_comparisons": compared,
                          "git_commit": ref}, indent=2))
        return
    output = {func.__name__: with_fixture(DomesticPlayerEvidenceTest, func)
              for func in (api_registry, rights_at_receipt, in_current_squad, keeper_majority, continued_episode,
                           equal_receipt_adjudication, native_roles_and_cutoffs)}
    output["press_titles"] = with_fixture(DomesticPressTests, press_titles)
    output["press_legacy"] = with_fixture(DomesticPressTests, legacy_press)
    output["wiki_after_move"] = wiki_after_move()
    output["wiki_replay"] = wiki_replay()
    output["wiki_bytes"] = with_fixture(VerifiedWikiReplayTests, wiki_bytes)
    output["moved_upcoming"] = with_fixture(VerifiedWikiReplayTests, moved_upcoming)
    output["gamma_projection"] = confirmed_gamma_move()
    output["commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    output["source_sha256"] = {p:hashlib.sha256(Path("src/polymarket861",p).read_bytes()).hexdigest()
           for p in ("league_player_evidence.py","league_player_status.py","league_press.py","league_press_labels.py",
                     "league_wiki_cache.py","league_live_inputs.py", "league_catalog.py", "football.py", "evidence.py",
                     "evidence_sources.py", "league_rosters.py", "news_importance.py", "news_model.py")}
    print(json.dumps(output, indent=2, ensure_ascii=False))

if __name__ == "__main__":
    main()
