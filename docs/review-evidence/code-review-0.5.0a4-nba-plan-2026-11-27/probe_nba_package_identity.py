#!/usr/bin/env python3
"""NBA 计划 INTEGRATION.md 打算把篮球代码放进同一个安装包的 src/polymarket861/nba/。
这个探针回答：这样做会不会动到足球已经签批的配方，哪一种改法会动到。

做法：把 --src 指向的源码复制到临时目录，做下面几种改动，每种都在一个新的解释器里重新计算
  * B0–B3 四个足球配方的主摘要和配对摘要（英超正式封存只认这个，变了就要重新比较 1,900 场并具名批准）；
  * 整个安装包的文件清单摘要（package_files）；
  * “选单实现”摘要（model_study.decision_implementation，strategy 预登记的试验用它判断实现有没有变）。

几种改动：
  baseline            不改；
  add_nba_package     只新增 polymarket861/nba/__init__.py 和 nba/model.py 两个占位文件；
  add_unused_helper   在上一步基础上，给 market_http.py 末尾加一个没人调用的新函数；
  edit_shared_read    在第二步基础上，在 market_http.read 的函数体开头加一条不改变行为的语句；
  edit_store_connect  在第二步基础上，在 store.connect 的函数体开头加同样一条语句（计划提到要防止 NBA 命令误开足球库，
                      这里看把检查加在 store.connect 里会不会动到足球配方）；
  edit_store_digest   在第二步基础上，在 store.digest 的函数体开头加同样一条语句（计划点名复用的 store.iso、instant、
                      canonical、digest 之一）。

market_http 是计划点名要复用的三个模块之一（另两个是 store 和 blob_storage），三个都在英超 B1 配方覆盖的
模块里。输出里同时列出 B1 配方覆盖的全部模块名。

用法：
  python -I probe_nba_package_identity.py --src <源码 src 目录>

不联网，不写仓库，只写自己建的临时目录。
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
args = parser.parse_args()

CHILD = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from polymarket861 import football, model_study, pipeline, store
recipes = {}
modules = None
for family in pipeline.FAMILIES:
    primary = pipeline._build_recipe(family, football.DEFAULT_LINES)
    paired = pipeline._study_recipe(primary)
    recipes[family] = {"primary": store.digest(primary), "paired": store.digest(paired)}
    if family == "B1":
        modules = sorted(name[:-3] for name in primary["algorithm_source"])
files = model_study.release_provenance()["package_files"]
decision = model_study.decision_implementation()
print(json.dumps({
    "recipes": recipes,
    "b1_recipe_modules": modules,
    "package_files": len(files),
    "package_files_sha256": store.digest(files),
    "decision_modules": len(decision["algorithm_source"]),
    "decision_implementation_sha256": store.digest(decision),
}))
"""


def measure(tree):
    done = subprocess.run(
        [sys.executable, "-I", "-c", CHILD, str(tree)], capture_output=True, text=True, check=True
    )
    return json.loads(done.stdout)


def add_nba_package(package):
    (package / "nba").mkdir()
    (package / "nba" / "__init__.py").write_text('"""Placeholder NBA subpackage."""\n', encoding="utf-8")
    (package / "nba" / "model.py").write_text('"""Placeholder NBA score model."""\n\nMODEL = "N1"\n', encoding="utf-8")


def add_unused_helper(package):
    path = package / "market_http.py"
    path.write_text(
        path.read_text(encoding="utf-8") + "\n\ndef nba_probe_unused():\n    return None\n", encoding="utf-8"
    )


def edit_shared_read(package):
    path = package / "market_http.py"
    lines = path.read_text(encoding="utf-8").split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith("def read("))
    lines.insert(at + 1, "    nba_probe_statement = None")
    path.write_text("\n".join(lines), encoding="utf-8")


def insert_after(package, module, signature):
    path = package / module
    lines = path.read_text(encoding="utf-8").split("\n")
    at = next(i for i, line in enumerate(lines) if line.startswith(signature))
    lines.insert(at + 1, "    nba_probe_statement = None")
    path.write_text("\n".join(lines), encoding="utf-8")


def edit_store_connect(package):
    insert_after(package, "store.py", "def connect(")


def edit_store_digest(package):
    insert_after(package, "store.py", "def digest(")


VARIANTS = {
    "baseline": [],
    "add_nba_package": [add_nba_package],
    "add_unused_helper": [add_nba_package, add_unused_helper],
    "edit_shared_read": [add_nba_package, edit_shared_read],
    "edit_store_connect": [add_nba_package, edit_store_connect],
    "edit_store_digest": [add_nba_package, edit_store_digest],
}

out = {}
with tempfile.TemporaryDirectory() as scratch:
    for name, steps in VARIANTS.items():
        tree = Path(scratch) / name
        shutil.copytree(Path(args.src) / "polymarket861", tree / "polymarket861", ignore=shutil.ignore_patterns("__pycache__"))
        for step in steps:
            step(tree / "polymarket861")
        out[name] = measure(tree)

base = out["baseline"]
summary = {"b1_recipe_modules": base["b1_recipe_modules"], "b1_recipe_module_count": len(base["b1_recipe_modules"]), "variants": {}}
for name, got in out.items():
    summary["variants"][name] = {
        "football_recipes_changed": sorted(
            f"{family}.{kind}"
            for family, pair in got["recipes"].items()
            for kind, value in pair.items()
            if value != base["recipes"][family][kind]
        ),
        "b1_primary": got["recipes"]["B1"]["primary"],
        "package_files": got["package_files"],
        "package_files_changed": got["package_files_sha256"] != base["package_files_sha256"],
        "decision_modules": got["decision_modules"],
        "decision_implementation_changed": got["decision_implementation_sha256"] != base["decision_implementation_sha256"],
    }
json.dump(summary, sys.stdout, ensure_ascii=False, indent=1)
print()
