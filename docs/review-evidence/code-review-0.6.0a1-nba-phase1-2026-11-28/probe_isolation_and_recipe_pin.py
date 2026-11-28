#!/usr/bin/env python3
"""上轮 X4-1（足球配方不因 NBA 开发而变）和 X4-3（两套数据目录互不误开）的通过标准，照原话实测。

A 配方身份钉住了没有
  把 src 和 scripts 拷到临时目录，在指定函数体开头插一条 `pass`，再跑持续集成里的那条命令
  `scripts/check_recipe_identity.py --approved scripts/football-recipe-baseline.json`。
  上轮写的通过标准：改 market_http.read 或 store.digest 时这条检查必须失败。
  对照组：不改、改 NBA 模块、改 store.connect（本版为了加目录守卫确实改过它）。

B 两个方向的目录守卫（本版代码）
  用 0.5.0a4 的源码建一个足球目录（生产机上的目录就是老版本建的，没有归属标记），用本版建一个
  NBA 目录。然后：NBA 命令指向足球目录、足球命令指向 NBA 目录、直接调用 store.connect 与
  nba.storage.connect 指向对方目录。每次都要求非零退出，且目标目录逐文件哈希、权限、目录项不变。
  另查：本版足球命令打开老版本建的足球目录仍然正常（守卫不能把自己的目录挡在外面）。

C 已部署的 0.5.0a4 进程看不到新守卫
  用 0.5.0a4 源码直接对 NBA 目录跑 `jobs status`、`fixture list` 和一次 store.connect。
  带 --unprivileged-uid 时以该普通用户身份再跑一遍（部署机上是普通用户；root 不受 0500 权限约束）。

用法：
  git archive 60c1a2d src | tar -x -C <old>            # 0.5.0a4 的源码
  python -I probe_isolation_and_recipe_pin.py --repo <仓库根> --old-src <old>/src [--unprivileged-uid 65534]

不联网；只写自己建的临时目录。
"""

import argparse
import ast
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--repo", required=True)
parser.add_argument("--old-src", required=True)
parser.add_argument("--unprivileged-uid", type=int)
args = parser.parse_args()
REPO = Path(args.repo).resolve()
OLD_SRC = Path(args.old_src).resolve()

CLI = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
       "from polymarket861.cli import main; raise SystemExit(main())")
CALL = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import importlib; "
        "module, name, target = sys.argv[1:4]; "
        "getattr(importlib.import_module(module), name)(__import__('pathlib').Path(target)).close()")


def run(code, src, argv, *, uid=None, env=None):
    cmd = [sys.executable, "-I", "-c", code, str(src), *map(str, argv)]
    if uid is not None:
        cmd = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", *cmd]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=300,
                          env={"PATH": os.environ["PATH"], "HOME": str(env or "/nonexistent")})
    lines = (done.stderr or done.stdout).strip().splitlines()
    return {"exit": done.returncode, "last_line": lines[-1][:240] if lines else ""}


def fingerprint(root):
    items = []
    for path in sorted(Path(root).rglob("*")):
        entry = path.lstat()
        kind = "dir" if stat.S_ISDIR(entry.st_mode) else "file" if stat.S_ISREG(entry.st_mode) else "other"
        sha = hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else ""
        items.append((path.relative_to(root).as_posix(), kind, oct(stat.S_IMODE(entry.st_mode)), sha))
    return hashlib.sha256(json.dumps(items).encode()).hexdigest(), [item[:3] for item in items]


