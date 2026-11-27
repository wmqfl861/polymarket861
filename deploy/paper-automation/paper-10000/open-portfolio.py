"""Execute the frozen six research selections once, with a shared account and independent native market receipts."""
import json
import os
import fcntl
import time
from contextlib import closing
from datetime import timedelta
from decimal import Decimal as D
from pathlib import Path
from polymarket861 import budget_archive_proof as proof, five_league_budget as budget, ledger_rules as r, market_archive as archive, market_provider as provider, store
import football_odds_access
from account_engine import FUND, FRACTION, POLICY, replay, size_at_book

ROOT=Path(__file__).resolve().parent
VAULT=ROOT/'five-league-market'
def load(path):return json.loads(path.read_text())
def save(path,value):
    tmp=path.with_suffix(path.suffix+'.working')
    with tmp.open('w') as f:
        json.dump(value,f,default=str,ensure_ascii=False,indent=1);f.flush();os.fsync(f.fileno())
    tmp.replace(path)
def log(at,kind,data):
    with (ROOT/'entry-attempts.jsonl').open('a') as f:f.write(json.dumps({'at':at,'type':kind,'data':data},ensure_ascii=False,default=str)+'\n')
def books_for(specs):
    tokens=sorted(x['token_id'] for x in specs)
    data,reply,sha=archive.research_fetch(VAULT,'books','shared-paper-slate-v1',provider.CLOB+'/books',{'token_ids':tokens},attempts=1)
    if not provider.fresh_observation(reply):raise ValueError('HTTP snapshot too old')
    raw={str(z['asset_id']):z for z in data}
    if set(raw)!=set(tokens) or len(raw)!=len(data):raise ValueError('incomplete book batch')
    items={}
    with closing(archive.database(VAULT)) as db,db:
        for s in specs:
            t=s['token_id'];m=s['market']
            b=archive._book_value(db,m,t,raw[t],reply,sha,m['game_times'])
            db.execute('INSERT OR IGNORE INTO ma_books VALUES (?,?,?,?)',(t,reply.received_at,sha,store.canonical(b)))
            items[s['id']]=b
    return items,reply
def as_input(b,s,suffix):
    return {**{k:b[k] for k in ('token_id','condition_id','received_at','source_sha256','bids','asks','book_hash','tick_size','min_order_size','exchange_at','observed_at')},'id':s['id']+'-'+suffix+'-'+b['received_at'],'fixture_id':s['fixture_id']}
def verify_comparison_receipt(pick, at):
    grant = football_odds_access.permission_digest(pick["league"], "scoreboard")
    bookmaker = pick["bookmaker"]
    if bookmaker.get("permission_sha256") != grant:
        raise ValueError("external odds receipt lacks current reviewed permission")
    age = r.instant(at) - r.instant(bookmaker["receipt"])
    if not timedelta(0) <= age < timedelta(minutes=15) or r.instant(at) >= r.instant(pick["kickoff"]):
        raise ValueError("external odds receipt outside pregame validity")


def plan(spec,b,first_terms):
    fee=first_terms['fee'];bid=D(b['bids'][0]['price']);ask=D(b['asks'][0]['price'])
    fair=D(spec['fair']);mid=(bid+ask)/2
    q=r.fair_values(D(spec['bookmaker']['probability']),{'special':spec['entry_terms']['special'],'family':spec['entry_terms']['family'],'side':spec['entry_terms']['side']})[0]
    news_fair=r.fair_values(D(spec['model_probability']),spec['entry_terms'])[0]
    if not 0<ask-bid<=r.MAX_SPREAD or max(abs(fair-mid),abs(news_fair-mid))>r.MAX_GAP:return None
    s=size_at_book(r.series(b,'asks'),fee,fair,q,FUND,D(spec['limit_price']))
    if not s or s['shares']<D(b['min_order_size']) or fair-s['planning_cost']<r.MIN_EDGE:return None
    return {**s,'model_fair':fair,'news_fair':news_fair,'external_fair':q,'sizing_bankroll':FUND,'kelly_fraction':FRACTION}
def public_specs(slate):
    return [{k:v for k,v in s.items() if k not in ('market','terms','entry_terms')} for s in slate]
