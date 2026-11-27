#!/usr/bin/env python3
"""四国“伤停情景”（league_player_status.absence_scenario）对不同球员的缺阵各算多大。

absence_scenario 只在配置了 API-Football 密钥、来源明确标了 Missing Fixture、并且球员全名能在
独立名单里唯一对上时才生效，结果标为“未校准”，不进配对预测。它对每名缺阵球员用的是
news_model.POLICY 里的 reported/out 权重（0.45）乘以位置效应，没有英超新闻模型里的
“重要性系数”（按出场分钟和首发算，0.25 到 1.0，查不到时取 0.55）和“持续时间折减”。

这个探针直接调用 absence_scenario，比较几种名单：
  一、主力前锋一人缺阵；二、同队第三门将一人缺阵；三、同一位置的边缘球员一人缺阵；
  四、一份常见长度的缺阵名单（两名前锋、两名中场、一名后卫、一名门将，共六人）。
并在旁边按英超口径手算同一份名单乘上 0.25、0.55、1.0 三档重要性系数后的进球率变动，
作为对照（这一列是按 POLICY 常数推算的，不是英超代码的输出）。

基准进球率取主队 1.50、客队 1.10、rho 0.01，自己设的，只为看相对大小。

用法：
  python -I probe_absence_scenario_weighting.py --src <仓库>/src

不联网，不读写仓库，不触碰任何真实数据目录。球员名字是编的。
"""

import argparse
import json
import math
import sys
from datetime import timedelta

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import football_model, league_player_status as source, news_model, store  # noqa: E402

NOW = store.instant("2026-11-28T18:30:00Z")
KICK = NOW + timedelta(hours=1)
matrix, _ = football_model._goals_matrix(1.50, 1.10, 0.01)
probs, totals = football_model.probabilities(matrix)
BASE = {"model": "B1", "rates": {"home": 1.50, "away": 1.10, "rho": 0.01},
        "kickoff": store.iso(KICK), "totals": totals, "probabilities": probs}

SQUAD = [
    ("Anton Keller", "GK"), ("Bruno Marek", "GK"), ("Casimir Lund", "GK"),
    ("Dario Welle", "DF"), ("Emil Horvat", "DF"),
    ("Felix Brandt", "MF"), ("Goran Ilse", "MF"),
    ("Henrik Sommer", "FW"), ("Ivo Tanner", "FW"), ("Jonas Reuter", "FW"),
]
ROSTER = {"players": [{"name": n, "source_position": p} for n, p in SQUAD],
          "ready_at": store.iso(NOW - timedelta(hours=2)), "evidence": [{"source_sha256": "a" * 64}]}


def scenario(absent):
    players = [{"player_id": 1000 + i, "name": name, "provider_type": "Missing Fixture"}
               for i, name in enumerate(absent)]
    snapshot = {
        "player_status": {
            "source": source.SOURCE,
            "injuries": {"state": "observed", "received_at": store.iso(NOW - timedelta(minutes=5)),
                         "provider_fixture_id": 1, "teams": {"home": {"players": players}, "away": {"players": []}}},
            "lineup": {"state": "pending", "teams": {}},
        },
        "teams": {"home": {"roster": ROSTER}, "away": {"roster": {}}},
    }
    out = source.absence_scenario(BASE, snapshot, cutoff=NOW)
    roles = [a["role"] for a in out["applied"]]
    policy = news_model.POLICY
    weight = policy["absence_weight"]["reported"]["out"]
    own = sum(policy["role_log_rate_effects"][r][0] for r in roles)
    opp = sum(policy["role_log_rate_effects"][r][1] for r in roles)
    bound = policy["max_abs_log_adjustment_per_team"]

    def pct(shift):
        return round(100 * (math.exp(max(-bound, min(bound, shift))) - 1), 2)

    return {
        "absent": absent,
        "status": out["status"],
        "applied_roles": roles,
        "excluded": out["excluded"],
        "home_rate_change_percent": round(100 * (out.get("rates", BASE["rates"])["home"] / 1.50 - 1), 2),
        "away_rate_change_percent": round(100 * (out.get("rates", BASE["rates"])["away"] / 1.10 - 1), 2),
        "home_win_change_points": round(100 * (out["probabilities"]["home_win"] - probs["home_win"]), 2),
        "away_win_change_points": round(100 * (out["probabilities"]["away_win"] - probs["away_win"]), 2),
        "same_list_under_epl_importance_multiplier": {
            str(m): {"home_rate_change_percent": pct(weight * m * own), "away_rate_change_percent": pct(weight * m * opp)}
            for m in (policy["importance"]["minimum_multiplier"], policy["importance"]["neutral_multiplier"],
                      policy["importance"]["maximum_multiplier"])
        },
    }


print(json.dumps(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "base": {"home_rate": 1.50, "away_rate": 1.10, "probabilities": {k: round(v, 4) for k, v in probs.items()
                                                                       if k in ("home_win", "draw", "away_win")}},
        "policy": {
            "reported_out_weight": news_model.POLICY["absence_weight"]["reported"]["out"],
            "role_log_rate_effects": news_model.POLICY["role_log_rate_effects"],
            "cap_per_team": news_model.POLICY["max_abs_log_adjustment_per_team"],
            "epl_importance_multiplier_range": [news_model.POLICY["importance"]["minimum_multiplier"],
                                                news_model.POLICY["importance"]["maximum_multiplier"]],
            "epl_importance_neutral": news_model.POLICY["importance"]["neutral_multiplier"],
        },
        "scenarios": {
            "first_choice_forward": scenario(["Henrik Sommer"]),
            "third_choice_goalkeeper": scenario(["Casimir Lund"]),
            "fringe_forward": scenario(["Jonas Reuter"]),
            "six_absentees": scenario(["Henrik Sommer", "Ivo Tanner", "Felix Brandt", "Goran Ilse",
                                       "Dario Welle", "Casimir Lund"]),
        },
    },
    indent=1, ensure_ascii=False,
))
