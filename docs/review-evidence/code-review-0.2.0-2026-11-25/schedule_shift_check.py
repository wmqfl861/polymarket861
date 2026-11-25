"""估算有多少比赛会超出“赛程自动修正最多移动 36 小时”的上限。

0.2.0 在 OpenLigaDB 与交易所两个来源一致时自动修正未来比赛的开球时刻，但单次移动不超过 36 小时
（schedule_consensus.MAX_SHIFT），超出的进入人工审核。随仓报告显示，登记的未来赛程是占位时刻
（2026/27 第 12 轮的 10 场全部登记为周六 15:00）。本脚本用过去五个赛季的真实开球时刻回答：
如果每一轮都以“该轮最集中的那个时段”作占位，有多少场的真实开球时刻离占位超过 36 小时。

占位的取法：取该轮比赛最多的那一天。那天是周六就用周六 15:00（伦敦时间），否则用那天最常见的开球时刻。
只读 `pm861 football fetch` 下载的 OpenFootball 赛季文件，不导入本项目的包，不写任何文件。

用法：python3 -I schedule_shift_check.py INPUT目录 [赛季 ...]
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
LIMIT_HOURS = 36
root = Path(sys.argv[1])
seasons = sys.argv[2:] or ["2021-22", "2022-23", "2023-24", "2024-25", "2025-26"]
total = Counter()
print("赛季      超出±36小时   其中同一比赛周内偏晚(36–96h)  改到别的比赛周(>96h)  偏早(<−36h)  受影响轮数")
for season in seasons:
    rounds = defaultdict(list)
    for match in json.loads((root / season / "en.1.json").read_text(encoding="utf-8"))["matches"]:
        when = datetime.fromisoformat(match["date"] + "T" + (match.get("time") or "15:00")).replace(tzinfo=LONDON)
        rounds[match["round"]].append(when)
    far = late_same_week = moved = early = affected = 0
    for kicks in rounds.values():
        day = Counter(k.date() for k in kicks).most_common(1)[0][0]
        if day.weekday() == 5:
            anchor = datetime(day.year, day.month, day.day, 15, 0, tzinfo=LONDON)
        else:
            anchor = Counter(k for k in kicks if k.date() == day).most_common(1)[0][0]
        offsets = [(k - anchor).total_seconds() / 3600 for k in kicks]
        outside = [o for o in offsets if abs(o) > LIMIT_HOURS]
        far += len(outside)
        affected += bool(outside)
        late_same_week += sum(LIMIT_HOURS < o <= 96 for o in outside)
        moved += sum(o > 96 for o in outside)
        early += sum(o < -LIMIT_HOURS for o in outside)
    n = sum(len(k) for k in rounds.values())
    total.update(n=n, far=far, late=late_same_week, moved=moved, early=early)
    print(f"{season}   {far:>3} / {n} ({far / n:5.1%})        {late_same_week:>3}                        {moved:>3}                {early:>3}          {affected} / {len(rounds)}")
print(f"合计      {total['far']} / {total['n']} ({total['far'] / total['n']:.1%})        {total['late']:>3}                        {total['moved']:>3}                {total['early']:>3}")
