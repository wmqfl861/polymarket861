"""NBA research. Uses its own data directory, models, and market observations."""


def fixtures(root, day, as_of):
    from ..store import iso, now_utc
    from .data import games_as_of

    cutoff = iso(as_of or now_utc())
    fields = ("game_id", "version", "kickoff", "home_abbr", "away_abbr", "home_name",
              "away_name", "stage", "status", "home_score", "away_score", "available_at")
    observed = [game for game in games_as_of(root, cutoff) if game["game_date"] == str(day)]
    observed.sort(key=lambda g: (g.get("kickoff") is None, g.get("kickoff") or "", g["game_id"]))
    return {"game_date": str(day), "as_of": cutoff, "source": "balldontlie",
            "observed_games": len(observed), "games": [{k: g.get(k) for k in fields} for g in observed]}


def register(commands):
    from datetime import date
    from . import data, markets, model, operations

    parser = commands.add_parser("nba", help="NBA 赛前研究、免费数据和只读盘口")
    actions = parser.add_subparsers(dest="nba_command", required=True)
    for module in (data, markets, model, operations):
        module.register(actions)
    games = actions.add_parser("fixtures", help="列出本机已收到的美东比赛日赛程和预测用比赛 ID")
    games.add_argument("--date", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD")
    games.add_argument("--as-of", help="只显示在此 UTC 时刻已收到的赛程版本")
    games.set_defaults(func=lambda a: fixtures(a.data_dir, a.date, a.as_of))
