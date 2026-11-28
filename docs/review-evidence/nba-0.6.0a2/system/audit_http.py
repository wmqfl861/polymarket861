"""Independent CLI audit. Only external HTTP transport and the wall clock are simulated.

Use AUDIT_SOURCE=/path/to/repo and PYTHONPATH=/path/to/repo/src; see final.md.
"""
from __future__ import annotations
import argparse
import contextlib
from copy import deepcopy
import datetime as times
from datetime import UTC, timedelta
from email.utils import format_datetime
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import zipfile

import httpx

ROOT=Path(os.environ['AUDIT_SOURCE']).resolve()
REAL=times.datetime
EAST=__import__('zoneinfo').ZoneInfo('America/New_York')
clock=REAL(2026,11,28,0,0,tzinfo=UTC)

class ClockMeta(type):
    def __instancecheck__(self,x):
        return isinstance(x,REAL)
class AuditTime(REAL, metaclass=ClockMeta):
    @classmethod
    def now(cls,tz=None):
        at=cls.fromtimestamp(clock.timestamp(),UTC)
        return at.astimezone(tz) if tz is not None else at.astimezone().replace(tzinfo=None)
def sec(n):
    global clock
    clock+=timedelta(seconds=n)
def settime(at):
    global clock
    clock=at

times.datetime=AuditTime
time.sleep=lambda n:sec(n)
os.environ['BALLDONTLIE_API_KEY']='audit-offline-synthetic-schedule'
from polymarket861.cli import main as cli

