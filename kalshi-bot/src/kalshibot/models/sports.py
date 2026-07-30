"""Sports prediction model + game-market parsing.

This is the sports counterpart of `models/weather.py`, built on the same idea
rather than a new one. The weather model treats the settlement temperature as
Normally distributed around an ensemble of forecast highs and integrates that
distribution over the market's threshold. Here the continuous driver is the
GAME MARGIN (home score minus away score): each rating member predicts an
expected margin, the members are averaged, their disagreement widens the
distribution, and P(YES) comes from integrating it over the winning threshold.

    driver           weather: daily high (deg F)   sports: margin (runs/points)
    ensemble member  one NWP model's forecast      one rating method's margin
    base sigma       forecast error                irreducible game randomness
    spread           model disagreement            method disagreement
    observation      METAR temperature so far      live score + period so far
    threshold        "97 or above"                 "margin >= 1", i.e. a win

The live-score clamp is the sports analog of the weather model's "the day's high
can only be at or above what has already been observed", and is the same kind of
statement: the final margin is the current lead PLUS whatever happens in the
remainder, so as the game progresses the lead becomes decisive and the remaining
uncertainty shrinks with the square root of the time left.

Everything the model used is persisted in `inputs`, so a wrong call can be read
back rather than guessed at. Where the data is insufficient -- no members, an
unparseable ticker, a game underway with a stale score feed -- the model returns
None (no opinion) instead of guessing.
"""
from __future__ import annotations

import math
import re
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from ..config import LeagueConfig
from ..storage.dao import SportsStateRow
from .base import MarketBase, MarketContext, Prediction, PredictionModel

# Margins are whole numbers, so "home wins" is "margin >= 1" and the continuous
# distribution is integrated at the half-integer boundary between a 0 and a 1
# margin -- the same exclusive-threshold care the weather model takes with
# "97 or above". Getting this wrong biases every game by half a run.
_WIN_BOUNDARY = 0.5

# Probabilities are clamped before being converted into a margin: the mapping
# diverges at 0 and 1, and a member claiming certainty would otherwise pin the
# ensemble mean at the end of the bisection range.
_P_CLAMP = 0.001


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def margin_from_prob(p_home: float, margin_sigma: float) -> float:
    """Expected home margin implied by a home win probability.

    The single link between "probability" and "margin" in the whole sports
    vertical: a member that natively speaks probabilities (Elo, log5) converts
    here, a member that natively speaks margins (a bookmaker spread) does not.

    This is the exact inverse of `prob_home_from_margin`, found by bisection
    (which that function's monotonicity in mu guarantees). The closed-form
    shortcut `sigma * ppf(p)` is NOT used: it ignores the tie-mass
    renormalisation, and the resulting error pushes every member a point or so
    further from 50/50 than it meant to be -- always in the direction of more
    confidence, which is the wrong way for this harness to be wrong.
    """
    p = min(1.0 - _P_CLAMP, max(_P_CLAMP, float(p_home)))
    lo, hi = -8.0 * margin_sigma - 1.0, 8.0 * margin_sigma + 1.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if prob_home_from_margin(mid, margin_sigma) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def prob_home_from_margin(mu: float, sigma: float) -> float:
    """P(home wins) for a margin ~ Normal(mu, sigma), excluding drawn margins.

    A margin of exactly 0 is a tie. Baseball has none (extra innings), the NFL
    has them at well under 1% of games and resolves both sides of a "Winner?"
    market to No when one happens. Rather than pretend the tie mass belongs to
    somebody, it is removed and the two win probabilities are renormalised --
    which keeps P(YES on home) + P(YES on away) = 1, the invariant the two
    Kalshi markets on a game actually satisfy.
    """
    if sigma <= 0:
        return 1.0 if mu > 0 else 0.0
    p_home = 1.0 - _normal_cdf(_WIN_BOUNDARY, mu, sigma)
    p_away = _normal_cdf(-_WIN_BOUNDARY, mu, sigma)
    total = p_home + p_away
    if total <= 0:
        return 0.5
    return min(1.0, max(0.0, p_home / total))


# --------------------------------------------------------------------------
# Market parsing
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SportsMarket(MarketBase):
    """One side of a Kalshi game-winner market.

    `game_key` is the Kalshi event ticker shared by both sides of the game, and
    is the join key for every sports table. `yes_team` is the team that must win
    for THIS market to resolve YES.
    """
    league: str
    game_key: str            # e.g. KXMLBGAME-26JUL282210SEALAD
    yes_team: str            # Kalshi team code, e.g. SEA
    pair_code: str           # away+home concatenated, e.g. SEALAD
    target_date: str         # YYYY-MM-DD, Eastern calendar date of the game
    start_ts: Optional[int]  # scheduled start (ms), when the ticker carries a time


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}

