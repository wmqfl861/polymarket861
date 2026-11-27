"""Freeze the six previously screened domestic-league contracts against current public sources."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from decimal import Decimal as D
from pathlib import Path
import hashlib
import json
import time

import football_odds_access
from archive_slate_evidence import verify

from polymarket861 import budget_archive_proof as proof, big5_market, market_archive as archive, market_provider as provider, store

ROOT=Path(__file__).resolve().parent
VAULT=big5_market.research_root(ROOT)
REPORTS=ROOT.parent/'five-automation'/'reports'
STUDY=ROOT/'source-evidence'/'pm861-weekly-value-20261127'
def read(path): return json.loads(path.read_text())

def odds_fraction(us):
    x=D(us)
    return D(1)/(1+x/100 if x>0 else 1-100/x)

def espn(spec):
    date = spec['kickoff'][:10].replace('-', '')
    data, permission_sha = football_odds_access.fetch(
        spec['league'], 'scoreboard', {'dates': date, 'limit': 100, 'observation': time.time_ns()}
    )
    assert provider.fresh_observation(data)
    score=json.loads(data.body)
    es=[e for e in score['events'] if str(e['id'])==spec['espn_id']]
    assert len(es)==1
    g=es[0]; c=g['competitions'][0];assert g['status']['type']['name']==c['status']['type']['name']=='STATUS_SCHEDULED'
    assert provider.moment(g['date'])==provider.moment(spec['kickoff'])
    sides={t['homeAway']:t['team']['displayName'] for t in c['competitors']}
    assert [sides['home'],sides['away']]==spec['espn_teams'],(spec['fixture'],sides)
    o=next(x for x in c['odds'] if x['provider']['name']=='DraftKings')
    if spec['family']=='total':
        opts={s:o['total'][s]['close'] for s in ('under','over')}
        assert opts['under']['line']=='u2.5' and opts['over']['line']=='o2.5'
        prices={k:v['odds'] for k,v in opts.items()}
        side=spec['outcome'].lower()
    else:
        prices={k:o['moneyline'][k]['close']['odds'] for k in ('home','draw','away')}
        side=spec['side']
    inv={k:odds_fraction(v) for k,v in prices.items()}
    prob=inv[side]/sum(inv.values())
    if spec['outcome']=='No': prob=1-prob
    digest=hashlib.sha256(data.body).hexdigest()
    folder=ROOT/'bookmaker-receipts';folder.mkdir(mode=0o700,exist_ok=True)
    (folder/(digest+'.json')).write_bytes(data.body)
    return {'permission_sha256':permission_sha,'probability':str(prob),'receipt':data.received_at,'source_sha256':digest,'prices':prices,'match_url':'https://www.espn.com/soccer/match/_/gameId/'+spec['espn_id']}

def collect_gamma(spec):
    pair=None
    for slug in (spec['fixture'],spec['fixture']+'-more-markets'):
        url=provider.GAMMA+'/events/slug/'+slug
        event,reply,sha=archive.research_fetch(VAULT,'league_event',slug,url,{'observation':time.time_ns()})
        assert provider.fresh_observation(reply)
        with closing(big5_market._db(VAULT)) as db,db:
            db.execute('BEGIN IMMEDIATE')
            identity=big5_market._identity(event,spec['league'],history=db,received_at=reply.received_at)
            assert identity[0]==slug and identity[1]==spec['fixture'] and identity[3]==spec['kickoff']
            assert event['active'] is True and event['closed'] is event['archived'] is False
            assert big5_market.schedule.pregame_flags(event,reply.received_at) in ('explicit_pregame','schedule_only')
            assert pair is None or pair==identity[3:6]
            pair=identity[3:6]
            big5_market._record(db,event,identity,reply,sha)
    allmarkets=archive.contracts(VAULT,spec['fixture'])
    selected,_=big5_market._selection(allmarkets,True)
    chosen=[c for c in selected.values() if c['condition_id']==spec['condition_id'] and c['slug']==spec['market_slug']]
    assert len(chosen)==1
    m=chosen[0]
    assert any(t['token_id']==spec['token_id'] and t['outcome']==spec['outcome'] for t in m['tokens'])
    terms=proof.verify_contract(ROOT,m['condition_id'],store.now_utc(),token=spec['token_id'],scheduled=True)
    fixture_id='/'.join([spec['league'],'2026-27',terms['home_club_id'],terms['away_club_id']])
    fd=provider.get(provider.CLOB+'/clob-markets/'+m['condition_id'],{'observation':time.time_ns()},timeout=12)
    feed=json.loads(fd.body)
    assert provider.fresh_observation(fd) and feed['ao'] is True and feed['c']==m['condition_id']
    assert D(str(feed['fd']['r']))==D(str(m['fee_schedule']['rate'])) and feed['fd']['e']==m['fee_schedule']['exponent'] and feed['fd']['to'] is m['fee_schedule']['takerOnly']
    return {'fixture_id':fixture_id,'opposite_token_id':next(t['token_id'] for t in m['tokens'] if t['token_id']!=spec['token_id']), 'market':m,'terms':terms['intent_terms'],'clob_info_checked_at':fd.received_at}

def run():
    verify(STUDY)
    full=read(STUDY/'markets.json')['markets']
    epl=read(REPORTS/'epl-round12-recheck-20261127.json')['entries']
    fs={x['slug']:x for x in read(STUDY/'forecasts.json')['fixtures']}
    slug_specs=[
      ('sea-rom-mon-2026-11-29-rom','No','罗马 vs 蒙扎 · 蒙扎不败','0.21','401874769',['AS Roma','Monza']),
      ('fl1-sbr-pfc-2026-11-27-pfc','No','布雷斯特 vs 巴黎FC · 布雷斯特不败','0.58','401876392',['Brest','Paris FC']),
      ('epl-ful-bou-2026-11-29-total-2pt5','Under','富勒姆 vs 伯恩茅斯 · 全场小2.5','0.47','401879228',['Fulham','AFC Bournemouth']),
      ('sea-fro-par-2026-11-28-par','Yes','弗罗西诺内 vs 帕尔马 · 帕尔马胜','0.16','401875081',['Frosinone','Parma']),
      ('epl-lee-cov-2026-11-28-cov','Yes','利兹联 vs 科文垂 · 科文垂胜','0.14','401879227',['Leeds United','Coventry City']),
      ('sea-int-gen-2026-11-28-int','No','国际米兰 vs 热那亚 · 热那亚不败','0.25','401874977',['Internazionale','Genoa'])]
    with closing(big5_market._db(VAULT)):pass
    slate=[]
    for rank,(slug,outcome,display,limit,event_id,teams) in enumerate(slug_specs):
        rows=[x for x in full if x['slug']==slug]
        assert len(rows)==1
        m=rows[0];fspec=fs[m['fixture']]
        t=next(x for x in m['tokens'] if x['outcome']==outcome)
        e=next((e for e in epl if e['token_id']==t['token_id']),None)
        if e:
            p=e['news_probability']; fair=min(D(e['conservative_fair']),D(e['fair_without_news']));model_sha=e['forecast_hash']
        else:
            probs=fspec['ranking_probabilities'];key=m['side']+'_win';p=D(str(probs[key]));p=1-p if outcome=='No' else p
            fair=D('.99')*p;model_sha=fspec['ranking_sha256']
        if slug == 'sea-int-gen-2026-11-28-int' and model_sha != '42185e5e46494a9d74a0e5fb68c3542458e326deca7cdea3a850663bf08fee87':
            raise ValueError('historical Inter model digest differs from the archived forecast')
        spec={'id': slug+'-'+outcome.lower(),'rank':rank,'league':m['league'],'fixture':m['fixture'],'market_slug':slug,'outcome':outcome,'condition_id':m['condition_id'],'token_id':t['token_id'],'forecast_fixture_id':m['fixture_id'],'kickoff':m['kickoff'],'family':m['family'],'side':m['side'],'line':m['line'],'display':display,'limit_price':limit,'model_probability':str(p),'fair':str(fair),'model_sha256':model_sha,'espn_id':event_id,'espn_teams':teams}
        slate.append(spec)
    with ThreadPoolExecutor(max_workers=4) as pool:
        e_jobs=[pool.submit(espn,s) for s in slate]
        g_jobs=[pool.submit(collect_gamma,s) for s in slate]
        for s, external,native in zip(slate,e_jobs,g_jobs):
            s['bookmaker']=external.result()
            s.update(native.result())
    (ROOT/'slate.json').write_text(json.dumps(slate,ensure_ascii=False,indent=1))
    print([(s['display'],s['bookmaker']['probability'][:10]) for s in slate])
if __name__=='__main__':run()
