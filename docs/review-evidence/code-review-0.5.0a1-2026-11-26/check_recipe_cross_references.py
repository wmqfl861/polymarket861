#!/usr/bin/env python3
"""新配方（football-probability-v2）纳入的代码，跨文件引用了哪些没有纳入的定义。

recipe_identity.symbols_digest 只在同一个文件里顺着名字追引用。对别的文件，
它记下的是一条导入（例如 [".", "store"] 或 [".store", "digest"]），被引用的那个定义
要靠 _PRIMARY 清单在那个文件名下另外点名，或者被那边点名的入口在文件内引用到，
才会进配方。这个脚本静态列出全部跨文件引用，逐个核对目标是否在配方里。

做法：
  1. 用产品自己的 symbols_digest 取得每个文件实际纳入的定义名（拦截它交给 store.digest 的内容）。
  2. 用同样的规则自己再算一遍，两边的名字集合必须相同，否则脚本报错退出。
  3. 在纳入的定义的语法树里找两类跨文件引用：
     a. `模块.名字`，其中“模块”是 `from . import 模块` 或 `from .包 import 模块` 绑定的包内模块；
     b. `from .模块 import 名字` 绑定、并且被纳入的定义用到的名字。
  4. 目标如果只是转手导出（例如 store.read_blob 实际定义在 blob_storage.py），先找到真正的定义。
     定义所在的文件不在清单里，或者名字不在该文件纳入的定义里，就列为“未纳入”。

用法：python -I check_recipe_cross_references.py --src <仓库>/src [--family B1]
只读源码，不联网，不需要数据目录。
"""

import argparse
import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--family", default="B1")
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import model_study, recipe_identity, store  # noqa: E402

PACKAGE = Path(polymarket861.__file__).resolve().parent
PHYSICAL = model_study.probability_implementation()["algorithm_source"]
MODULES = {p.stem for p in PACKAGE.glob("*.py")}


def product_selection(name, roots):
    captured = []
    with patch.object(store, "digest", side_effect=lambda value: captured.append(value) or "x"):
        recipe_identity.symbols_digest.__wrapped__(
            PACKAGE / name, PHYSICAL[name], roots, recipe_identity._INFRASTRUCTURE.get(name, ())
        )
    return captured[-1]


def own_selection(name, roots):
    """与 recipe_identity.symbols_digest 相同的规则，但保留语法树节点，供下面找跨文件引用。"""
    tree = ast.parse((PACKAGE / name).read_bytes())
    ignored = recipe_identity._INFRASTRUCTURE.get(name, ())
    definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                for item in ast.walk(target):
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                        definitions[item.id] = node
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            prefix = "." * node.level + (node.module or "") if isinstance(node, ast.ImportFrom) else ""
            for alias in node.names:
                definitions[alias.asname or alias.name.split(".")[0]] = [prefix, alias.name]
    selected, pending = {}, set(roots)
    while pending:
        item = pending.pop()
        if item in selected:
            continue
        node = definitions.get(item)
        selected[item] = node
        if isinstance(node, ast.AST):
            pending.update(
                ref.id
                for ref in ast.walk(node)
                if isinstance(ref, ast.Name) and ref.id in definitions and ref.id not in ignored
            )
    return selected, definitions


roots_by_file = {}
for name, roots in recipe_identity._PRIMARY.items():
    if name in PHYSICAL:
        if args.family == "B2" and name == "football_model.py":
            roots = roots + ("apply_context",)
        roots_by_file[name] = roots

selection, covered = {}, {}
for name, roots in roots_by_file.items():
    mine, definitions = own_selection(name, roots)
    theirs = product_selection(name, roots)
    if set(mine) != set(theirs):
        raise SystemExit(f"自算的纳入集合与产品不一致：{name}: {sorted(set(mine) ^ set(theirs))}")
    selection[name] = (mine, definitions)
    covered[name] = {k for k, v in mine.items() if isinstance(v, ast.AST)}


def package_module(binding):
    """导入绑定是否指向包内的一个模块；是则返回文件名。"""
    prefix, target = binding
    if prefix == "." and target in MODULES:
        return target + ".py"
    return None


def package_symbol(binding):
    """`from .模块 import 名字` 形式的绑定，返回（文件名，名字）。"""
    prefix, target = binding
    if prefix.startswith(".") and prefix.count(".") == 1 and prefix[1:] in MODULES:
        return prefix[1:] + ".py", target
    return None


references = {}
for name, (mine, definitions) in selection.items():
    for holder, node in mine.items():
        if not isinstance(node, ast.AST):
            continue
        # 函数体内的局部导入（from . import x / from .x import y）也是跨文件引用的来源。
        local = dict(definitions)
        for inner in ast.walk(node):
            if isinstance(inner, ast.ImportFrom) and inner.level == 1:
                prefix = "." + (inner.module or "")
                for alias in inner.names:
                    local[alias.asname or alias.name] = [prefix, alias.name]
        for inner in ast.walk(node):
            target = None
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name):
                binding = local.get(inner.value.id)
                if isinstance(binding, list) and package_module(binding):
                    target = (package_module(binding), inner.attr)
            elif isinstance(inner, ast.Name) and isinstance(inner.ctx, ast.Load):
                binding = local.get(inner.id)
                if isinstance(binding, list) and package_symbol(binding):
                    target = package_symbol(binding)
            if target and target[0] != name:
                references.setdefault(target, set()).add(f"{name}:{holder}")

_bindings = {}


def module_bindings(filename):
    """一个文件顶层的全部绑定：定义记为 True，包内导入记为它指向的（文件名，名字）。"""
    if filename not in _bindings:
        found = {}
        for node in ast.parse((PACKAGE / filename).read_bytes()).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                found[node.name] = True
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    for item in ast.walk(target):
                        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store):
                            found[item.id] = True
            elif isinstance(node, ast.ImportFrom) and node.level == 1 and node.module in MODULES:
                for alias in node.names:
                    found[alias.asname or alias.name] = (node.module + ".py", alias.name)
        _bindings[filename] = found
    return _bindings[filename]


def resolve(filename, symbol):
    """顺着“只是转手导出”的名字找到真正的定义所在，例如 store.read_blob 实际定义在 blob_storage.py。"""
    chain = [f"{filename}:{symbol}"]
    for _ in range(6):
        binding = module_bindings(filename).get(symbol)
        if not isinstance(binding, tuple):
            break
        filename, symbol = binding
        chain.append(f"{filename}:{symbol}")
    return filename, symbol, chain


rows = []
for (target_file, symbol), users in sorted(references.items()):
    final_file, final_symbol, chain = resolve(target_file, symbol)
    listed = final_file in covered
    inside = listed and final_symbol in covered[final_file]
    rows.append(
        {
            "target": f"{target_file}:{symbol}",
            "defined_at": chain[-1],
            "target_file_in_recipe_list": listed,
            "target_definition_in_recipe": inside,
            "deliberately_ignored": final_symbol in recipe_identity._INFRASTRUCTURE.get(final_file, ()),
            "used_by": sorted(users),
        }
    )

missing = [r for r in rows if not r["target_definition_in_recipe"]]
json.dump(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "family": args.family,
        "files_in_recipe_list": len(covered),
        "definitions_in_recipe": sum(len(v) for v in covered.values()),
        "cross_file_references": len(rows),
        "cross_file_references_into_recipe": len(rows) - len(missing),
        "cross_file_references_not_in_recipe": len(missing),
        "not_in_recipe": missing,
        "in_recipe": [r["target"] for r in rows if r["target_definition_in_recipe"]],
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