# KXMLBGAME-26JUL282210SEALAD-SEA  -> date 26JUL28, time 2210, pair SEALAD, yes SEA
# KXNFLGAME-26AUG15DALSEA-SEA      -> date 26AUG15, no time,   pair DALSEA, yes SEA
#
# VERIFIED against live Kalshi metadata (2026-07): MLB game tickers carry a
# 4-digit Eastern start time, NFL ones do not. Team codes are letters only, so
# a leading run of 4 digits after the date is unambiguously a time.
_SPORTS_TICKER_RE = re.compile(
    r"^(?P<series>[A-Z0-9]+)-(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<dd>\d{2})"
    r"(?P<time>\d{4})?(?P<pair>[A-Z]{4,10})-(?P<team>[A-Z]{2,4})$"
)

# Kalshi's sports timestamps are Eastern ("...originally scheduled for Jul 28,
# 2026 at 10:10 PM EDT"). Fixed -4h is EDT; the parsed start time is only used
# to pick between two same-day games (doubleheaders) and to know whether a game
# should have begun, so an hour of winter-time error is harmless -- and the
# authoritative start time comes from the linked ESPN event anyway.
_EASTERN_UTC_OFFSET_HOURS = 4


@dataclass(frozen=True)
class TickerParts:
    series: str
    target_date: str
    start_ts: Optional[int]
    pair_code: str
    yes_team: str


def parse_sports_ticker(ticker: str) -> Optional[TickerParts]:
    """Split a Kalshi game-winner ticker into its parts, or None if it is not one.

    Deliberately total: anything that does not match the verified shape -- a
    totals market, a futures market, a new sport with a different layout -- is
    declined here so the model never forms an opinion about a market it does not
    understand.
    """
    m = _SPORTS_TICKER_RE.match(ticker or "")
    if not m:
        return None
    month = _MONTHS.get(m.group("mon").upper())
    if month is None:
        return None
    try:
        day = date(2000 + int(m.group("yy")), month, int(m.group("dd")))
    except ValueError:
        return None

    start_ts: Optional[int] = None
    hhmm = m.group("time")
    if hhmm:
        hh, mm = int(hhmm[:2]), int(hhmm[2:])
        if hh > 23 or mm > 59:
            return None
        start = datetime(day.year, day.month, day.day, hh, mm,
                         tzinfo=timezone.utc) + timedelta(hours=_EASTERN_UTC_OFFSET_HOURS)
        start_ts = int(start.timestamp() * 1000)

    pair, team = m.group("pair"), m.group("team")
    # The YES team must be one end of the pair; otherwise this is some other
    # market shape wearing a similar ticker.
    if not (pair.startswith(team) or pair.endswith(team)):
        return None
    return TickerParts(series=m.group("series"), target_date=day.isoformat(),
                       start_ts=start_ts, pair_code=pair, yes_team=team)


def parse_game_market(raw: dict[str, Any], league: LeagueConfig) -> Optional[SportsMarket]:
    """Parse a raw Kalshi sports market into a typed SportsMarket.

    Which team YES pays out on comes from the ticker suffix, not from the
    sub-title fields: on live data BOTH `yes_sub_title` and `no_sub_title` carry
    the same team name (verified on KXMLBGAME, 2026-07), so trusting them would
    make the two sides of a game indistinguishable. `rules_primary` names the
    YES team in prose and is kept in `inputs` for auditing, but the ticker is
    the machine-readable authority.

    Which team is at HOME is deliberately NOT decided here: the pair segment is
    two variable-length codes concatenated (SEA+LAD, AZ+WSH), which cannot be
    split unambiguously in general. The collector resolves it from the linked
    ESPN event, and the model reads it from `sports_games`.
    """
    ticker = raw.get("ticker")
    if not ticker:
        return None
    parts = parse_sports_ticker(ticker)
    if parts is None:
        return None
    event_ticker = raw.get("event_ticker") or ticker.rsplit("-", 1)[0]
    return SportsMarket(
        ticker=ticker,
        series=league.kalshi_series,
        league=league.name,
        game_key=event_ticker,
        yes_team=parts.yes_team,
        pair_code=parts.pair_code,
        target_date=parts.target_date,
        start_ts=parts.start_ts,
    )


# --------------------------------------------------------------------------
# The model
# --------------------------------------------------------------------------
def fraction_remaining(state: Optional[SportsStateRow], regulation_periods: int) -> float:
    """How much of the game is still to be played, in [0, 1].

    Coarse by design -- whole periods, from the score feed's `period` field,
    which is the one in-game quantity every sport in the feed reports the same
    way. A finished game returns 0. A game in extra innings / overtime returns a
    small floor rather than 0: the score can still change, and a tied game there
    is genuinely a coin flip, which is what a tiny remaining fraction produces.
    """
    if state is None:
        return 1.0
    if state.completed or state.state == "post":
        return 0.0
    if state.state == "pre" or not state.period:
        return 1.0
    completed_periods = max(0, int(state.period) - 1)
    remaining = (regulation_periods - completed_periods) / float(regulation_periods)
    return min(1.0, max(0.02, remaining))


