"""Synthetic 14-day HTTP-to-readiness run with a full synthetic prior NBA season."""
import argparse
import collections
import json
import os
import tempfile
from datetime import date, timedelta
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import patch

from data_audit import Clock,TEST_KEY,data,markets,market_event,row,scan
from polymarket861.nba import operations,operations_report,readiness,warmup

FIRST = date(2026,10,20)
CLUBS='ATL BOS BKN CHA CHI CLE DAL DEN DET GSW HOU IND LAC LAL MEM MIA MIL MIN NOP NYK OKC ORL PHI PHX POR SAC SAS TOR UTA WAS'.split()


def prior_finals():
    tip=date(2025,10,21)
    clubs=list(range(30));games=[]
    for n in range(82):
        d=tip+timedelta(days=2*n)
        for j in range(15):
            h,a=clubs[j],clubs[-1-j]
            hm,aw=100+(n*7+j*3)%32,99+(n*11+j*5)%26
            if aw==hm:aw-=1
            games.append(row(d,season=2025,id=100000+n*15+j,datetime=None,period=4,status='Final',status_state='final',
                             home_team_score=hm,visitor_team_score=aw,
                             home_team={'id':h+1,'abbreviation':CLUBS[h],'full_name':CLUBS[h]},
                             visitor_team={'id':a+1,'abbreviation':CLUBS[a],'full_name':CLUBS[a]}))
        clubs=[clubs[0],clubs[-1],*clubs[1:-1]]
    return games


def market(d):
    ev=market_event(d);dt=data._utc(ev['startTime']).astimezone(data.EASTERN)
    day_text=dt.strftime('%A, %B %-d, %Y');time_text=dt.strftime('%-I:%M %p ET');last=(dt+timedelta(days=2)).strftime('%A, %B %-d, %Y')
    rules=(f'In the upcoming NBA game between Heat and Knicks, originally scheduled for {day_text} at {time_text} (as of creation of the first market related to the event): '
        'If the Heat win, the market will resolve to "Heat". If the Knicks win, the market will resolve to "Knicks". '
        "If this market's outcome was definitively determined under its rules by the listed resolution sources before abandonment, that outcome will stand. "
        f'Otherwise, the following no-result provisions apply. If the game is canceled, does not start by {last}, 11:59 PM ET, or is abandoned with no official result by that time, this market will resolve 50-50. '
        'The result will be determined based on the final score including any overtime periods.')
    rid=d.toordinal()
    ev['markets']=[{'id':str(rid+50),'conditionId':f'0x{rid:064x}','clobTokenIds':json.dumps([str(rid*2),str(rid*2+1)]),
                    'outcomes':'["Heat","Knicks"]','description':rules,'line':None,'slug':ev['slug'],'gameStartTime':ev['startTime'],
                    'marketMetadata':None,'active':True,'enableOrderBook':True,'closed':False,'acceptingOrders':True,'sportsMarketType':'moneyline'}]
    return ev


