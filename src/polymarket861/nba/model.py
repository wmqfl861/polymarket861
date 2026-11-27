"""Score-only NBA research models. Each observation is a completed game including overtime."""

from collections import defaultdict, deque
from contextlib import closing, nullcontext
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from itertools import groupby
import json
import math

import numpy as np

RECIPE = "nba-final-paired-residual-v1"
MODELS = ("N0", "N1", "N2")
FAMILIES = ("total", "home", "away")
MIN_HISTORY = 30
BANK_SIZE = 1200
TEAM_RATE = 0.065
SEASON_CARRY = 0.60
B2B_MARGIN_POINTS = 1.5
SCOPE = "final_including_overtime"


def instant(value):
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        raise ValueError("NBA cutoff/receipt must include an explicit UTC offset")
    return dt.astimezone(timezone.utc)


def day_of(row):
    return date.fromisoformat(str(row["game_date"]))


def eligible(row):
    return row.get("stage") == "regular" and row.get("status") == "final"


def check_scores(home, away):
    if type(home) is not int or type(away) is not int or min(home, away) < 0 or home == away:
        raise ValueError("a completed NBA game needs two nonnegative, unequal integer final scores")


def _check_fixture(fixture):
    if fixture.get("stage") != "regular":
        raise ValueError("phase one supports NBA regular season only")
    if not fixture.get("game_id") or not fixture.get("home") or not fixture.get("away"):
        raise ValueError("missing NBA game or team identity")
    if fixture["home"] == fixture["away"]:
        raise ValueError("NBA fixture requires two distinct teams")
    if fixture.get("neutral") is not None and type(fixture["neutral"]) is not bool:
        raise ValueError("NBA venue flag must be true, false or unknown")
    if type(fixture.get("season")) is not int:
        raise ValueError("season must be its integer starting year")


def prepare(rows):
    """Caller supplies one adjudicated source version per game."""
    seen = set()
    ordered = []
    for row in rows:
        if not eligible(row):
            continue
        _check_fixture(row)
        check_scores(row["home_score"], row["away_score"])
        if row["game_id"] in seen:
            raise ValueError(f"duplicate final game version: {row['game_id']}")
        seen.add(row["game_id"])
        day_of(row)
        ordered.append(row)
    if len({g.get("source") for g in ordered}) > 1:
        raise ValueError("mixed NBA sources require explicit game and team reconciliation")
    return sorted(ordered, key=lambda g: (g["game_date"], g["game_id"]))


class ScoreState:
    """Online league scoring environment plus opponent-adjusted attack and defence."""

    def __init__(self):
        self.season = None
        self.mean = 100.0
        self.home_edge = 3.0
        self.teams = defaultdict(lambda: [0.0, 0.0])
        self.samples = deque(maxlen=BANK_SIZE)
        self.unknown_site_games = 0
        self.total_games = 0
        self.latest_result = None

    def advance(self, season):
        if self.season is not None and season > self.season:
            carry = SEASON_CARRY ** (season - self.season)
            for vector in self.teams.values():
                vector[0] *= carry
                vector[1] *= carry
        if self.season is not None and season < self.season:
            raise ValueError("NBA season sequence regressed")
        self.season = season

    def centers(self, game, prior_dates):
        home, away = game["home"], game["away"]
        edge = self.home_edge / 2 if game.get("neutral") is False else 0.0
        carry = 1.0 if self.season is None else SEASON_CARRY ** (game["season"] - self.season)
        home_attack, home_conceded = self.teams.get(home, (0.0, 0.0))
        away_attack, away_conceded = self.teams.get(away, (0.0, 0.0))
        home_mean = self.mean + edge + carry * (home_attack + away_conceded)
        away_mean = self.mean - edge + carry * (away_attack + home_conceded)
        previous = day_of(game) - timedelta(days=1)
        contrast = int(prior_dates.get(away) == previous) - int(prior_dates.get(home) == previous)
        shift = 0.5 * B2B_MARGIN_POINTS * contrast
        return (home_mean, away_mean), (home_mean + shift, away_mean - shift)

    def learn_day(self, games, prior_dates):
        if not games:
            return
        self.advance(games[0]["season"])
        # Freeze all expectations before updating even when two games share a team or date.
        predictions = [self.centers(g, prior_dates) for g in games]
        for game, (n1, n2) in zip(games, predictions, strict=True):
            x, y = game["home_score"], game["away_score"]
            e_home, e_away = x - n1[0], y - n1[1]
            self.samples.append((x, y, bool(game.get("neutral")), e_home, e_away,
                                 x - n2[0], y - n2[1]))
            self.teams[game["home"]][0] += TEAM_RATE * e_home / 2
            self.teams[game["away"]][1] += TEAM_RATE * e_home / 2
            self.teams[game["away"]][0] += TEAM_RATE * e_away / 2
            self.teams[game["home"]][1] += TEAM_RATE * e_away / 2
            self.mean += 0.002 * ((x + y) / 2 - self.mean)
            if game.get("neutral") is None:
                self.unknown_site_games += 1
            if game.get("neutral") is False:
                self.home_edge += 0.002 * ((x - y) - self.home_edge)
            self.total_games += 1
        self.latest_result = day_of(games[-1])

    def bank(self):
        if self.total_games < MIN_HISTORY:
            raise ValueError(f"insufficient NBA regular-season finals (minimum {MIN_HISTORY})")
        return np.asarray(self.samples, dtype=float).T

    def distributions(self, game, prior_dates, bank):
        if (day_of(game) - self.latest_result).days > 500:
            raise ValueError("NBA score history is stale for this fixture")
        # N0 uses the league's observed score pairs. Neutral sites have no designated-home edge.
        h, a = bank[:2].astype(int)
        if game.get("neutral") is not False:
            yield np.concatenate((h, a)), np.concatenate((a, h))
        else:
            yield h, a
        for i, (mh, ma) in enumerate(self.centers(game, prior_dates)):
            rh, ra = bank[3 + 2*i:5 + 2*i]
            if game.get("neutral") is not False:
                rh, ra = np.concatenate((rh, ra)), np.concatenate((ra, rh))
            home = np.maximum(0, np.rint(rh - rh.mean() + mh)).astype(int)
            away = np.maximum(0, np.rint(ra - ra.mean() + ma)).astype(int)
            # A final cannot tie. Condition the empirical translated *final* law on this
            # support; never infer an OT period from final-only sources or add OT twice.
            ok = home != away
            if not ok.any():
                raise ValueError("no non-tied empirical final scores")
            yield home[ok], away[ok]


