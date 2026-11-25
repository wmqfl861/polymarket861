"""查看固定版本的 OpenFootball 赛季文件里，哪些轮次登记的还是占位开球时刻。

系统用这份文件登记赛程。赛果准入要求来源给出的开球时刻与登记时刻完全相等
（result_admission._anchor），不相等的赛果会被隔离为 schedule_or_round_mismatch。
未来比赛的占位时刻可以由 0.2.0 的赛程修正在开球前改正，已经踢完的轮次没有这条路。
所以“整轮 10 场登记在同一个时刻”的已赛轮次，凡是没有恰好在那个时刻开球的比赛，赛果都进不来。

只读 `pm861 football fetch` 下载的赛季文件，不导入本项目的包，不写任何文件。

用法：python3 -I placeholder_rounds_check.py INPUT目录 [赛季=2026-27]
"""

import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
season = sys.argv[2] if len(sys.argv) > 2 else "2026-27"
rounds = {}
for match in json.loads((root / season / "en.1.json").read_text(encoding="utf-8"))["matches"]:
    rounds.setdefault(match["round"], []).append(match)

print("轮次          场数  有比分  不同开球时刻数  说明")
placeholders = []
for name in sorted(rounds, key=lambda text: int(text.split()[-1])):
    games = rounds[name]
    slots = Counter((g["date"], g.get("time")) for g in games)
    scored = sum(1 for g in games if g.get("score"))
    note = ""
    if len(slots) == 1 and len(games) > 1:
        (date, time), _ = next(iter(slots.items()))
        note = f"占位：全部登记在 {date} {time}"
        placeholders.append(int(name.split()[-1]))
    print(f"{name:<12} {len(games):>4}  {scored:>5}  {len(slots):>12}    {note}")
print()
if placeholders:
    print(f"从第 {placeholders[0]} 轮起出现整轮占位，共 {len(placeholders)} 轮：" + "、".join(map(str, placeholders)))
else:
    print("没有整轮占位的轮次")
