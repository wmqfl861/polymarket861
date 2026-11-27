#!/usr/bin/env python3
"""四国新闻链接（league_press）是按什么字样把一篇报道算到某家俱乐部头上的。

league_press 每 15 分钟读一期 GDELT GKG 英文索引，只看两处：文章网址的路径、索引里的机构名。
命中的字样来自 _clubs()：俱乐部登记全名、ALIASES 里的别名、去掉结尾 FC/AFC 的写法，
并且只保留“归一化后至少两个词”的字样。这个探针做两件事：

一、把 96 家俱乐部各自能命中的字样列出来，数一数有多少家只有“带公司后缀的全名”这一种写法
    （例如 juventus fc、ssc napoli、sevilla fc），英文报道里常见的单名（Juventus、Napoli、Sevilla）
    因为只有一个词而不在表里。

二、拿一组自己写的网址过一遍真实的 _parse，看哪些被算到俱乐部头上、哪些没有。
    网址是为了说明匹配规则而写的，域名用 example.com，不代表真实报道，也不代表真实的命中比例。

用法：
  python -I probe_league_press_labels.py --src <仓库>/src

不联网，不读写仓库，不触碰任何真实数据目录。
"""

import argparse
import io
import json
import sys
import zipfile

parser = argparse.ArgumentParser()
parser.add_argument("--src", required=True)
args = parser.parse_args()
sys.path.insert(0, args.src)

import polymarket861  # noqa: E402
from polymarket861 import league_press, league_registry  # noqa: E402

SEASON = "2026-27"
names = league_press._clubs(SEASON)
by_club = {}
for label, club in names.items():
    by_club.setdefault(club, []).append(label)

leagues = {}
only_formal = {}
for league in league_registry.PROFILES:
    members = league_registry.members(league, SEASON)
    table = {team: sorted(by_club.get((league, team), [])) for team in members}
    # “只有正式全名”：能命中的字样全部等于登记全名本身的归一化写法。
    formal = [team for team, labels in table.items() if set(labels) <= {league_press._words(team)}]
    leagues[league] = {"clubs": len(members), "labels": table}
    only_formal[league] = {"clubs_matched_only_by_their_full_registered_name": len(formal), "which": formal}

CASES = [
    # 英文报道常见的单名写法，谈的确实是这场球
    ("football: Juventus v Napoli preview", "https://example.com/football/juventus-napoli-preview-serie-a", ""),
    ("football: Barcelona v Sevilla team news", "https://example.com/sport/barcelona-vs-sevilla-team-news-injuries", ""),
    ("football: Lyon v Lille report", "https://example.com/sport/football/lyon-lille-match-report", ""),
    ("football: Werder Bremen v Stuttgart", "https://example.com/bundesliga/werder-bremen-stuttgart-lineups", ""),
    ("football: Dortmund injury update", "https://example.com/soccer/borussia-dortmund-injury-update", ""),
    ("football: PSG, club named in the index", "https://example.com/sport/psg-squad-news", "Paris Saint-Germain"),
    ("football: Real Madrid", "https://example.com/sport/real-madrid-injury-list-grows", ""),
    # 字样相同、说的不是这家俱乐部
    ("motor racing: 24 Hours of Le Mans", "https://example.com/motorsport/24-hours-of-le-mans-entry-list", ""),
    ("travel: weekend in Le Mans", "https://example.com/travel/a-weekend-in-le-mans-old-town", ""),
    ("finance: tax havens such as Monaco", "https://example.com/business/tax-havens-such-as-monaco-face-new-rules", ""),
    ("society: people known as Roma", "https://example.com/world/europe/minority-known-as-roma-housing-report", ""),
    ("basketball: Real Madrid EuroLeague", "https://example.com/basketball/euroleague-real-madrid-beat-olympiacos", ""),
    ("women's football: Manchester United Women", "https://example.com/womens-football/manchester-united-women-wsl", ""),
]

EDITION = "20261127000000"
lines = []
for n, (_, url, orgs) in enumerate(CASES):
    fields = [f"{EDITION}-{n}", EDITION, "1", "example.com", url, *[""] * 8, orgs, ""]
    lines.append("\t".join(fields))
raw = io.BytesIO()
with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr(EDITION + ".gkg.csv", "\n".join(lines) + "\n")
scanned, rows = league_press._parse(raw.getvalue(), EDITION, "2026-11-27T00:01:00.000000Z", names)
hits = {}
for (league, team, url), doc in rows.items():
    hits.setdefault(url, []).append({"league": league, "club": team, "label": doc["club_label"], "via": doc["matched_via"]})

print(json.dumps(
    {
        "package_version": polymarket861.__version__,
        "python": sys.version.split()[0],
        "season": SEASON,
        "clubs": sum(v["clubs"] for v in leagues.values()),
        "labels": len(names),
        "single_word_labels": sum(" " not in label for label in names),
        "only_full_registered_name": only_formal,
        "synthetic_cases": [
            {"case": title, "url": url, "indexed_organizations": orgs, "attributed_to": hits.get(url, [])}
            for title, url, orgs in CASES
        ],
        "label_table": {league: v["labels"] for league, v in leagues.items()},
    },
    indent=1, ensure_ascii=False,
))
