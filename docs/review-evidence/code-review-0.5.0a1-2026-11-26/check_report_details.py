#!/usr/bin/env python3
"""随仓报告里三处可以直接重算的细节。不导入本项目的包，不联网。

1. 南特对图卢兹的兑付凭据（P4-1）：reports/five-leagues-0.5/settlement-observation.json 里内嵌的
   四份原件，解压后的 SHA-256 是否等于报告记录的值；原件里写的终态是不是大球 0、小球 1。
2. 第 12 轮提前封存里的停赛条目（T2-2）：reports/news-early-seals-0.4.2.json 的 90 条已计入的伤停里，
   停赛有几条、是谁、合计权重占多少；与开发方 news-audit.json 列出的 6 条是不是同一批。
3. 比较报告（T2-5，可选）：升级演练得到的 release-compare 报告与随仓发布的那份相比，
   叶子字段有多少个相同、哪些不同。

用法：
  python -I check_report_details.py <仓库> [--compare <升级演练的临时目录>/release-compare.json]
"""

import argparse
import base64
import gzip
import hashlib
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("repo")
parser.add_argument("--compare")
args = parser.parse_args()
repo = Path(args.repo)
reports = repo / "reports"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def leaves(value, path=""):
    if isinstance(value, dict):
        for key in sorted(value):
            yield from leaves(value[key], f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from leaves(item, f"{path}/{index}")
    else:
        yield path, value


# 1. 兑付凭据
settlement = load(reports / "five-leagues-0.5" / "settlement-observation.json")
receipts = []
for name, receipt in settlement["receipts"].items():
    if receipt["body_encoding"] != "gzip+base64":
        raise SystemExit(f"{name}: 没有预料到的编码 {receipt['body_encoding']}")
    original = gzip.decompress(base64.b64decode(receipt["body"]))
    body = json.loads(original)
    row = {
        "receipt": name,
        "source": receipt["source"].split("?")[0],
        "bytes": len(original),
        "recorded_sha256": receipt["sha256"],
        "sha256_of_decoded_body": hashlib.sha256(original).hexdigest(),
    }
    row["matches"] = row["recorded_sha256"] == row["sha256_of_decoded_body"]
    if name == "gamma":
        row["terminal_state_in_original"] = {
            k: body.get(k) for k in ("outcomes", "outcomePrices", "closed", "umaResolutionStatus")
        }
    elif name == "clob":
        row["terminal_state_in_original"] = {
            "closed": body.get("closed"),
            "accepting_orders": body.get("accepting_orders"),
            "tokens": [{k: t.get(k) for k in ("outcome", "price", "winner")} for t in body.get("tokens", [])],
        }
    else:
        rows = body.get("data") if isinstance(body, dict) else body
        row["terminal_state_in_original"] = [
            {k: t.get(k) for k in ("outcome", "resolved", "closed", "final_price") if k in t} for t in rows or []
        ]
    receipts.append(row)

# 2. 停赛条目
seals = load(reports / "news-early-seals-0.4.2.json")["technical_early_seals"]
applied = [
    {"fixture_id": seal["fixture_id"], **item} for seal in seals for item in seal["applied_player_weights"]
]
suspended = [item for item in applied if item["event"] == "suspension"]
total = sum(item["combined_weight"] for item in applied)
share = sum(item["combined_weight"] for item in suspended)
audit = load(reports / "five-leagues-0.5" / "news-audit.json")["frozen_0_4_2"]["suspensions"]
suspensions = {
    "applied_items": len(applied),
    "events": {event: sum(1 for i in applied if i["event"] == event) for event in sorted({i["event"] for i in applied})},
    "combined_weight_all_items": round(total, 6),
    "combined_weight_suspensions": round(share, 6),
    "suspension_share_of_combined_weight": round(share / total, 4),
    "suspensions": [
        {
            "fixture_id": i["fixture_id"],
            "player": i["player"],
            "team": i["team"],
            "role": i["role"],
            "combined_weight": round(i["combined_weight"], 6),
            "importance_multiplier": round(i["importance_multiplier"], 4),
        }
        for i in sorted(suspended, key=lambda i: -i["combined_weight"])
    ],
    "same_six_as_developer_audit": sorted((i["fixture_id"], i["player"]) for i in suspended)
    == sorted((a["fixture_id"], a["player"]) for a in audit),
    "developer_classification_under_new_rule": sorted({a["classification_under_new_rule"] for a in audit}),
}

result = {
    "settlement_receipts": {"all_match": all(r["matches"] for r in receipts), "receipts": receipts},
    "round_12_suspensions": suspensions,
}

# 3. 比较报告
if args.compare:
    mine = dict(leaves(load(args.compare)))
    published = dict(leaves(load(reports / "five-leagues-0.5" / "release-comparison-0.4.2.json")))
    paths = sorted(set(mine) | set(published))
    different = [p for p in paths if mine.get(p, "<缺>") != published.get(p, "<缺>")]
    result["release_comparison"] = {
        "published": "reports/five-leagues-0.5/release-comparison-0.4.2.json",
        "leaf_fields": len(paths),
        "identical": len(paths) - len(different),
        "different": {p: {"this_run": mine.get(p, "<缺>"), "published": published.get(p, "<缺>")} for p in different},
    }

json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
print()
