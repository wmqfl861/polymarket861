"""Check raw hash, native window, both token books and midpoint for each frozen real selection."""
from datetime import timedelta
from decimal import Decimal
import argparse,json,zipfile
from pathlib import Path
from collections import Counter
from audit_originals import dt,sha,levels
p=argparse.ArgumentParser();p.add_argument('source',type=Path);a=p.parse_args();repo=a.source
report=json.loads((repo/'reports/nba-phase1/market-collector-20261128T0126Z.json').read_text()); archive=repo/'tests/fixtures/nba'/report['fixture'];assert sha(archive.read_bytes())==report['fixture_sha256'];orig=Counter();events=[]
with zipfile.ZipFile(archive) as z:
    for e in report['event_windows']:
        ev,books=[json.loads(z.read(e[k]['path'])) for k in ('gamma','clob')];bookmap={b['asset_id']:b for b in books};markets={m['conditionId']:m for m in ev['markets']};assert len(bookmap)==len(books)
        for k in ('gamma','clob'):assert sha(z.read(e[k]['path']))==e[k]['sha256'] and e[k]['http_status']==200
        due=dt(e['kickoff'])-timedelta(minutes={'T-60m':60,'T-24h':1440}[e['window']]);rank={};families={}
        for family,v in e['choices'].items():
            assert due-timedelta(minutes=5)<=dt(v['observed_at'])<=dt(v['available_at'])<due
            orig[e['window']+' '+v['quality']]+=1
            if v['quality']=='unlisted':continue
            assert v['quality']=='ok';m=markets[v['condition_id']];tok=v['token_id'];tokens=json.loads(m['clobTokenIds']);labels=json.loads(m['outcomes']);home=next(c['name'] for c in ev['teams'] if c['ordering']=='home');assert tok==tokens[labels.index(home if family=='win' else 'Over')]
            q=[levels(bookmap[t]) for t in tokens];assert q[0][0]+q[1][1]==q[0][1]+q[1][0]==1;assert sum(q[tokens.index(tok)])/2==Decimal(str(v['midpoint'])) and all(bookmap[t]['market']==m['conditionId'] for t in tokens);assert e['clob']['received']==v['observed_at']
            families[family]=v['midpoint']
        for cid in e['spread_conditions_archived']:
            m=markets[cid];assert m['sportsMarketType']=='spreads';assert all(bookmap[t]['market']==cid for t in json.loads(m['clobTokenIds']));orig['archived_spread']+=1
        events.append({'id':e['id'],'window':e['window'],'clob_received':e['clob']['received'],'midpoints':families})
print(json.dumps({'verified_event_windows':len(events),'counts':orig,'events':events,'fixture_sha256':sha(archive.read_bytes())},ensure_ascii=False,indent=2))
