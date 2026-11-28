"""Local CLI. All market operations use public data or simulated balances."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__


def main(argv=None):
    from . import (
        big5_scheduler,
        dashboard,
        datasets,
        evidence,
        five_league_budget,
        football,
        jobs,
        league_catalog,
        ledger,
        maintenance,
        nba,
        market_archive,
        news,
        news_market,
        operations,
        opportunity,
        pipeline,
        result_feed,
        runner,
        season,
        settlement,
        shadow,
        strategy,
    )

    parser = argparse.ArgumentParser(
        prog="pm861", description="五大联赛和 NBA 赛前预测、评分与只读市场研究"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="私有运行目录，保存 SQLite、档案及作业",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for module in (
        nba,
        datasets,
        league_catalog,
        big5_scheduler,
        news_market,
        maintenance,
        market_archive,
        five_league_budget,
        football,
        evidence,
        news,
        ledger,
        jobs,
        pipeline,
        runner,
        season,
        settlement,
        shadow,
        strategy,
        result_feed,
        dashboard,
        operations,
        opportunity,
    ):
        module.register(commands)
    args = parser.parse_args(argv)
    try:
        from .nba import storage

        if args.command == "nba":
            if args.data_dir is None:
                args.data_dir = Path(os.environ.get("PM861_NBA_DATA_DIR", Path.home() / ".polymarket861-nba"))
            storage.check_nba_root(args.data_dir)
        else:
            if args.data_dir is None:
                args.data_dir = Path(os.environ.get("PM861_DATA_DIR", Path.home() / ".polymarket861"))
            storage.check_football_root(args.data_dir)
        result = args.func(args)
        if result is not None:
            print(
                json.dumps(
                    result, ensure_ascii=False, indent=2, allow_nan=False, default=str
                )
            )
        if hasattr(args, "result_exit_code"):
            return args.result_exit_code(result)
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"pm861: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