class Scene:
    def __init__(self, folder):
        self.folder=folder
        with zipfile.ZipFile(ROOT/'tests/fixtures/nba/market-lines-20261128.zip') as z:
            self.event=json.loads(z.read('1377580/gamma.json'))
            self.books=json.loads(z.read('1377580/clob.json'))
        self.tip=REAL.fromisoformat(self.event['startTime'])
        self.report=json.loads((ROOT/'reports/nba-phase1/market-lines-20261128.json').read_text())['events'][0]
        chosen={v['condition_id'] for v in self.report['families'].values()}
        self.event['markets']=[m for m in self.event['markets'] if m['conditionId'] in chosen]
        self.books=[b for b in self.books if b['market'] in chosen]
        self.day=self.tip.astimezone(EAST).date()
        self.source_tip=self.tip
        self.extra_bdl=[]; self.extra_events=[]; self.delays={}; self.price_by_token={}
        self.trace=[]; self.status='Scheduled'; self.period=0; self.home_points=0; self.away_points=0
        self.null_tip=False; self.bdl_overrides={}; self.transform_book=None; self.reverse=True
        self.http429=False; self.blank=False
    def at(self,min_to_tip,seconds=0):
        settime(self.tip-timedelta(minutes=min_to_tip)+timedelta(seconds=seconds))
    def team(self,code):
        return {"id":{'MIN':1,'MEM':2,'BOS':3,'LAL':4}[code], 'abbreviation':code,'full_name':{'MIN':'Minnesota Timberwolves','MEM':'Memphis Grizzlies','BOS':'Boston Celtics','LAL':'Los Angeles Lakers'}[code]}
    def row(self):
        return {'id':710001,'date':str(self.day),'datetime':None if self.null_tip else self.source_tip.isoformat(), 'season':2026,'status':self.status,'period':self.period,'season_type':'regular','postseason':False,'home_team_score':self.home_points,'visitor_team_score':self.away_points,'home_team':self.team('MIN'),'visitor_team':self.team('MEM'),**self.bdl_overrides}
    def history(self):
        for i in range(41):
            offset=2+i//4
            t=self.tip-timedelta(days=offset)
            pair=[('MIN','BOS'),('LAL','MEM'),('MEM','MIN'),('BOS','LAL')][i%4]
            yield dict(self.row(),id=730000+i,date=str(t.astimezone(EAST).date()),datetime=t.isoformat(),period=4,status='Final',home_team_score=109+(i%10),visitor_team_score=95+(i%11),home_team=self.team(pair[0]),visitor_team=self.team(pair[1]))
    def respond(self,req):
        path=req.url.path; full=str(req.url); q=dict(req.url.params)
        self.trace.append({'at':clock.isoformat(),'method':req.method,'path':path, 'type':q.get('season_type')})
        if req.url.host=='api.balldontlie.io':
            assert req.method=='GET'
            rows=[] if q['season_type']!='regular' else [*self.history(),self.row(),*self.extra_bdl]
            rows=[r for r in rows if q['start_date']<=str(r['date'])<=q['end_date']]
            body={'data':rows,'meta':{'next_cursor':None}}
        elif path=='/events/keyset':
            assert req.method=='GET'
            events=[] if self.blank else [self.event,*self.extra_events]; start=int(q.get('after_cursor',0)); end=start+int(q['limit']);body={'events':events[start:end],'next_cursor':str(end) if len(events)>end else None}
        elif path.startswith('/events/'):
            assert req.method=='GET'
            body=next(e for e in [self.event,*self.extra_events] if str(e['id'])==path.split('/')[-1])
        elif path=='/books':
            assert req.method=='POST'; tokens=[r['token_id'] for r in json.loads(req.content)];self.trace[-1]['tokens']=len(tokens)
            body=[deepcopy(b) for b in self.books if b['asset_id'] in tokens]
            if self.reverse: body.reverse()
            for b in body: b['timestamp']=str(int(clock.timestamp()*1000))
            if self.transform_book: body=self.transform_book(body)
        elif path=='/v2/tokens':
            assert req.method=='GET'; tok=q['token_id']; cid,price=self.price_by_token[tok]
            body={'data':[{'token_id':tok,'condition_id':cid,'resolved':True,'final_price':price}]}
        else: raise AssertionError(f'unexpected URL {full}')
        sec(self.delays.get(path,0.05))
        headers={'Date':format_datetime(clock,usegmt=True),'Age':'0'}
        if self.http429 and path=='/books': return httpx.Response(429,headers={**headers,'Retry-After':'125'},json={})
        return httpx.Response(200,headers=headers,json=body)
    def cmd(self,*args):
        out=io.StringIO(); err=io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err): code=cli(['--data-dir',str(self.folder),'nba',*args])
        assert out.getvalue(),(args,code,err.getvalue())
        body=json.loads(out.getvalue()); return body
    def sync(self):return self.cmd('data','sync','--start',str(self.day-timedelta(days=15)),'--end',str(self.day))
    def market(self):return self.cmd('markets','collect')
    def forecast(self): return self.cmd('run','--component','forecast')['components']['forecast']
    def show(self):
        ws=self.cmd('forecast-show','bdl:710001')['windows']
        return [w for w in ws if w['horizon']=='T-60m']
    def counts(self):
        return self.cmd('markets','status')['payout_reconciliation']
    def raw(self,sql):
        with sqlite3.connect(f'file:{self.folder}/nba.sqlite3?mode=ro',uri=True) as db:
            return db.execute(sql).fetchall()
    def final(self,h,a,period=5):
        self.status='Final'; self.period=period; self.home_points=h;self.away_points=a
    def terminal(self,h,a,void_family=None,period_label='F/OT'):
        for m in self.event['markets']:
            family=next(k for k,v in self.report['families'].items() if v['condition_id']==m['conditionId'])
            labels=json.loads(m['outcomes']); toks=json.loads(m['clobTokenIds'])
            if family=='win': primary='Timberwolves';value=int(h>a)
            else:
                primary='Over'; value=int({'total':h+a,'home_total':h,'away_total':a}[family] > m['line'])
            prices=[.5 if family==void_family else (value if label==primary else 1-value) for label in labels]
            m.update(closed=True,umaResolutionStatus='resolved',outcomePrices=json.dumps(prices));self.price_by_token.update({tok:(m['conditionId'],v) for tok,v in zip(toks,prices,strict=True)})
        self.event.update(ended=True,period=period_label)
    def wrap(self):
        sim=httpx.MockTransport(self.respond)
        httpx.Client._transport_for_url=lambda _,url:sim
        httpx.AsyncClient._transport_for_url=lambda _,url:sim
    def early(self):
        self.at(85); s=self.sync(); assert s['complete'],s
        self.at(82);m=self.market();assert m['state']=='ok',m
    def pair(self):
        self.at(64); m=self.market();assert m['state']=='ok',m
        self.at(63);self.forecast();w=self.show()[-1];return w

