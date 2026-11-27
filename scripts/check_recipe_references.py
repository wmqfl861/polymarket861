#!/usr/bin/env python3
"""Review cross-module edges from the live primary football probability recipe."""

import argparse
import ast
import json
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from polymarket861 import recipe_identity, store  # noqa: E402


def import_names(node):
    """Resolve Python's local binding, including relative, absolute and aliased imports."""
    for item in node.names:
        if isinstance(node, ast.ImportFrom):
            module = node.module.split(".") if node.module else []
            prefix = ["polymarket861"] if node.level == 1 else []
            parts = (*prefix, *module, item.name)
            name = item.asname or item.name
        else:
            parts = tuple(item.name.split("."))
            name = item.asname or parts[0]
            if not item.asname:
                parts = parts[:1]
        if parts[0] == "polymarket861":
            yield name, parts[1:]
        elif parts[0] == "importlib":
            yield name, ("@importlib", *parts[1:])
        else:
            yield name, None


def dotted(node, imports):
    if isinstance(node, ast.Name):
        return imports.get(node.id, ())
    if isinstance(node, ast.Attribute):
        return [(*base, node.attr) for base in dotted(node.value, imports)]
    return ()


SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def scope_parts(roots):
    """Keep imports local to their lexical scope, including different class methods."""
    nodes, nested = [], []
    pending = [(root, None) for root in roots]
    while pending:
        node, parent = pending.pop()
        if isinstance(node, SCOPES):
            nested.append(node)
            continue
        nodes.append((node, parent))
        for child in ast.iter_child_nodes(node):
            pending.append((child, node))
    return nodes, nested


class Package:
    def __init__(self, directory):
        self.directory = directory
        self.modules = {p.stem for p in directory.glob("*.py")}

    @cache
    def module(self, name):
        tree = ast.parse((self.directory / (name + ".py")).read_bytes())
        imports = {
            local: qualified
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for local, qualified in import_names(node)
        }
        # The recipe selector hashes direct top-level bindings. A conditional
        # module import can reroute a caller even if both destinations are signed.
        for node, parent in scope_parts(tree.body)[0]:
            if parent is not None and isinstance(node, (ast.Import, ast.ImportFrom)):
                for local, qualified in import_names(node):
                    if qualified is not None and qualified[:1] != ("@importlib",):
                        imports[local] = (name, "<dynamic>")
        return recipe_identity.bindings(tree), imports

    def binding_digest(self, qualified_name):
        module, symbol = qualified_name.split(".", 1)
        if module in self.modules:
            definitions, _ = self.module(module)
            node = definitions.get(symbol)
            if isinstance(node, ast.AST):
                return store.digest(recipe_identity._tree(node))
        return None

    def route(self, module, symbol):
        """The re-export itself and its final destination must both be identified."""
        visited = set()
        while (module, symbol) not in visited:
            visited.add((module, symbol))
            yield f"{module}.{symbol}"
            _, imports = self.module(module)
            target = imports.get(symbol)
            if not target or len(target) != 2 or target[0] not in self.modules:
                return
            module, symbol = target
        raise ValueError(f"Circular package reference for {module}.{symbol}")

    @cache
    def references(self, module, node):
        _, imports = self.module(module)
        module_scope = {
            key: {target} for key, target in imports.items() if target is not None
        }
        result = set()
        pending = [(node, module_scope)]
        while pending:
            owner, inherited = pending.pop()
            local = inherited.copy()
            body = [owner]
            if isinstance(owner, SCOPES):
                body = owner.body if isinstance(owner.body, list) else [owner.body]
                # Defaults, annotations, bases and decorators run in the outer scope.
                body_nodes = set(body)
                pending.extend(
                    (part, inherited)
                    for part in ast.iter_child_nodes(owner)
                    if part not in body_nodes
                )
                if hasattr(owner, "args"):
                    args = owner.args
                    for arg in (
                        *args.posonlyargs,
                        *args.args,
                        *args.kwonlyargs,
                        args.vararg,
                        args.kwarg,
                    ):
                        if arg is not None:
                            local.pop(arg.arg, None)
            parts, nested = scope_parts(body)
            # A branch may bind the same alias to either of two different helpers.
            choices = {}
            for part, _ in parts:
                if isinstance(part, (ast.Import, ast.ImportFrom)):
                    for name, target in import_names(part):
                        if name == "*" and target is not None:
                            raise ValueError(
                                f"Use named imports for package references in {module}"
                            )
                        choices.setdefault(name, set())
                        if target is not None:
                            choices[name].add(target)
            local.update(choices)
            pending.extend((child, local) for child in nested)
            result.update(self.scope_references(parts, local))
        return result

    def scope_references(self, parts, local):
        for part, parent in parts:
            if isinstance(part, ast.Call) and (
                (isinstance(part.func, ast.Name) and part.func.id == "__import__")
                or ("@importlib", "import_module") in dotted(part.func, local)
            ):
                arg = part.args[0] if part.args else None
                if (
                    not isinstance(arg, ast.Constant)
                    or not isinstance(arg.value, str)
                    or (
                        arg.value.startswith((".", "polymarket861."))
                        or arg.value == "polymarket861"
                    )
                ):
                    yield "polymarket861.<dynamic>"
            if (
                isinstance(part, ast.Call)
                and isinstance(part.func, ast.Name)
                and part.func.id == "getattr"
                and len(part.args) >= 2
            ):
                for base in dotted(part.args[0], local):
                    if not base:
                        yield "polymarket861.<dynamic>"
                    if len(base) != 1 or base[0] not in self.modules:
                        continue
                    attr = part.args[1]
                    symbol = (
                        attr.value
                        if isinstance(attr, ast.Constant)
                        and isinstance(attr.value, str)
                        else "<dynamic>"
                    )
                    yield from self.route(base[0], symbol)
            if not isinstance(part, (ast.Name, ast.Attribute)) or not isinstance(
                part.ctx, ast.Load
            ):
                continue
            for target in dotted(part, local):
                if not target:
                    if not isinstance(parent, ast.Attribute):
                        yield "polymarket861.<dynamic>"
                    continue
                if target[0] == "@importlib":
                    continue
                if target[0] not in self.modules:
                    # Importing directly from the package can refer to __init__.py.
                    yield f"__init__.{target[0]}"
                    continue
                if len(target) == 2:
                    yield from self.route(*target)
                if len(target) != 1:
                    continue
                if isinstance(parent, ast.Attribute) and part is parent.value:
                    continue
                if (
                    isinstance(parent, ast.Call)
                    and isinstance(parent.func, ast.Name)
                    and parent.func.id == "getattr"
                    and parent.args
                    and part is parent.args[0]
                ):
                    continue
                # Passing a module as a value hides which definitions the recipe calls.
                yield f"{target[0]}.<dynamic>"

    def primary_edges(self, family):
        selected = {}
        for filename, roots in recipe_identity.primary_roots(family):
            module = Path(filename).stem
            definitions, imports = self.module(module)
            if "*" in imports and imports["*"] is not None:
                raise ValueError(
                    f"Use named imports for package references in {filename}"
                )
            missing = set(roots) - definitions.keys()
            if missing:
                raise ValueError(
                    f"Missing primary recipe roots in {filename}: {sorted(missing)}"
                )
            nodes = recipe_identity.selected_bindings(
                definitions, roots, recipe_identity._INFRASTRUCTURE.get(filename, ())
            )
            for name, node in nodes.items():
                selected[f"{module}.{name}"] = (module, node)
        return {
            (caller, target)
            for caller, (module, node) in selected.items()
            if isinstance(node, ast.AST)
            for target in self.references(module, node)
            if target not in selected
        }


