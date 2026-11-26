#!/usr/bin/env python3
"""封存一份预测时实际执行到的函数，有哪些不在新配方（football-probability-v2）的覆盖范围里。

0.5.0a1 的配方不再按整个文件取摘要，改为对 recipe_identity._PRIMARY 里声明的入口，
连同它们在同一个文件内按名字引用到的定义，逐个取语法树摘要。跨文件的调用
（例如 store.eligible_fixtures(...)）只记一条导入，不会顺着追过去，被调用的函数要靠
清单里另外点名才算进配方。这个脚本在一次真实的 `forecast make` 上挂执行探针，
把执行到的包内函数和配方实际纳入的定义逐个对照。

用法：
  python -I trace_recipe_coverage.py --src <仓库>/src --data <已批准 0.5.0a1 的临时数据目录> \
      --inputs <该目录下 datasets/football-inputs/*/sources> --fixture epl/2026-27/<主队>/<客队> \
      [--horizon early]

只应当对临时数据目录使用，因为它会真的封存一份预测。输出 JSON 到标准输出。
"""

import argparse
import json
import sys
import threading
from pathlib import Path
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--data", required=True)
parser.add_argument("--inputs", required=True)
parser.add_argument("--fixture", required=True)
parser.add_argument("--horizon", default="early")
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import cli, model_study, recipe_identity, store  # noqa: E402

PACKAGE = Path(polymarket861.__file__).resolve().parent


def covered_definitions():
    """对每个点名的文件，取出配方实际纳入的顶层定义名（用产品自己的函数算，不重写算法）。"""
    physical = model_study.probability_implementation()["algorithm_source"]
    covered = {}
    for name, roots in recipe_identity._PRIMARY.items():
        if name not in physical:
            continue
        captured = []
        with patch.object(store, "digest", side_effect=lambda value: captured.append(value) or "x"):
            recipe_identity.symbols_digest.__wrapped__(
                PACKAGE / name,
                physical[name],
                roots,
                recipe_identity._INFRASTRUCTURE.get(name, ()),
            )
        selected = captured[-1]
        covered[name] = {
            "roots": list(roots),
            # 语法树节点编码成 [节点类型, {字段}]，导入只编码成 [前缀, 名字] 两个字符串。
            "definitions": sorted(
                k for k, v in selected.items() if isinstance(v, list) and len(v) == 2 and isinstance(v[1], dict)
            ),
            "imports_recorded_by_name_only": sorted(
                k for k, v in selected.items() if isinstance(v, list) and len(v) == 2 and isinstance(v[1], str)
            ),
            "unresolved_names": sorted(k for k, v in selected.items() if v is None),
            "deliberately_ignored": list(recipe_identity._INFRASTRUCTURE.get(name, ())),
        }
    return covered, sorted(physical)


executed = {}


def probe(frame, event, arg):
    if event != "call":
        return
    code = frame.f_code
    filename = code.co_filename
    if not filename.startswith(str(PACKAGE)):
        return
    key = (Path(filename).name, code.co_qualname)
    executed[key] = executed.get(key, 0) + 1


covered, physical_files = covered_definitions()
argv = ["--data-dir", args.data, "forecast", "make", args.fixture, args.inputs, "--horizon", args.horizon]
threading.setprofile(probe)
sys.setprofile(probe)
try:
    with patch("builtins.print", lambda *a, **k: None):
        status = cli.main(argv)
finally:
    sys.setprofile(None)
    threading.setprofile(None)

modules = {}
for (module, qualname), calls in sorted(executed.items()):
    top = qualname.split(".")[0]
    entry = modules.setdefault(module, {"in_recipe_list": module in covered, "covered": {}, "not_covered": {}})
    inside = module in covered and top in covered[module]["definitions"]
    bucket = entry["covered" if inside else "not_covered"]
    bucket[top] = bucket.get(top, 0) + calls

summary = {
    # 临时目录的位置换成 <data>，这样输出在别的机器上也能逐字节比较。
    "command": "pm861 " + " ".join(a.replace(args.data.rstrip("/"), "<data>") for a in argv),
    "exit_status": status,
    "package_version": polymarket861.__version__,
    "python": sys.version.split()[0],
    # 全包的 .py 文件里去掉 model_study.NON_DECISION_MODULES 之后的个数，不是全包的文件数。
    "decision_module_files": len(physical_files),
    "files_named_in_recipe_list": len(covered),
    "files_executed": len(modules),
    "files_executed_outside_recipe_list": sorted(m for m, e in modules.items() if not e["in_recipe_list"]),
    "executed_top_level_definitions": sum(len(e["covered"]) + len(e["not_covered"]) for e in modules.values()),
    "executed_and_covered": sum(len(e["covered"]) for e in modules.values()),
    "executed_and_not_covered": sum(len(e["not_covered"]) for e in modules.values()),
}
json.dump({"summary": summary, "modules": modules, "recipe_coverage": covered}, sys.stdout, ensure_ascii=False, indent=1)
print()
