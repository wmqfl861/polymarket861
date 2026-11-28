import json,os,tempfile
from datetime import date,timedelta
from pathlib import Path
from data_audit import TEST_KEY,Clock,row,scan,data
from polymarket861.nba import source_acceptance
import httpx

def run(root,retrospective):
    clock=Clock();anchor=date(2026,11,26);clock.set(anchor,14)
    moved=anchor-timedelta(days=4) if retrospective else anchor
    original=row(moved,datetime=f'{moved}T21:15:00Z')
    assert scan(root,clock,moved,{'regular':[original]})['complete']
    clock.advance(120)
    f1=row(anchor-timedelta(days=2),status='Final',status_state='final',period=4,home_team_score=103,visitor_team_score=112)
    f2=row(anchor-timedelta(days=1),status='Final',status_state='final',period=4,home_team_score=103,visitor_team_score=112)
    current=row(moved,datetime=f'{moved}T23:00:00Z');post=row(anchor-timedelta(days=6),postponed=True)
    live=row(anchor+timedelta(days=1))
    def fetch(params,key):
        if params['start_date']=='2026-10-01':
            pool={'regular':[row(date(2026,10,22))], 'preseason':[row(date(2026,10,5))]}
        elif params['start_date']=='2025-12-01':
            pool={'ist':[row(date(2025,12,19),season=2025,ist_stage='Championship')]}
        else:
            pool={'regular':[current,post,f1,f2,live]}
        return httpx.Response(200,json={'data':pool.get(params['season_type'],[]),'meta':{'next_cursor':None}})
    r=source_acceptance.verify_schedule(root,through=anchor,fetch=fetch,clock=clock,sleep=clock.advance)
    return {'verified':r['verified'],'state':r['state'],'observed_at':r['observed_at'], 'check':r['checks']['reschedule_observed'],'source_state':r['ranges'][0]['state']}
if __name__=='__main__':
    os.environ['BALLDONTLIE_API_KEY']=TEST_KEY
    with tempfile.TemporaryDirectory() as temp:
        base=Path(temp);print(json.dumps({k:run(base/k,v) for k,v in [('on_time',False),('after_both_tips',True)]},indent=2))
