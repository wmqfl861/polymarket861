#!/usr/bin/env python3
"""deploy/paper-automation 下的脚本不在安装包里，但直接调用安装包的函数。
这个探针列出它们用到的 polymarket861 属性，检查每一个在给定源码里是否存在，并数出其中下划线开头的内部函数。
只解析语法树和导入安装包模块，不运行这些脚本，不联网。

用法：
  python -I probe_deploy_script_wheel_bindings.py --src <src 目录> <deploy/paper-automation 目录>
"""

import argparse
import ast
import importlib
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("scripts")
args = parser.parse_args()
sys.path.insert(0, args.src)

used = {}
lines = {}
for path in sorted(Path(args.scripts).rglob("*.py")):
    text = path.read_text(encoding="utf-8")
    lines[path.name] = text.count("\n")
    tree = ast.parse(text)
    alias = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("polymarket861"):
            for name in node.names:
                if node.module == "polymarket861":
                    alias[name.asname or name.name] = "polymarket861." + name.name
                else:
                    used.setdefault((node.module, name.name), set()).add(path.name)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in alias:
            used.setdefault((alias[node.value.id], node.attr), set()).add(path.name)

missing, private = [], {}
for (module, name), files in sorted(used.items()):
    if not hasattr(importlib.import_module(module), name):
        missing.append({"module": module, "name": name, "used_by": sorted(files)})
    if name.startswith("_"):
        private[f"{module.split('.')[-1]}.{name}"] = sorted(files)

json.dump(
    {
        "script_lines": lines,
        "script_lines_total": sum(lines.values()),
        "package_attributes_used": len(used),
        "missing_in_this_source": missing,
        "private_names_used": len(private),
        "private_names": private,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