def run_one(name,folder):
    p=Scene(folder);p.wrap();r={}
    if name=='late':
        p.early(); p.at(60,seconds=-2); p.delays['/books']=3; m=p.market(); p.at(59); p.forecast()
        r={'errors':m['errors'],'choices':p.raw('SELECT count(*) FROM nba_market_choices')[0][0],'window':p.show()[-1]['state']}
    elif name.startswith('compare_'):
        delta=int(name.split('_')[1]); early_retain='resample' in name
        p.source_tip=p.tip+timedelta(minutes=delta);p.early()
        if early_retain:
            p.at(65,3);p.market()
        p.at(63 if delta>=0 else 64,10);p.market()
        cutoff=p.source_tip-timedelta(minutes=60)
        if delta>14:
            settime(cutoff-timedelta(minutes=12));p.sync()
        settime(cutoff-timedelta(seconds=50));f=p.forecast();w=p.show()[-1];pred=w.get('prediction',{});ref=pred.get('market_at_prediction',{}).get('win',{});ncut=p.tip-timedelta(minutes=60)
        r={'state':w['state'],'note':w['note'],'errors':f['fixture_errors'],'schedule':pred.get('market_schedule',{}).get('status'),'reference_families':sorted(pred.get('market_at_prediction',{})),'win_quote_at':ref.get('available_at'),'within_both_five_minute_windows':None if not ref else max(ncut,cutoff)-timedelta(minutes=5)<=REAL.fromisoformat(ref['observed_at'])<=REAL.fromisoformat(ref['available_at'])<min(ncut,cutoff),'clob_batches':sum(e['path']=='/books' for e in p.trace)}
    elif name.startswith('tolerance_status_'):
        delta=int(name.split('_')[-1]);p.source_tip=p.tip+timedelta(minutes=delta);settime(min(p.tip,p.source_tip)-timedelta(minutes=70));p.sync();p.market();result=p.cmd('markets','kickoff-differences');rows=[f for d in result['days'] for f in d['fixtures'] if f.get('source_game_id')=='bdl:710001'];r={'status':rows[0]['status'],'delta_seconds':rows[0]['delta_seconds'],'qualified_days':result['paired_days']}
    elif name=='no_market_to_deadline':
        p.blank=True;p.early();p.at(64);before=p.forecast();p.at(61,seconds=-5);after=p.forecast();w=p.show()[-1];r={'first_waited':before.get('waiting_for_win_quote'),'final_seals':after.get('sealed'),'window':w['state'],'refs':sorted(w.get('prediction',{}).get('market_at_prediction',{}))}
    elif name=='clock_pairs':
        p.source_tip=p.tip+timedelta(minutes=10);p.early();p.at(63); p.market();p.at(53);p.forecast();p.at(50,seconds=-50);p.forecast();w=p.show()[-1];r={'state':w['state'],'match':w['prediction']['market_schedule'],'refs':sorted(w['prediction']['market_at_prediction']),'win':w['prediction']['market_at_prediction'].get('win')}
    elif name=='past_tolerance':
        p.source_tip=p.tip+timedelta(minutes=31);p.early();p.at(63);p.market();p.at(42);p.sync();p.at(32);p.forecast();w=p.show()[-1];r={'state':w['state'],'match':w['prediction']['market_schedule'],'refs':sorted(w['prediction']['market_at_prediction'])}
    elif name=='earlier_close':
        p.source_tip=p.tip-timedelta(minutes=10);p.early();p.at(63);p.market();p.at(2);m=p.market();r={'errors':m['errors'],'closes':p.raw('SELECT count(*) FROM nba_market_close')[0][0]}
    elif name=='ambiguous_venue':
        p.extra_events=[{**p.event,'id':'999998','gameId':999999,'startTime':(p.tip+timedelta(minutes=9)).isoformat()}];p.early();p.at(63);p.market();p.at(61);f=p.forecast();r={'errors':f['fixture_errors'],'windows':[(w['state'],w['note']) for w in p.show()]}
    elif name=='live_missing_datetime':
        p.status='In Progress';p.period=1;p.home_points=13;p.away_points=17;p.null_tip=True
        p.at(84);s=p.sync();p.at(63);m=p.market();r={'source_state':s['state'],'bdl_current':p.raw("SELECT json_extract(payload,'$.status'),json_extract(payload,'$.play_observed'),json_extract(payload,'$.kickoff'),json_extract(payload,'$.game_date') FROM nba_game_versions WHERE game_id='bdl:710001' ORDER BY version DESC LIMIT 1"),'market_state':m['state'],'errors':m['errors'],'books':p.raw('SELECT count(*) FROM nba_market_choices WHERE quality=\'ok\'')[0][0]}
    elif name=='nullable_live_duplicate':
        p.extra_bdl=[dict(p.row(),id=710002,datetime=None,status='In Progress',period=1,home_team_score=13,visitor_team_score=17)];p.early();p.at(64);m=p.market();p.at(61);f=p.forecast();p.at(59);p.forecast();w=p.show()[-1];r={'window':w['state'],'note':w['note'],'market_errors':m['errors'],'fixture_errors':f['fixture_errors'],'refs':sorted(w.get('prediction',{}).get('market_at_prediction',{})),'bdl':p.raw("SELECT game_id,json_extract(payload,'$.status'),json_extract(payload,'$.kickoff') FROM nba_game_versions WHERE game_id IN ('bdl:710001','bdl:710002')")}
    elif name.startswith('small_revision_'):
        p.early();p.at(63);p.market();first=clock;p.at(62,seconds=-45);p.source_tip=p.tip+timedelta(minutes=1);s=p.sync();known=clock
        if name.endswith('recapture'):
            p.at(62,seconds=10);p.market()
        p.at(60,seconds=10);p.forecast();ws=p.show();pred=ws[-1].get('prediction',{});q=pred.get('market_at_prediction',{}).get('win',{});r={'superseded':ws[0]['state'],'new':ws[-1]['state'],'original_at':first.isoformat(),'revision_available_by':known.isoformat(),'admitted_book':q.get('observed_at'),'after_revision':REAL.fromisoformat(q['observed_at'])>=known if q else None,'market_families':sorted(pred.get('market_at_prediction',{}))}
    elif name=='source_schedule_replaced':
        p.early();p.at(64);p.market();p.at(62);p.source_tip=p.tip+timedelta(minutes=10);s=p.sync();p.at(53);p.forecast();p.at(50,seconds=-50);f=p.forecast();ws=p.show();r={'source_state':s['state'],'errors':f['fixture_errors'],'windows':[(w['state'],w['kickoff'],sorted(w.get('prediction',{}).get('market_at_prediction',{})),w.get('prediction',{}).get('market_schedule',{}).get('status')) for w in ws]}
    elif name=='terminal_revisions':
        p.early();w=p.pair();r={'initial_seal':w['state'],'sealed_hash':w['sha256'],'history':[]}
        p.at(2);p.market();p.at(-190);p.terminal(118,109,'away_total');p.market();p.forecast();report=p.cmd('report','--date',str(p.day));sha=report['sha256'];rev=report['revision']
        def sample():
            summary=p.counts(); win=p.show()[-1]; g=win['grade_history'][-1]['grade']; cond=g['payout_reconciliation']['win'];return {'win':cond['state'],'game_grading':g['state'],'overtime':summary['verified_overtime_total_games'],'void':summary['counts']['void_50_50'],'window_sha':win['sha256'],'revision':cond['revision']}
        r['history'].append(sample())
        for i,(h,a,per,withdraw) in enumerate([(118,109,5,False),(108,119,4,False),(None,None,None,True),(118,109,5,False)]):
            p.at(-260-i*65)
            if withdraw: p.status='Scheduled';p.period=0;p.home_points=p.away_points=0
            else:p.final(h,a,per)
            p.sync();p.forecast();r['history'].append(sample())
        r['prior_report_unchanged']=p.cmd('report','--date',str(p.day),'--revision',str(rev))['sha256']==sha
        week=p.cmd('report','--week',str(p.day-timedelta(days=p.day.weekday())))['report']['payout_reconciliation'];r['week']={k:week[k] for k in ('native_conditions','counts','verified_overtime_total_games')}
    elif name=='budget':
        p.extra_events=[{**p.event,'id':str(1500000+i),'gameId':2500000+i} for i in range(14)]
        p.at(64); one=p.market();n=len(p.trace);p.at(63,seconds=10);two=p.market();n2=len(p.trace)
        req=p.raw('SELECT sent FROM nba_market_requests ORDER BY sent'); instants=[REAL.fromisoformat(s[0]) for s in req];maxrpm=max(sum(t-timedelta(minutes=1)<a<=t for a in instants) for t in instants)
        r={'first_errors':one['errors'],'second_errors':two['errors'],'count_first':n,'count_second':n2-n,'max_rolling_60s':maxrpm,'observed_windows':p.raw('SELECT count(distinct id) FROM nba_market_choices')[0][0],'market_sides':p.raw("SELECT count(*) FROM nba_market_choices WHERE quality='ok'")[0][0]}
    elif name=='rate':
        p.early();p.at(64);p.http429=True;a=p.market();before=len(p.trace);p.at(63);b=p.market();after=len(p.trace);p.at(61,seconds=5);p.http429=False;c=p.market();r={'first':a['errors'],'suppressed':after==before,'retry':b['errors'],'recovered':c['state'],'choices':p.raw("SELECT count(*) FROM nba_market_choices WHERE quality='ok'")[0][0]}
    else:raise ValueError(name)
    r['http_requests']=len(p.trace);r['endpoints']=sorted(set(x['method']+' '+x['path'] for x in p.trace));return r

NAMES=['compare_2','compare_-2','compare_2_resample','compare_5','compare_-5','compare_10','compare_31','compare_180','tolerance_status_-30','tolerance_status_30','tolerance_status_-180','no_market_to_deadline','late','clock_pairs','past_tolerance','earlier_close','ambiguous_venue','live_missing_datetime','nullable_live_duplicate','small_revision_nofresh','small_revision_recapture','source_schedule_replaced','terminal_revisions','rate','budget']
if __name__=='__main__':
    args=argparse.ArgumentParser();args.add_argument('--out',type=Path,required=True);args.add_argument('names',nargs='*');opts=args.parse_args()
    results={}; opts.out.parent.mkdir(parents=True,exist_ok=True)
    for name in opts.names or NAMES:
        with tempfile.TemporaryDirectory(prefix='system-cli-',dir=opts.out.parent) as tmp:
            try:results[name]=run_one(name,Path(tmp)/'nba')
            except Exception as exc:
                import traceback
                results[name]={'error':str(exc),'traceback':traceback.format_exc()}
        opts.out.write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(results,ensure_ascii=False,indent=2))