def run(root,scenario):
    os.environ['BALLDONTLIE_API_KEY']=TEST_KEY
    clock=Clock();clock.set(FIRST-timedelta(days=1),12)
    previous=prior_finals()
    pre=scan(root,clock,date(2025,10,21),{'regular':previous},date(2026,6,30));assert pre['complete'],pre
    days=[FIRST+timedelta(days=i) for i in range(14 if scenario=='positive' else 15)]
    events={d:market(d) for d in days if scenario not in ('off','gap') or (d-FIRST).days!=5}
    def transport(url,params,*,timeout):
        now=data._iso(clock());params=params or {}
        if url.endswith('/events/keyset'):
            payload={'events':[e for e in events.values() if params['start_time_min']<=data._iso(e['startTime'])<=params['start_time_max']]}
        elif '/events/' in url:
            d=date.fromordinal(int(url.rsplit('/',1)[-1]));payload=events[d]
        elif url.endswith('/books'):
            payload=[{'asset_id':tok,'market':f'0x{int(tok)//2:064x}',
                'bids':[{'price': '0.57' if int(tok)%2 else '0.41','size':'50'}],
                'asks':[{'price': '0.59' if int(tok)%2 else '0.43','size':'50'}],
                'timestamp':int(clock().timestamp()*1000)} for tok in params['token_ids']]
        else:raise ValueError(url)
        return markets.api.Reply(json.dumps(payload).encode(),now,now,{'date':format_datetime(clock(),usegmt=True),'age':'0'},url)
    logs=[]
    with patch.object(markets.api,'get',transport):
        for i,d in enumerate(days):
            clock.set(d,3,25)
            markets.collect(root,now=clock)
            clock.set(d,15)
            live=events.get(d)
            if i:
                yesterday=d-timedelta(days=1)
                if events.get(yesterday):
                    f=row(yesterday,home_team_score=112+i,visitor_team_score=98+i%11,period=4,status='Final',status_state='final')
                    f['home_team']['id']=20;f['visitor_team']['id']=16
                    assert scan(root,clock,yesterday,{'regular':[f]})['complete']
                elif scenario=='off':
                    assert scan(root,clock,yesterday,{'regular':[]})['complete']
            if not live:
                if scenario=='off':
                    assert scan(root,clock,d,{})['complete']
                    markets.collect(root,now=clock)
                operations.forecast_cycle(root,clock=clock)
                continue
            g=row(d);g['home_team']['id']=20;g['visitor_team']['id']=16
            assert scan(root,clock,d,{'regular':[g]})['complete']
            clock.set(d,21,51)
            assert scan(root,clock,d,{'regular':[g]})['complete']
            clock.set(d,21,57)
            m=markets.collect(root,now=clock)
            clock.advance(10);op=operations.forecast_cycle(root,clock=clock)
            clock.set(d,22,15);operations.forecast_cycle(root,clock=clock)
            logs.append((str(d),m['state'],op['sealed'],op['fixture_errors']))
        d=days[-1];clock.set(d+timedelta(days=1),15)
        f=row(d,home_team_score=115,visitor_team_score=104,period=4,status='Final',status_state='final')
        f['home_team']['id']=20;f['visitor_team']['id']=16
        assert scan(root,clock,d,{'regular':[f]})['complete']
        operations.forecast_cycle(root,clock=clock)
    report=readiness.readiness(root,d,now=clock());cov=report['calendar']
    grades=[];training=[];whys=[]
    import sqlite3
    from polymarket861.nba import storage
    with storage.open_existing(root) as db:
        for r in db.execute("select payload from nba_windows where horizon='T-60m' and state='sealed' order by game_day"):
            s=json.loads(r[0]);w=s['warmup'];training.append(s['forecast']['training_games']);whys.append(w['reason'])
        for r in db.execute("select distinct game_id from nba_grade_versions where horizon='T-60m' and json_extract(payload,'$.state')='scored'"):
            grades.append(r[0])
    return {'prior_real_http_calls_synthetic_transport':pre['pages'],'prior_finals':len(previous),'scenario':scenario,
            'calendar_qualified':cov['qualified'],'reasons':cov['reasons'],'n_game_days':cov['observed_game_days'],
            'calendar_gaps':cov['calendar_gaps'],'totals':cov['totals'],'rates':[cov['warm_seal_rate'],cov['eligible_win_pairing_rate']],
            'first_warm':cov['first_warm_game_day'],'three_paired_schedule_days':report['kickoff_differences']['paired_days'],
            'training_games':training,'warmup_reasons':dict(collections.Counter(whys)), 'scored':len(grades),
            'gates':report['gates'],'phase_two_activated':report['phase_two_activated'],'logs':logs}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('scenario',choices=('positive','gap','off'));a=p.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        print(json.dumps(run(Path(temp)/'nba',a.scenario),indent=2))
