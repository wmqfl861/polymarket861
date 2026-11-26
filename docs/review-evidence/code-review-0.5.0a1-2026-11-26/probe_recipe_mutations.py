#!/usr/bin/env python3
"""改动试验：改哪些代码会让已批准的配方失效、要求重新批准，改哪些不会。

每一组把源码树整份复制到临时目录，只改一处文字，再把一份“已经批准 0.5.0a1 配方”的
临时数据目录复制一份，用改过的源码在上面运行：
  1. `season readiness`：发布状态是 ready 还是 approval_required；
  2. `forecast make <比赛> --horizon early`：能否封存，封存出的概率、配方摘要、新闻配方摘要。
可选地再运行 `leagues baseline-study`，看五国回测报告的摘要有没有变。

仓库里的源码和真实数据目录都不会被触碰。

用法：
  python -I probe_recipe_mutations.py --src <仓库>/src --data <已批准 0.5.0a1 的临时数据目录> \
      --python <装好依赖的 Python 3.12> --scratch <空目录> [--history <固定的 OpenFootball 目录>]

--data 目录下要有 datasets/football-inputs/*/sources（由 upgrade_replication.sh 生成的目录即可）。
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--data", required=True)
parser.add_argument("--python", required=True)
parser.add_argument("--scratch", required=True)
parser.add_argument("--history")
parser.add_argument("--fixture", default="epl/2026-27/crystal-palace/hull-city")
args = parser.parse_args()

# （组名，文件，原文，改成，说明）。原文必须在文件里恰好出现一次。
MUTATIONS = [
    ("对照：不改", None, None, None, "基准"),
    (
        "配方内：岭惩罚 8.0 改 8.5",
        "football_model.py",
        "RIDGE = 8.0\n",
        "RIDGE = 8.5\n",
        "球队强度拟合的超参数，被 fit_team_model 按名字引用",
    ),
    (
        "配方内：只加一行注释",
        "football_model.py",
        "RIDGE = 8.0\n",
        "RIDGE = 8.0\n# 审核试验：只加注释，不改语法树\n",
        "语法树不变",
    ),
    (
        "配方外：看板文字",
        "dashboard.py",
        None,  # 运行时在文件末尾追加一行注释之外的真实语句
        None,
        "看板模块不参与概率计算",
    ),
    (
        "配方外：联赛登记表删掉一条别名",
        "league_registry.py",
        None,  # 运行时删掉西甲的一条写法对照
        None,
        "football.py 在显式联赛路径上调用 league_registry，但这个文件不在配方清单里",
    ),
    (
        "配方外：新闻调整上限 0.12 改 0.13",
        "news_model.py",
        None,  # 运行时定位
        None,
        "新闻对照臂的策略常数；主臂 B1 不用它",
    ),
]


def run(source, data, *command):
    env = {"PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"}
    return subprocess.run(
        [args.python, "-m", "polymarket861", "--data-dir", str(data), *command],
        env=env,
        capture_output=True,
        text=True,
    )


def mutate(path, name):
    text = path.read_text(encoding="utf-8")
    if name.startswith("配方外：看板文字"):
        changed = text + '\nREVIEW_PROBE_LABEL = "审核试验：看板模块多一个常量"\n'
        return changed, "文件末尾增加一个模块级常量"
    if name.startswith("配方外：联赛登记表"):
        marker = '"RC Celta": "RC Celta de Vigo", '
        if text.count(marker) != 1:
            raise SystemExit("league_registry.py 里没有找到唯一的 " + marker)
        return text.replace(marker, ""), "删掉西甲的一条写法对照：" + marker.strip()
    if name.startswith("配方外：新闻调整上限"):
        marker = '"max_abs_log_adjustment_per_team": 0.12'
        if text.count(marker) != 1:
            raise SystemExit("news_model.py 里没有找到唯一的 " + marker)
        return text.replace(marker, '"max_abs_log_adjustment_per_team": 0.13'), marker + " 改为 0.13"
    raise AssertionError(name)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


scratch = Path(args.scratch)
results = []
for index, (name, filename, old, new, why) in enumerate(MUTATIONS):
    work = scratch / f"arm{index}"
    source = work / "src"
    shutil.copytree(args.src, source, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
    data = work / "data"
    shutil.copytree(args.data, data)
    detail = "不改"
    if filename:
        target = source / "polymarket861" / filename
        if old is None:
            changed, detail = mutate(target, name)
        else:
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                raise SystemExit(f"{filename}: 原文出现 {text.count(old)} 次，应当恰好 1 次")
            changed, detail = text.replace(old, new), f"{old.strip()} 改为 {new.strip()}"
        target.write_text(changed, encoding="utf-8")
    inputs = next((data / "datasets" / "football-inputs").glob("*/sources"))
    ready = run(source, data, "season", "readiness", "--year", "2026", "--round", "12")
    release = json.loads(ready.stdout)["release"] if ready.returncode == 0 else {"error": ready.stderr.strip()[-300:]}
    seal = run(source, data, "forecast", "make", args.fixture, str(inputs), "--horizon", "early")
    row = {
        "arm": name,
        "file": filename,
        "change": detail,
        "why": why,
        "release_state": release.get("state"),
        "release_ready": release.get("ready"),
        "forecast_make_exit": seal.returncode,
    }
    if seal.returncode == 0:
        body = json.loads(seal.stdout)
        row.update(
            recipe_digest=body["recipe_digest"],
            news_recipe_sha256=body.get("news_recipe_sha256"),
            release_provenance_sha256=body.get("release_provenance_sha256"),
            probabilities=body["probabilities"],
            with_news_probabilities=body.get("news_pair", {}).get("with_news", {}).get("probabilities"),
        )
    else:
        row["forecast_make_message"] = seal.stderr.strip().splitlines()[-1][:300] if seal.stderr.strip() else None
    if args.history:
        report = work / "baseline-study.json"
        study = subprocess.run(
            [args.python, "-m", "polymarket861", "leagues", "baseline-study", args.history, "--output", str(report)],
            env={"PYTHONPATH": str(source), "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1"},
            capture_output=True,
            text=True,
        )
        row["baseline_study_exit"] = study.returncode
        if study.returncode == 0 and report.exists():
            row["baseline_study_sha256"] = sha(report)
        else:
            row["baseline_study_message"] = (study.stderr.strip().splitlines() or [""])[-1][:300]
    results.append(row)

base = results[0]
for row in results[1:]:
    row["same_recipe_as_control"] = row.get("recipe_digest") == base.get("recipe_digest") if "recipe_digest" in row else None
    row["same_probabilities_as_control"] = row.get("probabilities") == base.get("probabilities") if "probabilities" in row else None
    row["same_news_recipe_as_control"] = row.get("news_recipe_sha256") == base.get("news_recipe_sha256") if "news_recipe_sha256" in row else None
    row["same_provenance_as_control"] = (
        row.get("release_provenance_sha256") == base.get("release_provenance_sha256") if "release_provenance_sha256" in row else None
    )
    if "baseline_study_sha256" in row or "baseline_study_exit" in row:
        row["same_baseline_study_as_control"] = row.get("baseline_study_sha256") == base.get("baseline_study_sha256")
json.dump({"fixture": args.fixture, "arms": results}, sys.stdout, ensure_ascii=False, indent=1)
print()