def audit(package, policy):
    expected = set()
    changed_bindings = {}
    for target, decision in policy.items():
        if target.endswith(".<dynamic>"):
            raise ValueError(f"Use a named package reference for {target}")
        if not decision["reason"].strip():
            raise ValueError(f"Missing rationale for {target}")
        for name, digest in decision.get("reviewed_bindings", {}).items():
            actual_digest = package.binding_digest(name)
            if actual_digest is None or actual_digest != digest:
                changed_bindings[name] = {"expected": digest, "actual": actual_digest}
        for family in decision.get("families", ["B0", "B1", "B2", "B3"]):
            for caller in decision["callers"]:
                expected.add((family, caller, target))
    actual = {
        (family, caller, target)
        for family in ("B0", "B1", "B2", "B3")
        for caller, target in package.primary_edges(family)
    }
    return {
        "scope": "primary football probability and input admission; B3 news is signed separately",
        "reviewed_edges_by_family": {
            family: sum(f == family for f, _, _ in actual & expected)
            for family in ("B0", "B1", "B2", "B3")
        },
        "unreviewed": sorted(actual - expected),
        "obsolete": sorted(expected - actual),
        "changed_reviewed_bindings": changed_bindings,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, default=ROOT / "src")
    parser.add_argument(
        "--policy", type=Path, default=ROOT / "scripts/recipe-reference-policy.json"
    )
    args = parser.parse_args()
    result = audit(
        Package(args.src / "polymarket861"), json.loads(args.policy.read_text())
    )
    print(json.dumps(result, indent=2))
    return int(bool(result["unreviewed"] or result["obsolete"] or result["changed_reviewed_bindings"]))


if __name__ == "__main__":
    raise SystemExit(main())
