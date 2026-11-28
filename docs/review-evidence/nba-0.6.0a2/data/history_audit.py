"""Independent model, archive and upgrade checks. All provider I/O is synthetic except archive bytes."""
import argparse
import collections
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from data_audit import FIRST, TEST_KEY, Clock, data, catalogue, row, scan


def finals(base, n):
    games=[]
    for i in range(n):
        day=base+timedelta(days=i)
        games.append(row(day, status='Final', status_state='final', period=4,
                         home_team_score=97+(i*7)%32, visitor_team_score=88+(i*9)%24, datetime=None, season=2026))
        if games[-1]['home_team_score']==games[-1]['visitor_team_score']:
            games[-1]['home_team_score']+=1
    return games


def future_leak(root):
    from polymarket861.nba import model
    clock=Clock();d=date(2026,11,27);clock.set(d,12)
    elapsed=finals(date(2026,10,21),35)
    target=d+timedelta(days=2)
    valid = scan(root,clock,elapsed[0]['date'],{'regular':elapsed+[row(target)]},target)
    assert valid['complete'],valid
    args=SimpleNamespace(data_dir=root, game_id=f'bdl:{target.toordinal()}', as_of=data._iso(clock()),
                         total_line=None,home_line=None,away_line=None)
    preview=model._predict_command(args,clock=clock)
    clock.advance(120)
    future=d+timedelta(days=1)
    bad=finals(future,1)[0]
    r=scan(root,clock,future,{'regular':[bad]})
    args.as_of=data._iso(clock())
    with_future=model._predict_command(args,clock=clock)
    g=data.games_as_of(root,clock())
    suspect=next(x for x in g if x['game_id']==f'bdl:{future.toordinal()}')
    return {'reported_future_game_date':str(future),'current_et_date':str(clock().astimezone(data.EASTERN).date()),
            'future_label':suspect['status'],'scan':r['state'],'before_training_games':preview['training_games'],
            'after_training_games':with_future['training_games'], 'before_n2':preview['models']['N2']['p_home_win'],
            'after_n2':with_future['models']['N2']['p_home_win'],
            'same_training_hash':preview['training_view_sha256']==with_future['training_view_sha256']}


