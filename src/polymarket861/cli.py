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
        dashboard,
        datasets,
        evidence,
        football,
        jobs,
        ledger,
        maintenance,
        market_archive,
        operations,
        opportunity,
        pipeline,
        result_feed,
        runner,
        strategy,
    )

    parser = argparse.ArgumentParser(
        prog="pm861", description="英超赛前预测、逐轮评分与 Polymarket 模拟账本"
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("PM861_DATA_DIR", Path.home() / ".polymarket861")),
        help="私有运行目录，保存 SQLite、档案及作业",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for module in (
        datasets,
        maintenance,
        market_archive,
        football,
        evidence,
        ledger,
        jobs,
        pipeline,
        runner,
        strategy,
        result_feed,
        dashboard,
        operations,
        opportunity,
    ):
        module.register(commands)
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if result is not None:
            print(
                json.dumps(
                    result, ensure_ascii=False, indent=2, allow_nan=False, default=str
                )
            )
    except (ValueError, KeyError, OSError, sqlite3.Error) as exc:
        print(f"pm861: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0