def update_dates(dates, games):
    for game in games:
        for side in ("home", "away"):
            dates[game[side]] = day_of(game)


def _distribution(samples, line):
    values, counts = np.unique(samples, return_counts=True)
    cumulative = counts.cumsum()
    size = len(samples)
    quantiles = [int(values[np.searchsorted(cumulative, q * size)]) for q in (0.1, 0.5, 0.9)]
    result = {"mean": float(np.average(values, weights=counts)),
              "q10": quantiles[0], "q50": quantiles[1], "q90": quantiles[2],
              "distribution": {"values": values.tolist(), "counts": counts.tolist()}}
    if line is not None:
        if isinstance(line, bool) or not math.isfinite(float(line)):
            raise ValueError("invalid NBA points line")
        threshold = float(line)
        result.update(line=threshold, over=float(counts[values > threshold].sum() / size),
                      equal=float(counts[values == threshold].sum() / size),
                      under=float(counts[values < threshold].sum() / size))
    return result


MOMENTS = ("home", "away", "home_squared", "away_squared", "product", "tie", "one_point", "within_three")


def pair_moments(home, away):
    """Sufficient statistics of the integer joint law for archive calibration diagnostics."""
    gap = np.abs(home - away)
    return [float(np.mean(x)) for x in (home, away, home*home, away*away, home*away,
                                        gap == 0, gap == 1, gap <= 3)]


def forecast(state, fixture, prior_dates, bank, lines=None, *, include_joint=False):
    lines = lines or {}
    if set(lines) - set(FAMILIES):
        raise ValueError("unknown points-line family")
    models = {}
    for label, (home, away) in zip(MODELS, state.distributions(fixture, prior_dates, bank), strict=True):
        p = float(np.mean(home > away))
        models[label] = {"p_home_win": p, "p_away_win": 1-p, "joint_samples": len(home),
                         "pair_moments": dict(zip(MOMENTS, pair_moments(home, away), strict=True)),
                         "total": _distribution(home + away, lines.get("total")),
                         "home": _distribution(home, lines.get("home")),
                         "away": _distribution(away, lines.get("away"))}
        if include_joint:
            pairs, counts = np.unique(np.column_stack((home, away)), axis=0, return_counts=True)
            models[label]["joint_distribution"] = {"home": pairs[:, 0].tolist(), "away": pairs[:, 1].tolist(),
                                                   "counts": counts.tolist()}
    site = {False: "confirmed_non_neutral", True: "confirmed_neutral", None: "unknown_home_advantage_withheld"}
    return {"recipe": RECIPE, "scope": SCOPE, "game_id": fixture["game_id"],
            "venue_basis": site[fixture.get("neutral")], "training_unknown_site_games": state.unknown_site_games,
            "season": fixture["season"], "game_date": fixture["game_date"],
            "home": fixture["home"], "away": fixture["away"],
            "training_games": state.total_games, "last_training_date": state.latest_result.isoformat(),
            "residual_bank_games": len(state.samples), "models": models, "paper_eligible": False}


def _received_by(row, cutoff):
    return (row.get("source") != "538" and row.get("evidence_grade") != "final_archive_date_only"
            and row.get("received_at") and row.get("available_at")
            and max(instant(row["received_at"]), instant(row["available_at"])) <= cutoff)


