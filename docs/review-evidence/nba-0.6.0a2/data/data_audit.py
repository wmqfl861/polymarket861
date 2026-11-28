"""Read-only product audit with synthetic HTTP at the network boundary and fresh SQLite per case."""
import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

import httpx
from polymarket861.nba import data, markets

UTC = timezone.utc
FIRST = date(2026, 11, 2)
TEST_KEY = 'independent-audit-synthetic-no-account-861'


class Clock:
    def __init__(self):
        self.set(FIRST, 9)

    def set(self, day, hour, minute=0):
        self.value = datetime.combine(day, datetime.min.time(), UTC).replace(hour=hour, minute=minute)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def row(day, **new):
    base = dict(id=day.toordinal(), date=str(day), datetime=f'{day}T23:00:00Z', season=2026,
                status='6:00 pm ET', status_state='scheduled', period=0, postseason=False,
                home_team_score=0, visitor_team_score=0,
                home_team={'id': 2, 'abbreviation': 'NYK', 'full_name': 'New York Knicks'},
                visitor_team={'id': 11, 'abbreviation': 'MIA', 'full_name': 'Miami Heat'})
    return dict(base, **new)


def scan(root, clock, day, rows, end=None):
    def get(params, token):
        assert token == TEST_KEY
        arr = rows.get(params['season_type'], [])
        idx = params.get('cursor') or 0
        view = arr[idx:idx+100]
        nxt = idx + 100 if idx + 100 < len(arr) else None
        return httpx.Response(200, json={'data': view, 'meta': {'next_cursor': nxt}})
    return data.sync_bdl(root, str(day), str(end or day), fetch=get, clock=clock, sleep=clock.advance)


def market_event(day):
    return {'id': str(day.toordinal()), 'gameId': str(day.toordinal()+1000), 'active': True, 'closed': False,
            'archived': False, 'period': 'NS', 'startTime': f'{day}T23:00:00Z', 'slug': f'nba-mia-nyk-{day}',
            'teams': [{'ordering': a, 'name': b, 'league': 'nba', 'abbreviation': c, 'id': d}
                      for a,b,c,d in [('away','Heat','mia',4),('home','Knicks','nyk',2)]],
            'series':[{'id':'10345'}], 'tags':[{'id':'100639'}], 'markets': []}


def catalogue(root, clock, days, *, hour=23):
    def transport(url, params, *, timeout):
        assert url.endswith('/events/keyset'), url
        events = [dict(market_event(d),startTime=f'{d}T{hour:02}:00:00Z') for d in days if params['start_time_min'] <= data._iso(f'{d}T{hour:02}:00:00Z') <= params['start_time_max']]
        body = json.dumps({'events': events}).encode()
        stamp = data._iso(clock())
        return markets.api.Reply(body, stamp, stamp, {'date': format_datetime(clock(), usegmt=True), 'age':'0'}, url)
    with patch.object(markets.api, 'get', transport):
        result = markets.collect(root, now=clock)
    assert result['state'] == 'ok', result
    return result


def player_case(directory, scenario):
    from polymarket861.nba import source_probes
    clock, root = Clock(), directory / scenario
    for source in ('bigballs', 'dunkapi'):
        evidence = directory / 'terms-for-fixture-only.txt'
        evidence.write_text('Synthetic named independent audit. For this fixture only tests the declared envelope snapshot_date field.')
        source_probes.record_terms(root, source, 'Synthetic reviewer', evidence, 'Synthetic NBA upstream', evidence,
                                   'allowed','allowed','snapshot_date',evidence,clock=clock)
    selected = list(range(8)) if scenario in ('gap', 'unobserved_gap', 'observed_off_day', 'partial_off_day') else list(range(7))
    for i in selected:
        day = FIRST + timedelta(days=i)
        clock.set(day, 12)
        if scenario in ('gap','unobserved_gap','observed_off_day','partial_off_day') and i == 3:
            if scenario == 'gap':
                catalogue(root, clock, [day])
            if scenario == 'observed_off_day':
                clock.set(day,5,30)
                scan(root, clock, day, {'regular': []})
                catalogue(root, clock, [])
            if scenario=='partial_off_day':
                scan(root, clock, day, {'regular': []})
                clock.set(day + timedelta(days=1),3,45)
                catalogue(root, clock, [day])  # 18:00 ET game fell outside the late 1-hour lookback.
            continue
        if scenario=='native_earlier':
            clock.set(day,10);catalogue(root,clock,[day],hour=19);clock.set(day,12)
        sched = scan(root, clock, day, {'regular':[row(day)]})
        assert sched['complete'], sched
        for minutes in (90,60,15):
            clock.set(day, 23)
            clock.advance(-60*(minutes+2))
            def fetch(source, key):
                assert key == TEST_KEY
                field = 'date' if scenario == 'wrong_field' else 'snapshot_date'
                league = 'nfl' if scenario == 'wrong_league' else 'nba'
                entry = {'player_id':42, 'status':'Out', 'league':league}
                payload = {field:str(day), 'league':league,'injuries':[entry]}
                return httpx.Response(200,json={'data':payload} if source=='bigballs' else payload)
            source_probes.probe_due(root,clock=clock,fetch=fetch)
        clock.set(day, 23,30)
    at7 = source_probes.player_summary(root,clock())
    as_not_yet = source_probes.player_summary(root, clock()-timedelta(days=4))
    return {s: {'verified':x['source_verified'], 'days':x['game_days'],'recent_days':[d['game_day'] for d in x['days'][-9:]],
                't60_fresh_days':x['t60_fresh_days'], 'seven_day_coverage': x['seven_day_coverage'],
                'verified_historical_cutoff':as_not_yet[s]['source_verified']} for s,x in at7.items()}


