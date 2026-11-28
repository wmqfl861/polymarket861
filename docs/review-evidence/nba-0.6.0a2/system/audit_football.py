"""Exercise the six-ID hard gate with artificial signed market inputs and the real released engine."""
import argparse
from decimal import Decimal
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch
p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('oldsource',type=Path);p.add_argument('gitrepo',type=Path);p.add_argument('--work',type=Path,required=True);args=p.parse_args()
repo=args.source.resolve(); old=args.oldsource.resolve(); git=args.gitrepo.resolve();work=args.work.resolve(); sys.path[:0]=[str(repo/'src'),str(repo/'tests')]
from polymarket861 import big5_market,market_archive,store
from test_budget_domestic_archive import domestic_entry
import test_budget_domestic_archive as factories
from test_capital_native_proof import record_terminal,terminal_row
released=subprocess.check_output(['git','-C',str(git),'show','60c1a2d:deploy/paper-automation/paper-10000/account_engine.py']); paper=repo/'deploy/paper-automation/paper-10000';sys.path.insert(0,str(paper));render=runpy.run_path(str(paper/'monitor.py'))['render_position']
IDS=['sea-rom-mon-2026-11-29-rom-no','fl1-sbr-pfc-2026-11-27-pfc-no','epl-ful-bou-2026-11-29-total-2pt5-under','sea-fro-par-2026-11-28-par-yes','epl-lee-cov-2026-11-28-cov-yes','sea-int-gen-2026-11-28-int-no']

def sha(data):return hashlib.sha256(data).hexdigest()
def fingerprint(d):return {str(p.relative_to(d)):sha(p.read_bytes()) for p in d.rglob('*') if p.is_file()}
def block(mode):
    with TemporaryDirectory(dir=work,prefix='six-paper-') as tmp:
        acc=Path(tmp)/'account';acc.mkdir();(acc/'account_engine.py').write_bytes(released)
        former=runpy.run_path(str(acc/'account_engine.py'))['replay'];base=factories.native;allbooks=[];events=[];intents=[];states={}
        for idx,ident in enumerate(IDS,1):
            league=ident.split('-')[0]
            def native(aux=False,*,league=league):
                e=base(aux,league=league);e['id']=str(idx*100+int(e['id']));e['slug']=e['slug'].replace('-one-two-',f'-one{idx}-two{idx}-')
                for club in e['teams']:club['abbreviation']+=str(idx)
                for m in e['markets']:
                    num=idx*1000+int(m['conditionId'],16);m.update(slug=m['slug'].replace('-one-two-',f'-one{idx}-two{idx}-'),conditionId='0x'+format(num,'064x'),clobTokenIds=[str(num*10),str(num*10+1)])
                return e
            with patch.object(factories,'native',native):d,(ev,aux)=domestic_entry(acc,league,explicit=True)
            intent=d['intents'][0];intent.update(id=ident,rank=idx,decision_book=f'{idx}-a',execution_book=f'{idx}-b',sizing={'model_fair':'.55','news_fair':'.55','external_fair':'.52','sizing_bankroll':'10000','kelly_fraction':'.25','budget':'100'});intents.append(intent)
            for book in d['books']:book.update(id=f"{idx}-{book['id']}",bids=[{'price':'0.49','size':'200'}]);allbooks.append(book)
            # Winners, losers and one pending position. Settle two outcomes at the same instant.
            if idx<6:
                payout=idx%2;when=store.iso(store.instant(intent['kickoff'])+timedelta(hours=3,minutes=idx//2));native_root=big5_market.research_root(acc);m=market_archive.contracts(native_root,ev['slug'],as_of=intent['decision_at'])[0]
                for pos,v in enumerate([payout,1-payout]):
                    term=terminal_row(m,pos,v);h=record_terminal(native_root,term['token_id'],json.dumps({'data':[term]}).encode(),when)
                    if term['token_id']==intent['token_id']:
                        e={'id':f'pay{idx}','at':when,'original_sha256':h,**{k:intent[k] for k in ('fixture_id','condition_id','token_id')}};events.append(e);states[ident]={'state':'settled','event':e,'payout':str(payout)}
            else:states[ident]={'state':'awaiting_official_result'}
        ledger={**d,'account_policy':'shared-quarter-kelly-v1','intents':intents,'books':allbooks,'redemptions':[]};asof=store.iso(store.instant(events[-1]['at'])+timedelta(hours=1));
        if mode in ('bad_bid','bad_bid_open'):ledger['books'][4 if mode=='bad_bid' else 10]['bids'][0]['price']='0.48'
        old_report=former(dict(ledger,redemptions=events),as_of=asof,data_dir=acc);assert all(r['outcome']=='filled' for r in old_report['decisions']),old_report['proof_gaps'];(acc/'account-input.json').write_text(json.dumps(ledger));positions=[render(op,next(r for r in old_report['decisions'] if r['id']==op['id']),{},states[op['id']]) for op in intents];old_report.update(state='ok',account_input_sha256=sha((acc/'account-input.json').read_bytes()),positions=positions)
        if mode=='missing_original':old_report['positions'].pop()
        (acc/'account-status.json').write_text(json.dumps(old_report));(acc/'redemptions.json').write_text(json.dumps({'as_of':asof,'active_events':events}));before=fingerprint(acc)
        env=os.environ.copy();env.update(PYTHONPATH=str(old/'src'),PYTHONDONTWRITEBYTECODE='1',TMPDIR=str(work));cmd=subprocess.run([sys.executable,str(paper/'check_account_upgrade.py'),'--account-dir',str(acc)],cwd=work,env=env,capture_output=True,text=True); assert cmd.stdout,(cmd.returncode,cmd.stderr)
        result=json.loads(cmd.stdout);result.update(exit=cmd.returncode,all_original_files_byte_identical=before==fingerprint(acc),original_files=len(before),published_cash=old_report['total_available_cash'],published_pnl=old_report['total_settled_paper_pnl'],published_open_cost=old_report['total_unredeemed_cost']);return result
print(json.dumps({m:block(m) for m in ['healthy','bad_bid','bad_bid_open','missing_original']},indent=2,ensure_ascii=False))
