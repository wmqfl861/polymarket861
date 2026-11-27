"""Isolate input availability for the installed forecast service acceptance test."""

import os
from pathlib import Path

from polymarket861 import league_live_inputs


def controlled_inputs(state_root, *, league, football_root, market_state_dir, as_of=None):
    if league == "sea" and Path(os.environ["PM861_CI_FORECAST_OUTAGE"]).exists():
        raise OSError("simulated Italian input outage")
    return league_live_inputs.LeagueInputs((), (), {"league": league, "market_fixtures": {}})


league_live_inputs.load_inputs = controlled_inputs