class SportsEnsembleModel(PredictionModel):
    """Ensemble game model.

    Combines every rating member (Elo form, season log5, pythagorean run
    differential, optionally a sportsbook line) into a single distribution over
    the game's final margin, with a spread that GROWS when the members disagree.
    Then it folds in what has actually happened so far (the live score), which
    both shifts the distribution by the current lead and shrinks it as the game
    runs out.

        effective_sigma = sqrt((margin_sigma * sqrt(f))^2 + (spread * f)^2)
          margin_sigma = irreducible per-game randomness (config margin_sigma)
          spread       = std-dev of the members' predicted margins (disagreement)
          f            = fraction of the game still to be played

    The two terms scale differently on purpose: game randomness accumulates like
    a random walk (sqrt of the time left), while a systematic disagreement about
    which team is better only matters over the innings still to come (linear).

    HONEST CAVEAT, and the reason `min_sigma_floor` exists: the weather
    ensemble's members are genuinely independent physical models, so their
    disagreement is a fair proxy for uncertainty. The rating members here all
    read the same season of the same games from the same feed, so they agree far
    more than they are jointly right. Their spread understates real uncertainty;
    the floor stops a false consensus from producing a confident probability.
    """
    name = "sports_ensemble"

    def __init__(self, leagues: list[LeagueConfig], *, min_sigma_floor: float = 0.0,
                 max_state_age_seconds: int = 900) -> None:
        self._leagues = {lg.name: lg for lg in leagues}
        self.min_sigma_floor = float(min_sigma_floor)
        self.max_state_age_ms = int(max_state_age_seconds) * 1000

    def predict(self, market: MarketBase, context: MarketContext) -> Optional[Prediction]:
        if not isinstance(market, SportsMarket):
            return None
        league = self._leagues.get(market.league)
        if league is None:
            return None

        view = context.view
        game = view.sports_game(market.game_key)
        if game is None:
            return None      # not linked to a real game yet -> no opinion
        if market.yes_team not in (game.home_code, game.away_code):
            return None      # ticker and linked game disagree -> refuse to guess
        yes_is_home = market.yes_team == game.home_code

        members = view.latest_sports_ratings_by_provider(market.game_key)
        margins = {p: r.margin_home for p, r in members.items() if r.margin_home is not None}
        if not margins:
            return None      # no member has an opinion as-of now

        values = list(margins.values())
        mean = statistics.fmean(values)
        spread = statistics.stdev(values) if len(values) >= 2 else 0.0

        # Live-score clamp. Beyond the scheduled start we REQUIRE a fresh state:
        # a game in progress with a dead score feed is exactly the situation
        # where a pre-game probability is most confidently wrong.
        state = view.latest_game_state(market.game_key)
        start_ts = game.start_ts if game.start_ts is not None else market.start_ts
        started = start_ts is not None and context.decision_ts >= start_ts
        state_age_ms = (context.decision_ts - state.captured_ts) if state else None
        if started and (state is None or state_age_ms > self.max_state_age_ms):
            return None      # underway, but we cannot see the score -> no opinion

        f = fraction_remaining(state, league.regulation_periods)
        lead = 0
        if state is not None and state.home_score is not None and state.away_score is not None:
            lead = int(state.home_score) - int(state.away_score)

        if f <= 0.0:
            # Final. The margin is no longer a distribution.
            if lead == 0:
                return None  # a tie resolves both sides to No; do not guess a price
            p_home = 1.0 if lead > 0 else 0.0
            sigma = 0.0
        else:
            mu = lead + mean * f
            sigma = math.sqrt((league.margin_sigma * math.sqrt(f)) ** 2 + (spread * f) ** 2)
            sigma = max(sigma, self.min_sigma_floor * math.sqrt(f))
            p_home = prob_home_from_margin(mu, sigma)

        probability = p_home if yes_is_home else 1.0 - p_home

        inputs = {
            "ensemble_members": margins,          # {provider: expected home margin}
            "ensemble_mean_margin": round(mean, 3),
            "ensemble_spread_margin": round(spread, 3),
            "base_margin_sigma": league.margin_sigma,
            "effective_sigma": round(sigma, 3),
            "fraction_remaining": round(f, 3),
            "live_home_lead": lead,
            "game_state": state.state if state else None,
            "game_period": state.period if state else None,
            "state_age_seconds": round(state_age_ms / 1000, 1) if state_age_ms is not None else None,
            "p_home": round(p_home, 4),
            "yes_team": market.yes_team,
            "home_code": game.home_code,
            "away_code": game.away_code,
            "yes_is_home": yes_is_home,
            "league": market.league,
            "game_key": market.game_key,
            "as_of_ms": context.decision_ts,
        }
        return Prediction(probability=probability, uncertainty=sigma, inputs=inputs)
