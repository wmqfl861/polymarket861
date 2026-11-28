"""Recompute historical NBA market selections and terminal payouts from raw bytes, independent of product imports."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import re
import zipfile


def sha(b):return hashlib.sha256(b).hexdigest()
def dt(s):return datetime.fromisoformat(s.replace('Z','+00:00'))
def levels(b):
    values=[]
    for field,which in [('bids',max),('asks',min)]:
        rows=b[field];prices=[D(str(row['price'])) for row in rows]
        if not prices or any(D(str(row['size']))<=0 for row in rows) or any(not 0<p<1 for p in prices) or len(set(prices))!=len(prices):return None
        values.append(which(prices))
    return values if values[0]<values[1] else None

def pregame(root):
    report=json.loads((root/'reports/nba-phase1/market-lines-20261128.json').read_text()); paired={r['event_id']:r for r in report['events']}; size=Counter(); checks=[]
    path=root/'tests/fixtures/nba/market-lines-20261128.zip'
    assert sha(path.read_bytes())==report['fixture_sha256']
    with zipfile.ZipFile(path) as z:
        manifest=json.loads(z.read('manifest.json'));assert len(manifest)==len(paired)==11
        for receipt in manifest:
            event,book=[json.loads(z.read(receipt[k]['path'])) for k in ('gamma','clob')]; ref=paired[event['id']]; score=defaultdict(list); n=Counter(); expected={}; req={str(t) for t in receipt['clob']['params']['token_ids']}
            for k in ('gamma','clob'):
                raw=z.read(receipt[k]['path']); assert sha(raw)==receipt[k]['sha256']==ref[k+'_sha256'] and receipt[k]['http_status']==200
            assert dt(receipt['gamma']['received'])<=dt(receipt['clob']['received'])<dt(event['startTime'])
            books={r['asset_id']:r for r in book}; assert len(books)==len(book) and set(books)==req
            demand=set(); clubs={r['ordering']:r for r in event['teams']}; assert event['period']=='NS' and event.get('live') is not True and event.get('ended') is not True
            for m in event['markets']:
                if not (m.get('active') is True and m.get('closed') is False and m.get('acceptingOrders') is True and m.get('enableOrderBook') is True):continue
                typ=m.get('sportsMarketType'); slug=m['slug'];label='Over'
                if typ=='moneyline' and slug==event['slug']: family='win';label=clubs['home']['name']
                elif typ=='totals':assert slug.startswith(event['slug']+'-total-');family='total'
                elif typ=='spreads':assert slug.startswith(event['slug']+'-spread-');family='spread'
                elif typ=='team_totals':
                    family=next(side+'_total' for side,c in clubs.items() if slug.startswith(event['slug']+'-team-total-'+c['abbreviation'].lower()+'-'))
                else:continue
                cid=m['conditionId'];toks=json.loads(m['clobTokenIds']);labels=json.loads(m['outcomes']); assert len(set(toks))==2
                demand.update(toks);n[family]+=1
                if family=='spread':continue
                quotes=[levels(books[t]) for t in toks]; obs=dt(receipt['clob']['received']).timestamp()
                if any(not v for v in quotes) or any(books[t]['market']!=cid or int(books[t]['timestamp'])/1000>obs for t in toks):continue
                assert quotes[0][0]+quotes[1][1]==1 and quotes[0][1]+quotes[1][0]==1
                side=labels.index(label); mid=sum(quotes[side])/2
                score[family].append((abs(mid-D('.5')),cid.lower(),toks[side],mid,m.get('line'),(m.get('marketMetadata') or {}).get('line_kind')))
            assert demand==req
            for family,official in ref['families'].items():
                assert n[family]==official['listed'] and len(score[family])==official['two_sided']
                if score[family]:
                    _,cid,tok,mid,line,kind=min(score[family]);assert (cid,tok,mid,line,kind)==(official['condition_id'].lower(),official['token_id'],D(str(official['midpoint'])),official['line'],official['line_kind'])
                    size['alternate']+=kind=='alternate'; size['selected']+=1
                else:assert official['quality']=='unlisted';size['absent']+=1
            size['listings']+=sum(n.values());size['forecast']+=sum(n.values())-n['spread'];size['spreads']+=n['spread'];size['books']+=len(books)
            checks.append({'event':event['id'],'games':event['slug'],'requests':2,'book_batch_size':len(books),'listed':dict(n)})
    return dict(counts=dict(size),events=checks,fixture_sha=sha(path.read_bytes()),max_tokens=max(i['book_batch_size'] for i in checks))

def terminals(root,cache):
    report=json.loads((root/'reports/nba-phase1/v2-overtime-reconciliation.json').read_text()); cases=[];n=Counter()
    for record in [*report['overtime_games'],report['original_report_fixture']]:
        srcs={'gamma':(record['gamma_receipt'],f"gamma-{record['gamma_receipt']['url'].rsplit('/',1)[-1]}.json"),'scores':(record['scoreboard_receipt'],f"score-{record['game_day_et'].replace('-','')}.json")}; srcs.update({t['side']:(t['receipt'],'token-'+t['token_id']+'.json') for t in record['tokens']})
        raw={}
        for kind,(m,name) in srcs.items():
            b=(cache/name).read_bytes();assert sha(b)==m['sha256'];raw[kind]=json.loads(b);n['verified_raw']+=1
        g=raw['gamma'];s=next(e for e in raw['scores']['events'] if e['id']==record['espn_event_id']);m=next(m for m in g['markets'] if m['conditionId']==record['condition_id']);assert m['umaResolutionStatus']=='resolved' and m['closed'] is True
        assert dt(g['startTime'])==dt(s['date']) and g['ended'] is True and s['status']['type']['completed'] and str(g['id'])==record['venue_event_id']
        parts={side:next(c for c in s['competitions'][0]['competitors'] if c['homeAway']==side) for side in ('home','away')}
        series={side:[int(q['value']) for q in c['linescores']] for side,c in parts.items()};reg={side:sum(q[:4]) for side,q in series.items()};final={side:sum(q) for side,q in series.items()};ot=s['status']['period']>4
        assert series==record['points_by_period'] and all(len(q)==s['status']['period'] and str(sum(q))==parts[side]['score'] for side,q in series.items()) and (not ot or reg['home']==reg['away'])
        assert str(g['score'])==str(final['away'])+'-'+str(final['home']) and str(m['line'])==str(record['selected_line']) and m['sportsMarketType']=='totals' and m['marketMetadata']['line_family']=='full.total'
        assert sha(m['description'].encode())==record['rule_sha256'];names=json.loads(m['outcomes']);prices=json.loads(m['outcomePrices']);tokens=json.loads(m['clobTokenIds']); official=dict(zip(names,prices,strict=True)); expect=sum(final.values())>m['line']
        for side in ('over','under'):
            original,=raw[side]['data']; idx=names.index(side.title()); assert original['condition_id']==m['conditionId'] and original['token_id']==tokens[idx] and original['opposite_token_id']==tokens[1-idx] and original['resolved'] is True and D(str(original['final_price']))==D(official[side.title()])==int(expect if side=='over' else not expect)
        decisive=(sum(reg.values())>m['line'])!=expect; assert decisive==record['decisive_for_selected_line'];n['ot']+=int(ot);n['decisive']+=int(decisive);cases.append(dict(event=g['id'],day=record['game_day_et'],quarters=s['status']['period'],regulation=sum(reg.values()),final=sum(final.values()),line=m['line'],decisive=bool(decisive)))
    return dict(counts=dict(n),games=cases)
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('repo',type=Path);p.add_argument('terminal_cache',type=Path);arg=p.parse_args();print(json.dumps({'pregame':pregame(arg.repo),'terminal':terminals(arg.repo,arg.terminal_cache)},ensure_ascii=False,indent=2))