def predict(history, fixture, *, lines=None, as_of=None):
    """Fit on prior-day results. As-of mode requires actual received and usable receipts."""
    _check_fixture(fixture)
    target = day_of(fixture)
    cutoff = instant(as_of) if as_of is not None else None
    if cutoff:
        if (fixture.get("status") != "scheduled" or not fixture.get("kickoff")
                or not _received_by(fixture, cutoff)):
            raise ValueError("NBA fixture needs a received, usable pregame schedule")
        if cutoff >= instant(fixture["kickoff"]):
            raise ValueError("NBA cutoff must precede scheduled tipoff")
    candidates = []
    prior_schedule = {}
    for g in history:
        if g["game_id"] == fixture["game_id"] or g.get("stage") != "regular" or day_of(g) >= target:
            continue
        if g.get("source") != fixture.get("source") or (cutoff and not _received_by(g, cutoff)):
            continue
        if eligible(g) and cutoff and g.get("kickoff") and instant(g["kickoff"]) >= cutoff:
            continue
        if g.get("status") in ("scheduled", "live", "final"):
            when = day_of(g)
            for team in (g["home"], g["away"]):
                prior_schedule[team] = max(when, prior_schedule.get(team, when))
        if eligible(g):
            candidates.append(g)
    state = ScoreState()
    dates = {}
    ordered = prepare(candidates)
    for _, group in groupby(ordered, key=day_of):
        games = list(group)
        state.learn_day(games, dates)
        update_dates(dates, games)
    result = forecast(state, fixture, prior_schedule, state.bank(), lines, include_joint=True)
    scoring_fields = ("game_id", "game_date", "season", "home", "away", "neutral", "home_score", "away_score",
                      "version", "receipt_sha256", "available_at")
    inputs = {"scores": [[g.get(key) for key in scoring_fields] for g in ordered],
              "schedule_rest": {team: prior_schedule[team].isoformat() for team in (fixture["home"], fixture["away"])
                                if team in prior_schedule}}
    result["training_view_sha256"] = sha256(json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result["as_of"] = cutoff.isoformat() if cutoff else None
    result["evidence_grade"] = "receipt_filtered" if cutoff else "technical_archive"
    return result


def score(arm, home_score, away_score):
    """Exact empirical CRPS via E|X-y| - E|X-X'|/2, for arbitrary sparse integer support."""
    check_scores(home_score, away_score)
    losses = {"brier_win": (arm["p_home_win"] - int(home_score > away_score)) ** 2}
    for key, actual in zip(FAMILIES, (home_score + away_score, home_score, away_score), strict=True):
        histogram = arm[key]["distribution"]
        values = np.asarray(histogram["values"], dtype=float)
        counts = np.asarray(histogram["counts"], dtype=float)
        probs = counts / counts.sum()
        left = probs.cumsum()[:-1]
        spread = np.dot(np.diff(values), left * (1-left))
        losses["crps_" + key] = float(np.dot(abs(values - actual), probs) - spread)
    return losses


def register(subparsers):
    from . import model_replay

    actions = subparsers.add_parser("model", help="NBA score-only forecasts and historical replay").add_subparsers(
        dest="nba_model_command", required=True)
    p = actions.add_parser("predict", help="Predict a game in the received schedule")
    p.add_argument("game_id")
    p.add_argument("--as-of", required=True, help="UTC cutoff, with offset")
    for key in FAMILIES:
        p.add_argument("--" + key + "-line", type=float)
    p.set_defaults(func=_predict_command)
    model_replay.register(actions)


def _predict_command(args, *, clock=None):
    from ..store import iso, now_utc
    from . import data, operations, storage

    cutoff = instant(args.as_of)
    if cutoff > instant((clock or now_utc)()):
        raise ValueError("NBA preview cutoff cannot be in the future")
    as_of = iso(cutoff)
    versions = data.game_versions(args.data_dir, as_of=as_of, game_id=args.game_id)
    epoch = operations.require_active_epoch(versions)
    game = epoch["latest"]
    db = storage.open_existing(args.data_dir)
    with closing(db) if db is not None else nullcontext():
        try:
            proof = operations._schedule_proof(args.data_dir, db, game, as_of)
            eligibility = "verified_schedule_preview"
        except operations.ScheduleUnverified as exc:
            eligibility = "exploratory_unverified_schedule"
            proof = {"state": "unverified", "reason": str(exc)}
    history = data.games_as_of(args.data_dir, as_of=as_of, source="balldontlie")
    lines = {key: getattr(args, key + "_line") for key in FAMILIES}
    result = predict(history, game, as_of=cutoff, lines=lines)
    result.update(preview_eligibility=eligibility, schedule_verification=proof, schedule_epoch=epoch["game"]["version"],
                  schedule_version=game["version"], schedule_available_at=game["available_at"],
                  sealed=False, forward_scoring_eligible=False)
    return result
