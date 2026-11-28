"""Build actual candidate/legacy wheels, invoke shipped service command lines in isolated directories, verify bytes."""
import argparse
from contextlib import ExitStack
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from zoneinfo import ZoneInfo

p=argparse.ArgumentParser();p.add_argument('source',type=Path);p.add_argument('gitrepo',type=Path);p.add_argument('--work',type=Path,required=True);a=p.parse_args();src=a.source.resolve();git=a.gitrepo.resolve();work=a.work.resolve();os.environ['PYTHONDONTWRITEBYTECODE']='1';base_env={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','BALLDONTLIE_API_KEY','BIGBALLS_API_KEY','DUNKAPI_API_KEY')};base_env['PIP_NO_INDEX']='1';base_env['PIP_DISABLE_PIP_VERSION_CHECK']='1'
def run(cmd,cwd,env=base_env,expect=0):
    r=subprocess.run(cmd,cwd=cwd,env=env,text=True,capture_output=True,timeout=120)
    if expect is not None and r.returncode!=expect: raise RuntimeError((list(map(str,cmd)),r.returncode,r.stdout[-1500:],r.stderr[-800:]))
    return r

def fp(path): return {str(f.relative_to(path)):hashlib.sha256(f.read_bytes()).hexdigest() for f in path.rglob('*') if f.is_file()}
def build(source,folder):
    folder.mkdir(); dist=folder/'wheels';dist.mkdir();run([sys.executable,'-m','pip','wheel','--no-build-isolation','--no-deps','--no-cache-dir','-w',dist,source],cwd=folder)
    wheel,=dist.glob('*.whl');v=folder/'venv';run([sys.executable,'-m','venv','--system-site-packages',v],cwd=folder);run([v/'bin/python','-I','-m','pip','install','--no-deps','--no-index','--force-reinstall',wheel],cwd=folder)
    return v/'bin/pm861',{'wheel':wheel.name,'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),'version':run([v/'bin/pm861','--version'],cwd=folder).stdout.strip(),'interpreter_import':run([v/'bin/python','-I','-c','import importlib.metadata, pathlib, sys, polymarket861; p=pathlib.Path(polymarket861.__file__).resolve(); assert p.is_relative_to(pathlib.Path(sys.prefix)); print(importlib.metadata.version("polymarket861"))'],cwd=folder).stdout.strip()}

with tempfile.TemporaryDirectory(dir=work,prefix='wheels-') as tmp:
    t=Path(tmp); source=t/'candidate-src';source.mkdir();shutil.copytree(src/'src'/'polymarket861',source/'src'/'polymarket861',ignore=shutil.ignore_patterns('__pycache__'));[shutil.copy2(src/f,source/f) for f in ('README.md','pyproject.toml')]; new,ne=build(source,t/'new'); oldsrc=t/'released-src';oldsrc.mkdir();tar=t/'old.tar';run(['git','-C',git,'archive','-o',tar,'60c1a2d'],cwd=t)
    with tarfile.open(tar) as tt:tt.extractall(oldsrc,filter='data')
    old,ole=build(oldsrc,t/'old'); nba=t/'nba';football=t/'football'; fix=t/'fixture';fix.mkdir();(fix/'sitecustomize.py').write_text("import os,runpy,httpx\nfrom pathlib import Path\nrunpy.run_path(str(Path(os.environ['GITHUB_WORKSPACE'])/'.github/fixtures/nba-service/sitecustomize.py'))\ndef serve(*a,**kw): return httpx.Response(200,json={'data':[],'meta':{'next_cursor':None}})\nhttpx.get=serve\nhttpx.post=serve\n")
    tip=(datetime.datetime.now(datetime.UTC)+datetime.timedelta(minutes=63)).replace(second=0,microsecond=0); day=tip.astimezone(ZoneInfo('America/New_York')).date()
    env={**base_env,'GITHUB_WORKSPACE':str(src),'PYTHONPATH':str(fix),'BALLDONTLIE_API_KEY':'synthetic-local-only','PM861_CI_NBA_TIP':tip.isoformat(),'PM861_NBA_WATCH_COMPONENT':'all'}
    health=run([new,'--data-dir',nba,'nba','watchdog'],cwd=t,expect=1); assert json.loads(health.stdout)['state']!='ok'
    services={}
    for name in ('markets','schedule','forecast','watchdog','sources'):
        unit=src/'deploy'/f'polymarket861-nba-{name}.service'; words=shlex.split(next(l.removeprefix('ExecStart=') for l in unit.read_text().splitlines() if l.startswith('ExecStart=')));assert words[:3]==['%h/.local/share/polymarket861-nba/venv/bin/pm861','--data-dir','%h/.local/share/polymarket861-nba/state']; args=[s.replace('${PM861_NBA_WATCH_COMPONENT}','all') for s in words[3:]]
        out=run([new,'--data-dir',nba,*args],cwd=t,env=env);r=json.loads(out.stdout);services[name]={'exit':out.returncode,'args':args,'state':r.get('state'),'component':r.get('component')};assert not r.get('paper_trading',False)
    fixtures=json.loads(run([new,'--data-dir',nba,'nba','fixtures','--date',str(day)],cwd=t).stdout); assert [g['game_id'] for g in fixtures['games']]==['bdl:32']; pred=json.loads(run([new,'--data-dir',nba,'nba','forecast-show','bdl:32'],cwd=t).stdout);w=next(w for w in pred['windows'] if w['horizon']=='T-60m'); assert w['state']=='sealed'; ref=sorted(w['prediction']['market_at_prediction']); assert ref==['away_total','home_total','total','win'];ready=json.loads(run([new,'--data-dir',nba,'nba','readiness'],cwd=t).stdout);assert ready['phase_two_activated'] is False
    baseline=fp(nba);guards={}
    for name,exe in [('new',new),('released',old)]:
        for args in [('jobs','status'),('fixture','list')]:
            result=run([exe,'--data-dir',nba,*args],cwd=t,expect=None);guards[name+' '+' '.join(args)]={'exit':result.returncode,'same':fp(nba)==baseline};assert result.returncode and fp(nba)==baseline
    run([new,'--data-dir',football,'fixture','list'],cwd=t); ff=fp(football); r=run([new,'--data-dir',football,'nba','markets','collect'],cwd=t,env=env,expect=None);guards['nba on football']={'exit':r.returncode,'same':fp(football)==ff};assert r.returncode and fp(football)==ff
    state=run(['systemctl','--user','is-system-running'],cwd=t,expect=None);print(json.dumps({'installed':ne,'released':ole,'service_commands':services,'fixture':fixtures['games'][0]['game_id'],'sealed_families':ref,'models':list(w['prediction']['forecast']['models']),'ready':ready['state'],'guards':guards,'user_systemd':{'exit':state.returncode,'output':state.stdout.strip(),'error':state.stderr.strip(),'cgroup_present':Path('/sys/fs/cgroup').exists()}},indent=2,ensure_ascii=False))