def main():
    shortlist=load(ROOT/'slate.json');dbfile=ROOT/'account-input.json';picksfile=ROOT/'picks.json'
    doc=load(dbfile) if dbfile.exists() else {'schema':budget.SCHEMA,'evidence_mode':'local_receipts','account_policy':POLICY,'originals':{},'books':[],'intents':[],'redemptions':[]}
    filled_ids = {op['id'] for op in doc['intents']}
    pending = [pick for pick in shortlist if pick['id'] not in filled_ids]
    if not pending:
        return
    for pick in pending:
        verify_comparison_receipt(pick, store.now_utc())
    save(dbfile,doc)
    latest={s['id']:s for s in load(picksfile)} if picksfile.exists() else {}
    for s in shortlist:
        src=proof.verify_contract(ROOT,s['condition_id'],store.now_utc(),token=s['token_id'],scheduled=True)
        s['terms']=src['intent_terms']
        with proof.read_archive(ROOT) as (root,db):
            from polymarket861 import archive_proof
            m=archive_proof.original_market(db,root,s['condition_id'],store.now_utc())
            s['entry_terms']=proof._terms(root,m,s['token_id'],store.now_utc(),scheduled=True)
        s['max_stake']=latest.get(s['id'],{}).get('max_stake','0')
        s['blended_probability']=latest.get(s['id'],{}).get('blended_probability')
    for attempt in range(36):
        todo=[s for s in shortlist if s['id'] not in filled_ids]
        if not todo:break
        first,r1=books_for(todo)
        decision_time=store.now_utc()
        target=store.instant(decision_time)+timedelta(seconds=5)
        plans={}
        for s in todo:
            book=first[s['id']]
            verify_comparison_receipt(s, decision_time)
            splan=plan(s,book,s['terms'])
            if splan:
                s['max_stake']=str(splan['budget']);s['blended_probability']=str(splan['probability'])
                if store.instant(book['exchange_at'])>=store.instant(decision_time)-timedelta(seconds=30):
                    plans[s['id']]=splan
        save(picksfile,public_specs(shortlist))
        log(decision_time,'prospective-plans',{key:{'limit_price':next(s['limit_price'] for s in todo if s['id']==key),'first_receipt':r1.received_at,**v} for key,v in plans.items()})
        start_in=(target-store.instant(store.now_utc())).total_seconds()+2.4
        if start_in>0:time.sleep(start_in)
        second,r2=books_for(todo)
        end=store.instant(r2.received_at)
        new=[]
        for s in todo:
            key=s['id'];b=second[key];pl=plans.get(key)
            if pl is None:continue
            if not target<=store.instant(r2.requested_at)<=end<=target+timedelta(seconds=5):continue
            if not target<=store.instant(b['exchange_at'])<=end:continue
            verify_comparison_receipt(s, r.iso(end))
            a=as_input(first[key],s,'decision');z=as_input(b,s,'execution')
            op={'id':key,'fixture_id':s['fixture_id'],'kickoff':s['kickoff'],'decision_at':decision_time,'rank':s['rank'],'shares':str(pl['shares']),'limit_price':s['limit_price'],'decision_book':a['id'],'execution_book':z['id'],'sizing':{k:str(v) for k,v in pl.items()},**s['terms']}
            candidate={**doc,'intents':doc['intents']+[op],'books':doc['books']+[a,z]}
            report=replay(candidate,as_of=store.now_utc(),data_dir=ROOT)
            got=next(d for d in report['decisions'] if d['id']==key)
            log(store.now_utc(),got['outcome'],{'id':key,'reason':got.get('reason'),'cost':got['cost_including_cash_fees'],'decision_at':decision_time,'exec_at':end})
            if got['outcome']!='filled':
                print('RECHECK',key,got['outcome'],got.get('reason',''),flush=True);continue
            if any(d['outcome']!='filled' for d in report['decisions']):raise ValueError('prior verified fill changed')
            candidate=json.loads(json.dumps(candidate,default=str))
            save(dbfile,candidate);doc=candidate
            save(ROOT/'entry-report.json',report)
            filled_ids.add(key);new.append(key)
        print('Attempt',attempt+1,'prospective',len(plans),'fresh executions',len(new),'open positions',len(filled_ids),'/6',str([n.split('-2026-')[0] for n in new]),flush=True)
    print('OPENING',[(d['id'],d['cost_including_cash_fees']) for d in replay(doc,as_of=store.now_utc(),data_dir=ROOT)['decisions']],flush=True)
if __name__=='__main__':
    with (ROOT/'.entry.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        main()