def insert_pass(path, name, *, klass=None):
    """在函数体第一条语句前插一条 pass。语法树变了，运行行为不变。"""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    scope = tree.body
    if klass:
        scope = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == klass).body
    node = next(n for n in scope if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    first = node.body[0]
    lines = source.splitlines(keepends=True)
    lines.insert(first.lineno - 1, " " * first.col_offset + "pass\n")
    path.write_text("".join(lines), encoding="utf-8")


out = {"python": sys.version.split()[0]}

# A ── 配方身份
cases = [
    ("unmodified", None, None, "pass"),
    ("market_http.read (module function)", "market_http.py", ("read", None), "fail"),
    ("store.digest", "store.py", ("digest", None), "fail"),
    ("football_model.fit_team_model", "football_model.py", ("fit_team_model", None), "fail"),
    ("store.connect", "store.py", ("connect", None), "informational"),
    ("nba/model.py ScoreState.centers", "nba/model.py", ("centers", "ScoreState"), "pass"),
    ("nba/markets.py collect", "nba/markets.py", ("collect", None), "pass"),
]
pin = []
with tempfile.TemporaryDirectory() as scratch:
    for label, target, where, expected in cases:
        work = Path(scratch) / f"case{len(pin)}"
        shutil.copytree(REPO / "src", work / "src")
        shutil.copytree(REPO / "scripts", work / "scripts")
        if target:
            insert_pass(work / "src" / "polymarket861" / target, where[0], klass=where[1])
        code = ("import sys, runpy; sys.path.insert(0, sys.argv.pop(1)); "
                "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')")
        done = run(code, work / "src", [work / "scripts" / "check_recipe_identity.py", "--approved",
                                        work / "scripts" / "football-recipe-baseline.json"])
        verdict = "pass" if done["exit"] == 0 else "fail"
        pin.append({"edit": label, "check": verdict, "expected": expected,
                    "as_expected": expected == "informational" or verdict == expected,
                    "message": "" if done["exit"] == 0 else done["last_line"]})
out["recipe_pin"] = pin

# B、C ── 目录守卫
guards = []
legacy = []
with tempfile.TemporaryDirectory() as scratch:
    scratch = Path(scratch)
    if args.unprivileged_uid is not None:
        os.chmod(scratch, 0o755)
    football, nba = scratch / "football-state", scratch / "nba-state"
    made = run(CALL, OLD_SRC, ["polymarket861.store", "connect", football])
    out["football_root_created_by_0.5.0a4"] = {**made, "entries": fingerprint(football)[1]}
    made = run(CALL, REPO / "src", ["polymarket861.nba.storage", "connect", nba])
    out["nba_root_created_by_this_version"] = {**made, "entries": fingerprint(nba)[1]}

    def attempt(label, code, src, argv, target, sink, *, expect_refusal=True, uid=None):
        before = fingerprint(target)[0]
        done = run(code, src, argv, uid=uid, env=scratch / "home")
        after = fingerprint(target)
        sink.append({"case": label, **done, "refused": done["exit"] != 0, "target_unchanged": before == after[0],
                     "as_expected": (done["exit"] != 0) == expect_refusal and before == after[0],
                     **({} if before == after[0] else {"entries_after": after[1]})})

    new = REPO / "src"
    attempt("this version: `nba status` pointed at the football root", CLI, new,
            ["--data-dir", football, "nba", "status"], football, guards)
    attempt("this version: `nba run --component markets` pointed at the football root", CLI, new,
            ["--data-dir", football, "nba", "run", "--component", "markets"], football, guards)
    attempt("this version: nba.storage.connect(football root)", CALL, new,
            ["polymarket861.nba.storage", "connect", football], football, guards)
    attempt("this version: `fixture list` pointed at the NBA root", CLI, new,
            ["--data-dir", nba, "fixture", "list"], nba, guards)
    attempt("this version: `jobs status` pointed at the NBA root", CLI, new,
            ["--data-dir", nba, "jobs", "status"], nba, guards)
    attempt("this version: store.connect(NBA root)", CALL, new,
            ["polymarket861.store", "connect", nba], nba, guards)
    attempt("this version: store.connect(subdirectory of the NBA root)", CALL, new,
            ["polymarket861.store", "connect", nba / "inner"], nba, guards)
    attempt("this version: nba.storage.connect(subdirectory of the football root)", CALL, new,
            ["polymarket861.nba.storage", "connect", football / "inner"], football, guards)
    attempt("this version: `fixture list` on the football root made by 0.5.0a4 (must still work)", CLI, new,
            ["--data-dir", football, "fixture", "list"], football, guards, expect_refusal=False)

    users = [None] + ([args.unprivileged_uid] if args.unprivileged_uid is not None else [])
    for uid in users:
        who = "root" if uid is None and os.geteuid() == 0 else f"uid {os.geteuid() if uid is None else uid}"
        if uid is not None:
            for path in [nba, *nba.rglob("*")]:
                os.chown(path, uid, uid)
        for label, code, argv in (
            ("`jobs status`", CLI, ["--data-dir", nba, "jobs", "status"]),
            ("`fixture list`", CLI, ["--data-dir", nba, "fixture", "list"]),
            ("store.connect", CALL, ["polymarket861.store", "connect", nba]),
        ):
            attempt(f"0.5.0a4 as {who}: {label} pointed at the NBA root", code, OLD_SRC, argv, nba, legacy, uid=uid)
out["directory_guards_this_version"] = guards
out["deployed_0.5.0a4_against_nba_root"] = legacy
out["all_as_expected"] = all(item["as_expected"] for item in pin + guards + legacy)

json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
print()
