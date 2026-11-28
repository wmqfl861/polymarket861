"""Check deployment scripts against the importable package without running the timers."""

import ast
import importlib
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1] / "deploy" / "paper-automation"
PACKAGE = "polymarket861"


def references(source):
    tree = ast.parse(source)
    modules = {}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                if name.name == PACKAGE or name.name.startswith(PACKAGE + "."):
                    modules[name.asname or name.name] = name.name
                    found.add((name.name, ""))
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == PACKAGE:
                for name in node.names:
                    module = PACKAGE + "." + name.name
                    modules[name.asname or name.name] = module
                    found.add((module, ""))
            elif node.module.startswith(PACKAGE + "."):
                found.update((node.module, name.name) for name in node.names)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        parts = []
        cursor = node
        while isinstance(cursor, ast.Attribute):
            parts.append(cursor.attr)
            cursor = cursor.value
        if isinstance(cursor, ast.Name):
            chain = [cursor.id, *reversed(parts)]
            for index in range(len(chain) - 1, 0, -1):
                alias = ".".join(chain[:index])
                if alias in modules:
                    found.add((modules[alias], chain[index]))
                    break
    return found


def main():
    errors = []
    bindings = set()
    for path in sorted(ROOT.rglob("*.py")):
        for module, symbol in sorted(references(path.read_text(encoding="utf-8"))):
            bindings.add((module, symbol))
            try:
                imported = importlib.import_module(module)
                if symbol:
                    getattr(imported, symbol)
            except (ImportError, AttributeError) as exc:
                errors.append(f"{path.relative_to(ROOT)}: {module}.{symbol}: {exc}")
    for error in errors:
        print(error, file=sys.stderr)
    print(f"Checked {len(bindings)} football paper deployment imports/attributes")
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
