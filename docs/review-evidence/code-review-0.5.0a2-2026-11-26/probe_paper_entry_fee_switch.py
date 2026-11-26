#!/usr/bin/env python3
"""五国模拟账户的一笔入场，在“交易所开着手续费开关”时两档各得到什么结果。

0.5.0a2 给五国资金回放加了排期研究档（--entry-basis scheduled_with_incomplete_flags）。
开发方的集成测试 tests/test_budget_domestic_archive.py 用的是关掉手续费的合成合约；
随仓报告说 11 月 26 日实采的 912 份合约全部开着手续费，而已审核的费率表是空的。

这个探针沿用那份测试的 domestic_entry：真实的四国采集路径，受控的 Gamma 和 CLOB 回复，
每个联赛做四组，只改两样东西：
  - 赛事有没有明确的赛前标记（live=false、ended=false），没有标记是 11 月 26 日实采看到的情形；
  - 合约的手续费开关。开着时用随仓文档记下的参数（rate 0.05、exponent 1、只收吃单方）。
手续费开着时产品拒绝给出合约条款，测试自带的写法到这一步就会报错。为了看回放本身怎样处理，
探针改用英超影子账户已有的研究用费用估计（shadow_market._fee，标注 research_assumption）
补上条款，再分别按默认档和排期研究档回放。

用法：
  python -I probe_paper_entry_fee_switch.py --src <仓库>/src --tests <仓库>/tests <空的临时目录>

不联网，不读写仓库，不触碰任何真实数据目录。
"""

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
parser.add_argument("--tests", required=True)
parser.add_argument("scratch")
args = parser.parse_args()
sys.path[:0] = [args.src, args.tests]

import test_budget_domestic_archive as helper  # noqa: E402

import polymarket861  # noqa: E402
from polymarket861 import (  # noqa: E402
    archive_proof,
    dbscope,
    fee_registry,
    five_league_budget,
    ledger_terms,
    shadow_market,
)

REAL_FEE = {"rate": 0.05, "exponent": 1, "takerOnly": True}
RESEARCH = "scheduled_with_incomplete_flags"


class Terms:
    """What the helper sees as ledger_terms: the product's terms, or the shadow account's labelled estimate."""

    def __init__(self):
        self.refusal = None

    def contract_at(self, root, condition, token, at, *, match_id=None):
        try:
            return ledger_terms.contract_at(root, condition, token, at, match_id=match_id)
        except ValueError as exc:
            self.refusal = str(exc)
            with dbscope.reading(root) as db:
                market = archive_proof.original_market(db, root, condition, at)
            fee = shadow_market._fee(root, market, at)
            return ledger_terms._terms_with_fee(root, market, token, at, match_id, fee)


def with_fee_switch(reply):
    def wrapped(payload, *rest, **more):
        if isinstance(payload, dict) and "markets" in payload:
            for market in payload["markets"]:
                market["feesEnabled"] = True
                market["feeSchedule"] = dict(REAL_FEE)
        return reply(payload, *rest, **more)

    return wrapped


def run(league, explicit, fee_on):
    name = f"{league}-{'flags' if explicit else 'noflags'}-{'fee' if fee_on else 'nofee'}"
    root = Path(args.scratch) / name
    terms = Terms()
    patches = [patch.object(helper, "ledger_terms", terms)]
    if fee_on:
        patches.append(patch.object(helper, "reply", with_fee_switch(helper.reply)))
    for p in patches:
        p.start()
    try:
        doc, _ = helper.domestic_entry(root, league, explicit=explicit)
    finally:
        for p in reversed(patches):
            p.stop()
    fee = doc["intents"][0]["fee"]
    row = {
        "league": league,
        "event_flags": "live=false, ended=false" if explicit else "absent",
        "fee_switch_in_gamma": fee_on,
        "product_terms_refused_with": terms.refusal,
        "fee_in_intent": {k: fee.get(k) for k in ("policy", "enabled", "rate", "exponent", "research_assumption") if k in fee},
    }
    for label, basis in (("default_tier", "explicit_pregame"), ("research_tier", RESEARCH)):
        out = five_league_budget.replay(doc, data_dir=root, entry_basis=basis)
        row[label] = {"outcomes": out["leagues"][league]["outcomes"], "proof_gaps": out["proof_gaps"]}
    return row


rows = [
    run(league, explicit, fee_on)
    for league in ("lal", "bun", "sea", "fl1")
    for explicit in (True, False)
    for fee_on in (False, True)
]


def filled(tier, fee_on):
    return sum(r[tier]["outcomes"].get("filled", 0) for r in rows if r["fee_switch_in_gamma"] is fee_on)


json.dump(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "reviewed_fee_policies": len(fee_registry.REVIEWED),
        "fee_switch_used_when_on": REAL_FEE,
        "entries_filled": {
            "fee_switch_off": {"default_tier": filled("default_tier", False), "research_tier": filled("research_tier", False), "of": 8},
            "fee_switch_on": {"default_tier": filled("default_tier", True), "research_tier": filled("research_tier", True), "of": 8},
        },
        "rows": rows,
    },
    sys.stdout,
    ensure_ascii=False,
    indent=1,
)
print()
