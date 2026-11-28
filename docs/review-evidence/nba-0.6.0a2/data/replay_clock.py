"""Mutate all future finals in the complete modern archive; compare original model forecasts, not scores."""
import argparse,copy,hashlib,importlib.util,json
from datetime import date,timedelta
from pathlib import Path
from unittest.mock import patch
from polymarket861.nba import model,model_replay,venue


def run(root):
    location=root/'scripts/nba_wikimedia_archive.py';spec=importlib.util.spec_from_file_location('nba_archive_worker',location)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    rows,sha=module.verified_finals(root/'reports/nba-phase1/modern-archive')
    pivot=date(2026,1,20)
    changed=[dict(g,home_score=g['away_score'],away_score=g['home_score']) if model.day_of(g)>=pivot else g for g in rows]
    def observations(arr,lag):
        seen={};original=model.forecast
        def record(state,game,*args,**kwargs):
            out=original(state,game,*args,**kwargs)
            track=json.dumps(out['models'],separators=(',',':'),sort_keys=True).encode()
            seen[game['game_id']]=(game['game_date'],hashlib.sha256(track).digest());return out
        with patch.object(model,'forecast',record):model_replay.backtest(arr,first_season=2025,last_season=2025,lag_days=lag,replicates=100)
        return seen
    counts={}
    for lag in (1,2):
        a,b=observations(rows,lag),observations(changed,lag);assert a.keys()==b.keys()
        early=[k for k,v in a.items() if v[0]<=str(pivot+timedelta(days=lag))]
        late=set(a)-set(early)
        counts[lag]={'season_rows':len(a),'early_total':len(early),'early_changed':sum(a[k]!=b[k] for k in early),'late_total':len(late),'late_changed':sum(a[k]!=b[k] for k in late)}
    def neutral(proof_day,game_day,h,a):
        g={'home':'bdl:20','away':'bdl:3','home_abbr':h,'away_abbr':a,'game_date':game_day,'stage':'regular','neutral':None}
        return list(venue.resolve(g,model.instant(proof_day)))[:2]
    venues={
        'within_publication_day':neutral('2025-12-12T01:00:00Z','2025-12-13','ORL','NY'),
        'next_eastern_day':neutral('2025-12-12T05:00:01Z','2025-12-13','ORL','NY'),
        'canonical':neutral('2025-12-13T18:00:00Z','2025-12-13','ORL','NYK'),
        'official_flag_about_other_game':neutral('2025-12-13T18:00:00Z','2025-12-13','MIA','NYK'),
        'rescheduled_date':neutral('2025-12-13T18:00:00Z','2025-12-14','ORL','NY')}
    return {'pivot':str(pivot),'modern_final_sha':sha,'lags':counts,'venues':venues}
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('repo',type=Path);args=p.parse_args();print(json.dumps(run(args.repo),indent=2))
