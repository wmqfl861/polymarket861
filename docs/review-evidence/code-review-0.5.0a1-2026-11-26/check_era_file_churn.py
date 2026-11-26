#!/usr/bin/env python3
"""新闻前瞻研究的“概率版本”摘要，按现在的定义回看，最近几次发版会不会让它变（T1-2）。

epl-news-home-and-full-over-v1 只计登记后第一个概率版本的封存。这个版本摘要
（news_study_plan.era）包含两组按文件取的源码摘要：
  - 新闻概率源码：news_study_plan._MODEL_FILES 里的文件整个模块取语法树摘要，
    news_study_plan._SYMBOLS 里的文件只取点名的定义；
  - B1 主配方：recipe_identity._PRIMARY 里的文件取点名的定义。
这个脚本把几个发版提交的源码各导出一份，用 0.5.0a1 自己的摘要函数和 0.5.0a1 的清单，
对每个文件、每个提交各算一遍，再数相邻两个版本之间有多少个文件的摘要不同。
它回答的是：假如这项研究早就按现在的定义登记了，过去每一次升级会不会开启一个新的概率版本。
摘要按语法树取，只改注释或排版的文件不算变化；另外列出文字上有改动的文件数作对照。

用法：
  python -I check_era_file_churn.py --src <仓库>/src --repo <仓库> <空的临时目录> \
      [--releases 0.3.0=12d1717,0.4.1=ea58105,0.4.2=8125d57,0.5.0a1=49313f4]

只读仓库（git archive），不联网，不需要数据目录。
"""

import argparse
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--repo", required=True)
parser.add_argument("scratch")
parser.add_argument("--releases", default="0.3.0=12d1717,0.4.1=ea58105,0.4.2=8125d57,0.5.0a1=49313f4")
args = parser.parse_args()
sys.path.insert(0, args.src)

from polymarket861 import news_study_plan, recipe_identity  # noqa: E402

PACKAGE = "src/polymarket861"
WHOLE = tuple(news_study_plan._MODEL_FILES)
SYMBOLS = dict(news_study_plan._SYMBOLS)
PRIMARY = dict(recipe_identity._PRIMARY)
FILES = sorted(set(WHOLE) | set(SYMBOLS) | set(PRIMARY))
releases = [item.split("=") for item in args.releases.split(",")]


def export(commit):
    """把一个提交的包源码导出到临时目录，返回目录。"""
    target = Path(args.scratch) / commit
    target.mkdir(parents=True)
    blob = subprocess.run(
        ["git", "-C", args.repo, "archive", commit, PACKAGE], check=True, capture_output=True
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(blob)) as archive:
        archive.extractall(target, filter="data")
    return target / PACKAGE


def digests(root):
    """每个文件在三种取法下的摘要；文件不存在记为 None。"""
    table = {}
    for name in FILES:
        path = root / name
        if not path.exists():
            table[name] = None
            continue
        physical = hashlib.sha256(path.read_bytes()).hexdigest()
        row = {"physical": physical}
        if name in WHOLE:
            row["news_whole_module"] = recipe_identity.module_digest(path, physical)
        if name in SYMBOLS:
            row["news_symbols"] = recipe_identity.symbols_digest(
                path, physical, SYMBOLS[name], tuple(sorted(news_study_plan._STORAGE_ONLY.get(name, ())))
            )
        if name in PRIMARY:
            row["b1_primary"] = recipe_identity.symbols_digest(
                path, physical, PRIMARY[name], recipe_identity._INFRASTRUCTURE.get(name, ())
            )
        table[name] = row
    return table


tables = {version: digests(export(commit)) for version, commit in releases}
steps = []
for (before, _), (after, _) in zip(releases, releases[1:]):
    old, new = tables[before], tables[after]
    changed = {"news_whole_module": [], "news_symbols": [], "b1_primary": []}
    text = []
    for name in FILES:
        a, b = old[name] or {}, new[name] or {}
        if a.get("physical") != b.get("physical"):
            text.append(name)
        for kind in changed:
            if (kind in a or kind in b) and a.get(kind) != b.get(kind):
                changed[kind].append(name)
    any_digest = sorted(set().union(*changed.values()))
    steps.append(
        {
            "from": before,
            "to": after,
            "era_files_with_text_changes": len(text),
            "era_files_with_digest_changes": len(any_digest),
            "whole_module_files_changed": changed["news_whole_module"],
            "news_symbol_files_changed": changed["news_symbols"],
            "b1_primary_files_changed": changed["b1_primary"],
            "text_changed_but_no_digest_changed": sorted(set(text) - set(any_digest)),
            "would_start_a_new_probability_era": bool(any_digest),
        }
    )

json.dump(
    {
        "definition_taken_from": "0.5.0a1 (news_study_plan._MODEL_FILES, _SYMBOLS; recipe_identity._PRIMARY)",
        "python": sys.version.split()[0],
        "era_files": len(FILES),
        "whole_module_files": list(WHOLE),
        "news_symbol_files": len(SYMBOLS),
        "b1_primary_files": len(PRIMARY),
        "releases": dict(releases),
        "steps": steps,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
