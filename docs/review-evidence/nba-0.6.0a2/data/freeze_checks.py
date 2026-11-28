"""Run the independent data review against files extracted from one committed Git tree."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE=Path(__file__).resolve().parent
WORK=HERE.parent.parent
PY=WORK/'five05-validation/venv/bin/python'
REPO=WORK/'polymarket861'

def git(*args):
    return subprocess.check_output(['git','-C',str(REPO),*args],text=True).strip()


def main(sha):
    sha=git('rev-parse',f'{sha}^{{commit}}');root=HERE/'frozen'/sha[:12]
    root.mkdir(parents=True,exist_ok=True);out=root.parent/(sha[:12]+'-output');out.mkdir(exist_ok=True)
    with tempfile.TemporaryFile() as tmp:
        subprocess.run(['git','-C',str(REPO),'archive','--format=tar',sha],stdout=tmp,check=True);tmp.seek(0)
        with tarfile.open(fileobj=tmp) as tar:tar.extractall(root,filter='data')
    env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONPATH=os.pathsep.join(map(str,(root/'src',root/'tests'))))
    for k in ('BALLDONTLIE_API_KEY','PM861_BALLDONTLIE_KEY','BIGBALLS_API_KEY','DUNKAPI_API_KEY'):
        env.pop(k,None)
    def task(name,args,allowed=(),parse=True):
        path=out/(name+('.json' if parse else '.txt'))
        proc=subprocess.run([str(PY),*map(str,args)],cwd=root,env=env,capture_output=True,timeout=300)
        if proc.returncode not in {0,*allowed}:
            raise RuntimeError((name,proc.stderr.decode(errors='replace')[-2000:]))
        path.write_bytes(proc.stdout if parse else proc.stdout+proc.stderr)
        if parse:return json.loads(path.read_bytes())
        return path.read_text()
    def cmd(*s):return ['-m','polymarket861','--data-dir',str(out/'empty'), 'nba',*s]
    configs={**{k:([HERE/'data_audit.py','--run',k],) for k in ('players','schedule','mixed')},
      **{k:([HERE/'history_audit.py','--run',k,'--repo',root],) for k in ('archive','future','upgrade')},
      **{('forward-'+k):([HERE/'forward_14.py',k],) for k in ('positive','off','gap')},
      'acceptance-clock':([HERE/'acceptance_clock.py'],), 'legacy-pair':([HERE/'legacy_pair.py'],),
      'replay-clock':([HERE/'replay_clock.py',root],), 'no-keys':(cmd('data','capabilities'),),
      'empty-forward':(cmd('readiness','--through','2026-11-26'),),
      'no-keys-schedule':(cmd('data','verify-schedule','--through','2026-11-26'),(1,)),
      'no-keys-warmup':(cmd('model','warmup','--season','2026','--through','2026-11-26','--sync'),(1,)),
      'no-keys-modern':(cmd('model','modern-study','--sync'),(1,)),
      'repository-data-tests':(['-m','unittest','-q','test_nba_data','test_nba_data_admission','test_nba_model','test_nba_model_warmup','test_nba_readiness','test_nba_markets_review'],(),False),
      **{f'historical-538-{lag}':(cmd('model','backtest','--archive',str(WORK/'nba-review-repair/score/nbaallelo-pinned.csv'),'--first-season','2005','--last-season','2014','--replicates','10000','--lag-days',str(lag)),) for lag in (1,2)} }
    with ThreadPoolExecutor(max_workers=4) as ex:
        pending={k:ex.submit(task,k,*params) for k,params in configs.items()};allr={k:v.result() for k,v in pending.items()}
    pl=allr['players'];assert all(v['verified']==(s in ('normal','observed_off_day')) for s,items in pl.items() for v in items.values()),pl
    assert all(v['enough_days']==(s=='normal') and v['paired_days']==(3 if s=='normal' else 0) for s,v in allr['schedule'].items()),allr['schedule']
    assert not allr['legacy-pair']['enough_days'] and allr['legacy-pair']['paired_days']==0
    assert allr['mixed']['complete'] is False and not allr['mixed']['schedule_day_success']
    assert all(v=='schedule_unverified' for v in allr['mixed']['probes'].values())
    assert allr['acceptance-clock']['on_time']['verified'] and not allr['acceptance-clock']['after_both_tips']['verified']
    assert allr['future']['same_training_hash'] and allr['future']['future_label']!='final'
    assert allr['upgrade']['as_read_by_a2']['eligible_finals']==0 and allr['upgrade']['after_rescan']['unchanged_asof']
    for s in ('positive','off','gap'):
        v=allr['forward-'+s];assert v['calendar_qualified']==(s!='gap') and v['scored']==14 and not v['phase_two_activated'] and v['training_games']==list(range(1230,1244)),v
        if s!='gap':assert v['totals']['warm_sealed']==v['totals']['paired_win']==v['totals']['due']==14
    ar=allr['archive'];assert ar['verified_bytes']==180 and ar['adjudicated']['checked_revision_refs']==205 and ar['adjudicated']['cross_538_irw_games']==3540 and not ar['adjudicated']['different_from_538']
    assert all(v['identical'] and v['n']==6150 for v in ar['replay'].values())
    assert all(v['early_changed']==0 and v['late_changed']==v['late_total']>0 for v in allr['replay-clock']['lags'].values())
    assert all(a==b for a,b in zip([v[0] for v in allr['replay-clock']['venues'].values()],[False,True,True,False,False]))
    assert not any(allr['empty-forward']['gates'].values())
    assert not allr['no-keys-schedule']['verified'] and allr['no-keys-schedule']['state']=='missing_key'
    assert not allr['no-keys-modern']['complete'] and not allr['no-keys-warmup']['eligible']
    assert all(v['authenticated_business_receipts']==0 for v in allr['no-keys']['sources'].values())
    for lag in (1,2):assert (out/f'historical-538-{lag}.json').read_bytes()==(root/f'reports/nba-phase1/v2-538-lag{24*lag}.json').read_bytes()
    baseline=git('rev-parse','36faed3^{commit}');base_py=HERE.parent/'base/src/polymarket861/nba'
    for f in ('data.py','operations.py','markets.py'):
        obj=subprocess.check_output(['git','-C',str(REPO),'show',f'{baseline}:src/polymarket861/nba/{f}']); assert obj==(base_py/f).read_bytes()
    summary={'result':'PASS','commit':sha,'tree':git('rev-parse',f'{sha}^{{tree}}'),'src_tree':git('rev-parse',f'{sha}:src'),
      'nba_src_tree':git('rev-parse',f'{sha}:src/polymarket861/nba'),'baseline':baseline,
      'commands':{k:' '.join(('python',*map(str,v[0]))) for k,v in configs.items()}, 'tests':allr['repository-data-tests'].strip(),
      'provider_receipts':{s:v['authenticated_business_receipts'] for s,v in allr['no-keys']['sources'].items()},
      'artifacts':{k:hashlib.sha256((out/(k+('.txt' if k=='repository-data-tests' else '.json'))).read_bytes()).hexdigest() for k in configs},
      'independent_verification_driver_sha':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (out/'summary.json').write_text(json.dumps(summary,indent=2)+'\n');print(json.dumps({k:v for k,v in summary.items() if k in ('result','commit','tree','src_tree','nba_src_tree','tests','provider_receipts')},indent=2))
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('commit');main(p.parse_args().commit)