def signature(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def archive(repo,data_root,dest):
    from polymarket861.nba import model_replay
    script=repo/'scripts/nba_wikimedia_archive.py';source=repo/'reports/nba-phase1/modern-archive'
    (dest/'rebuilt').mkdir(exist_ok=True)
    child=subprocess.run([sys.executable,str(script),'build','--cache',str(data_root/'wiki'),'--directory',str(dest/'rebuilt'),'--pins',str(source/'wikipedia-revisions.json')],capture_output=True,text=True,check=True)
    pins=json.loads((source/'wikipedia-revisions.json').read_text())
    verified=sum(hashlib.sha256(json.loads((data_root/'wiki'/f"{r['year']}-{r['team']}.json").read_text())['content'].encode()).hexdigest()==r['sha256'] for r in pins)
    with (source/'agreed-finals.csv').open() as f:
        rows=list(csv.DictReader(f))
    teams=Counter((r['date'],t) for r in rows for t in (r['home'],r['away']))
    counts=Counter(r['season'] for r in rows)
    clean=all(int(r['home_score'])>0 and int(r['away_score'])>0 and r['home_score']!=r['away_score'] and r['home']!=r['away'] for r in rows)
    import shutil
    cross=source/'crosschecks'
    shutil.copytree(cross,dest/'rebuilt'/'crosschecks',dirs_exist_ok=True)
    refs=json.loads((cross/'revision-pins.json').read_text());proofs=json.loads((cross/'proofs.json').read_text())
    cross_verified=0
    for r in refs:
        raw=data_root/('archive-cross/frwiki' if r['lang']=='fr' else 'wiki')/f"{r['year']}-{r['team']}.json"
        doc=json.loads(raw.read_text());real_rev=doc.get('revid',doc.get('revision'))
        cross_verified+=real_rev==r['revision'] and hashlib.sha256(doc['content'].encode()).hexdigest()==r['sha256']
    def score_key(r):return (int(r['season']),r['date'],*sorted((r['home'],r['away'])))
    def score(r):return r['home'],r['away'],int(r['home_score']),int(r['away_score'])
    archive={score_key(r):score(r) for r in rows}
    with (cross/'withdraw.csv').open() as f:withdrawn=list(csv.DictReader(f))
    for r in withdrawn:assert archive.pop(score_key(r))==score(r)
    with (cross/'resolved.csv').open() as f:changes=list(csv.DictReader(f))
    assert len({score_key(r) for r in changes})==len(changes)
    related={(r['lang'],str(r['revision'])) for r in refs}
    assert all(p=='538' or tuple(p.split(':')) in related for g in proofs for p in g['proofs'])
    assert sorted(r['game'] for r in proofs)==sorted('/'.join((r['season'],r['date'],r['home'],r['away'])) for r in changes)
    replaced=sum(score_key(r) in archive for r in changes)
    for r in changes:archive[score_key(r)]=score(r)
    csv_rep='\n'.join(','.join(map(str,(k[0],k[1],*pair))) for k,pair in sorted(archive.items()))
    score_sha=hashlib.sha256(csv_rep.encode()).hexdigest()
    balance=Counter((k[0],t) for k in archive for t in k[2:]);slots=Counter((k[1],t) for k in archive for t in k[2:]);opps=collections.defaultdict(set)
    for k in archive:opps[k[0]].add(k[2:])
    assert len(balance)==180 and max(slots.values())==1
    assert all(n==(72 if y==2020 else 82) for (y,t),n in balance.items())
    all538={};alias={'CHO':'CHA','BRK':'BKN','PHO':'PHX'}
    with (data_root/'archive-cross'/'irw-scores.csv').open() as f:
        for r in csv.DictReader(f):
            y=int(r['season'])-1
            if 2020<=y<=2022 and not r['playoff']:
                hm,aw=(alias.get(r[k],r[k]) for k in ('agent_a','agent_b'))
                d=datetime.fromtimestamp(int(r['date']),timezone.utc).date().isoformat();key=(y,d,*sorted((hm,aw)))
                assert key not in all538
                all538[key]=(hm,aw,int(r['score_a']),int(r['score_b']))
    diffs=[(key,pair,archive.get(key)) for key,pair in all538.items() if archive.get(key)!=pair]
    replay={}
    for lag in (1,2):
        subprocess.run([sys.executable,str(script),'study','--directory',str(dest/'rebuilt'),'--lag-days',str(lag)],capture_output=True,text=True,check=True)
        p=source/f'study-{lag*24}h.json';o=json.loads(p.read_text());assert score_sha==o['archive']['sha256']
        replay[lag]={'identical':p.read_bytes()==(dest/'rebuilt'/p.name).read_bytes(),
                     'n':o['pooled']['games'],'N2':o['pooled']['losses']['N2'],
                     'paired':o['pooled']['paired']['N2_minus_N1']['brier_win']}
    return {'pins':len(pins),'verified_bytes':verified,'latest_revision':max(p['timestamp'] for p in pins),
            'partial_agreed_finals':len(rows),'distinct_team_day':len(teams),'max_team_on_day':max(teams.values()),
            'positive_scores_distinct_teams':clean,'by_season':counts,'absent_eval':6150-sum(n for y,n in counts.items() if y!='2020'),
            'absent_warmup':1080-counts['2020'],
            'rebuilt_identical':{name:signature(source/name)==signature(dest/'rebuilt'/name) for name in ('agreed-finals.csv','withheld.json','wikipedia-revisions.json')},
            'adjudicated':{'resolved':len(changes),'replaced_existing':replaced,'withdrawn':len(withdrawn),'final_counts':Counter(k[0] for k in archive),'max_team_on_day':max(slots.values()),'teams':len(balance),'opponents':{k:len(v) for k,v in opps.items()},'sha256':score_sha,'checked_revision_refs':cross_verified,'revision_ref_count':len(refs),'matched_correction_proofs':len(proofs),'cross_538_irw_games':len(all538),'different_from_538':diffs,'raw_538_sha256':signature(data_root/'archive-cross'/'irw-scores.csv')},'replay':replay}


def legacy_generate(root):
    clock=Clock();d=date(2026,11,20);clock.set(d,12)
    data_sample=finals(date(2026,10,15),30)
    r=scan(root,clock,data_sample[0]['date'],{'regular':data_sample+[row(d)]},d)
    assert r['complete'],r
    from polymarket861.nba import operations
    operations.forecast_cycle(root,clock=clock)
    print(json.dumps({'legacy_rows':len(data.games_as_of(root)), 'complete':r['complete']}))


def upgrade(repo,data_root,dest):
    from polymarket861.nba import model, warmup, readiness, source_probes, markets_schedule, operations
    with tempfile.TemporaryDirectory() as ddir:
        root=Path(ddir)/'nba';env=dict(os.environ,PYTHONPATH=str(data_root/'base'/'src'),PYTHONDONTWRITEBYTECODE='1')
        prior=subprocess.run([sys.executable,__file__,'--run','legacy-generate','--root',str(root)],env=env,text=True,capture_output=True,check=True)
        legacy=json.loads(prior.stdout)
        d=date(2026,11,20);clock=Clock();clock.set(d,15)
        now=data.games_as_of(root,clock());games=len(now)
        before={'rows':games,'sources':dict(Counter(r['stage_basis'] for r in now)), 'eligible_finals':sum(model.eligible(r) for r in now),
                'source_gate':source_probes.player_summary(root,clock())['bigballs']['source_verified'],
                'kickoff_days':markets_schedule.kickoff_differences(root,as_of=clock())['paired_days']}
        # No BDL rescans, only forward market evidence. Prior guessed regulars must not certify a paired day.
        # An _old_ read-only database and its old seals are intentionally retained across versions.
        d2=d+timedelta(days=1)
        ready=readiness.readiness(root,d,now=datetime.combine(d2,datetime.min.time(),timezone.utc)+timedelta(hours=12));
        before['forward_eligible']=ready['calendar']['qualified'];before['calendar_success']=ready['calendar']['days'][-1]['schedule_scan_succeeded_on_date'] if ready['calendar']['days'] else None
        clock.set(d,16);updated=scan(root,clock,d,{'regular':[row(d)]});cur=data.games_as_of(root,clock());again=data.games_as_of(root,datetime.combine(d,datetime.min.time(),timezone.utc)+timedelta(hours=15));
        return {'old':legacy,'as_read_by_a2':before,'after_rescan':{'new_versions':updated['new_versions'],
                'confirmed':sum(r.get('stage_basis')=='disjoint_phase_queries' for r in cur),
                'unchanged_asof':now==again,'eligible_finals':sum(model.eligible(r) for r in cur)}}


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--run',choices=['archive','future','upgrade','legacy-generate'],required=True);p.add_argument('--root',type=Path);p.add_argument('--repo',type=Path,default=Path(__file__).resolve().parents[2]/'polymarket861');args=p.parse_args()
    os.environ['BALLDONTLIE_API_KEY']=TEST_KEY
    dest=Path(__file__).resolve().parent;state=dest.parent
    if args.run=='archive':out=archive(args.repo.resolve(),state,dest)
    elif args.run=='upgrade':out=upgrade(args.repo.resolve(),state,dest)
    elif args.run=='legacy-generate':legacy_generate(args.root);sys.exit()
    else:
        with tempfile.TemporaryDirectory() as t:out=future_leak(Path(t)/'nba')
    print(json.dumps(out,indent=2,ensure_ascii=False))