def mixed(root):
    from polymarket861.nba import readiness, source_probes
    clock=Clock()
    dat=FIRST
    r=scan(root,clock,dat,{'regular':[row(dat),row(dat,id=70000,home_team={'id':1})]})
    clock.set(dat,21,58)
    def fetch(source,key):
        return httpx.Response(200,json=({'data':{'snapshot_date':str(dat),'injuries':[{'player_id':1,'status':'Out'}]}} if source=='bigballs' else {'snapshot_date':str(dat),'injuries':[{'player_id':1,'status':'Out'}]}))
    p=source_probes.probe_due(root,clock=clock,fetch=fetch)
    end=clock()+timedelta(days=1)
    cov=readiness.readiness(root,dat,now=end)
    return {'state': r['state'],'complete':r['complete'],'quarantined':r['quality']['quarantined_rows'],
            'probes':{s:item['state'] for s,item in p['sources'].items()},
            'schedule_day_success':cov['calendar']['days'][-1]['schedule_scan_succeeded_on_date']}


def match_case(root, scenario):
    from polymarket861.nba import markets_schedule
    clock = Clock()
    days=[FIRST+timedelta(days=i) for i in range(3)]
    for day in days:
        clock.set(day, 4, 30)
        catalogue(root,clock,days)
        scan(root,clock,day,{'regular':[row(day)]})
        clock.set(day,20)
        if scenario=='postponed':
            scan(root,clock,day,{'regular':[row(day,postponed=True)]})
        if scenario=='withdrawn':
            scan(root,clock,day,{})
        if scenario=='unknown':
            scan(root,clock,day,{'regular':[row(day,status='different',status_state='different')]})
        if scenario=='moved':
            scan(root,clock,day,{'regular':[row(day,datetime=f'{day}T21:30:00Z')]})
    clock.set(days[-1]+timedelta(days=1),13)
    diff=markets_schedule.kickoff_differences(root,as_of=clock())
    return {'enough_days':diff['enough_days'],'paired_days':diff['paired_days'],'counts':diff['counts'],
            'day_reports':[(d['date_et'],d['complete_day'],d['fixtures'][0]['status'],d['fixtures'][0].get('reason')) for d in diff['days']]}


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--run',choices=['players','schedule','keys','mixed'],default='players');args=parser.parse_args()
    for k in ('BIGBALLS_API_KEY','BALLDONTLIE_API_KEY','DUNKAPI_API_KEY'):
        os.environ[k]=TEST_KEY
    with tempfile.TemporaryDirectory(prefix='pm861-independent-data-') as tmp:
        root=Path(tmp)
        if args.run=='players':
            result={s:player_case(root,s) for s in ('normal','gap','unobserved_gap','observed_off_day','wrong_field','wrong_league','partial_off_day','native_earlier')}
        elif args.run=='schedule':
            result={s:match_case(root/s,s) for s in ('normal','postponed','withdrawn','unknown','moved')}
        elif args.run=='mixed':
            result=mixed(root/'mixed')
        print(json.dumps(result,indent=2,ensure_ascii=False))
