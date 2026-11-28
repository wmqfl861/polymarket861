import json,os,sys,subprocess,tempfile
from pathlib import Path
from datetime import timedelta
from data_audit import FIRST,TEST_KEY, Clock,scan,row,catalogue

def main():
    os.environ['BALLDONTLIE_API_KEY']=TEST_KEY
    dates=[FIRST+timedelta(days=i) for i in range(3)]
    if len(sys.argv)>1:
        clock=Clock();clock.set(FIRST-timedelta(days=1),19)
        output=scan(Path(sys.argv[1]),clock,dates[0],{'regular':[row(d) for d in dates]},dates[-1]);assert output['complete'],output
        return
    from polymarket861.nba import markets_schedule,data
    with tempfile.TemporaryDirectory() as work:
        root=Path(work)/'nba'
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'base/src'))
        subprocess.run([sys.executable,__file__,str(root)],env=env,check=True)
        clock=Clock()
        for day in dates:
            clock.set(day,4,30)
            catalogue(root,clock,dates)
        clock.set(dates[-1]+timedelta(days=1),12)
        a2_games=data.games_as_of(root,clock());report=markets_schedule.kickoff_differences(root,as_of=clock())
        print(json.dumps({'stages':[g['stage_basis'] for g in a2_games],'paired_days':report['paired_days'],'counts':report['counts'],'enough_days':report['enough_days'],'receipts_per_day':[(d['date_et'],d['score_scan_sha256'],d['complete_day']) for d in report['days']]},indent=2))
if __name__=='__main__':main()
