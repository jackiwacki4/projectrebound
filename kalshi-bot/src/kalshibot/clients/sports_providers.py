"""Sports rating providers behind one interface, so the model can ensemble them.

The weather ensemble asks several independent weather models "how hot will it
get?". The sports ensemble asks several independent rating methods "by how much
will the home team win?" -- and, exactly as with weather, their disagreement
becomes the model's uncertainty.

Rating providers (predict the home margin):
  - elo          : Elo ratings replayed from recently completed games. A
                   RECENT-FORM signal: a win over a strong opponent moves a team
                   more than a win over a weak one, and old games fade out.
  - log5         : Bradley-Terry / log5 on season win-loss records. A
                   SEASON-LONG signal that ignores who the wins came against.
  - pythagorean  : expected win rate from runs/points scored and allowed. Says
                   what the record SHOULD be, so it disagrees with log5 exactly
                   where a team has been lucky or unlucky.
  - espn_bookmaker : the sportsbook line (off by default -- see below).

Observation provider (what has actually happened so far):
  - espn_scoreboard : live score, period, and final status per game.

Unlike the weather members, these are NOT independent measurements of the world:
they all read the same season of the same games from the same feed, so they
agree more than they are jointly right, and their spread understates real
uncertainty. That is what `model.min_sigma_floor` in the config is for, and it
is stated plainly in the model docstring rather than hidden here.

**Why the bookmaker member is off by default.** A sportsbook line is not a
prediction method, it is another market's price. Enabling it turns the bot from
"does an independent model find mispricing?" into "does Kalshi disagree with
DraftKings?" -- a legitimate question, and probably a more profitable one, but a
different one, and it would silently dominate the ensemble. Turn it on
deliberately, knowing that is the experiment you are now running.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..config import LeagueConfig
from ..models.sports import margin_from_prob, prob_home_from_margin
from ..util import now_ms
from .espn_client import BookmakerLine, EspnClient, TeamStanding


@dataclass(frozen=True)
class Matchup:
    """A game to be rated. `game_key` is the Kalshi event ticker."""
    game_key: str
    away_code: str
    home_code: str
    start_ts: Optional[int] = None
    source_event_id: Optional[str] = None


@dataclass(frozen=True)
class CompletedGame:
    away_code: str
    home_code: str
    away_score: int
    home_score: int
    start_ts: int


@dataclass(frozen=True)
class LeagueData:
    """Everything the collector fetched this cycle, handed to every provider.

    Providers are pure functions of this plus the league constants -- no
    provider does its own I/O except the bookmaker member, which needs a
    per-game request.
    """
    league: LeagueConfig
    standings: dict[str, TeamStanding] = field(default_factory=dict)
    completed: list[CompletedGame] = field(default_factory=list)


@dataclass(frozen=True)
class ProviderRating:
    provider: str
    game_key: str
    margin_home: float        # expected home_score - away_score
    p_home: float             # the win probability that margin implies
    issued_ts: int
    raw: dict[str, Any] = field(default_factory=dict)


def _rating(provider: str, matchup: Matchup, margin: float, league: LeagueConfig,
            raw: dict[str, Any]) -> ProviderRating:
    return ProviderRating(
        provider=provider, game_key=matchup.game_key, margin_home=margin,
        p_home=prob_home_from_margin(margin, league.margin_sigma),
        issued_ts=now_ms(), raw=raw,
    )


class RatingProvider(ABC):
    name: str

    @abstractmethod
    def rate(self, matchups: list[Matchup], data: LeagueData) -> list[ProviderRating]:
        """Rate every matchup this provider has an opinion about.

        A provider with insufficient data returns fewer rows (or none) rather
        than a guess -- the model handles a thin ensemble, but cannot detect a
        fabricated member.
        """
        raise NotImplementedError


# --------------------------------------------------------------------------
# log5 / Bradley-Terry on season records
# --------------------------------------------------------------------------
def log5(p_a: float, p_b: float) -> float:
    """Probability A beats B given each team's win rate against the field.

    The standard log5 formula (Bill James): it is what "these teams' records
    imply about this matchup" means, and it correctly says a .600 team beats a
    .500 team about 60% of the time rather than 55% or 65%.
    """
    p_a = min(0.999, max(0.001, p_a))
    p_b = min(0.999, max(0.001, p_b))
    num = p_a * (1.0 - p_b)
    den = num + p_b * (1.0 - p_a)
    return 0.5 if den <= 0 else num / den


class Log5Provider(RatingProvider):
    """Season win-loss records, home advantage added in margin space."""
    name = "log5"
    min_games = 10

    def rate(self, matchups: list[Matchup], data: LeagueData) -> list[ProviderRating]:
        out: list[ProviderRating] = []
        for m in matchups:
            home = data.standings.get(m.home_code)
            away = data.standings.get(m.away_code)
            if not home or not away:
                continue
            if home.games_played < self.min_games or away.games_played < self.min_games:
                continue      # too early in the season to mean anything
            wp_home = home.wins / home.games_played
            wp_away = away.wins / away.games_played
            p_neutral = log5(wp_home, wp_away)
            margin = (margin_from_prob(p_neutral, data.league.margin_sigma)
                      + data.league.home_advantage_margin)
            out.append(_rating(self.name, m, margin, data.league, {
                "home_record": f"{home.wins}-{home.losses}",
                "away_record": f"{away.wins}-{away.losses}",
                "p_neutral": round(p_neutral, 4),
                "home_advantage_margin": data.league.home_advantage_margin,
            }))
        return out


# --------------------------------------------------------------------------
# Pythagorean expectation on scoring differential
# --------------------------------------------------------------------------
class PythagoreanProvider(RatingProvider):
    """Expected win rate from points/runs scored and allowed.

    win% = PF^e / (PF^e + PA^e). The exponent is sport-specific; 1.83 is the
    long-standing baseball value (Bill James / Pete Palmer) and is a reasonable
    default elsewhere. This member exists to disagree with `log5`: it measures
    how a team has actually outscored opponents, so it flags teams whose record
    is ahead of or behind their scoring.
    """
    name = "pythagorean"
    min_games = 10

    def __init__(self, exponent: float = 1.83) -> None:
        self.exponent = float(exponent)

    def _expected_win_pct(self, s: TeamStanding) -> Optional[float]:
        if s.points_for <= 0 or s.points_against <= 0:
            return None
        pf = s.points_for ** self.exponent
        pa = s.points_against ** self.exponent
        return pf / (pf + pa)

    def rate(self, matchups: list[Matchup], data: LeagueData) -> list[ProviderRating]:
        out: list[ProviderRating] = []
        for m in matchups:
            home = data.standings.get(m.home_code)
            away = data.standings.get(m.away_code)
            if not home or not away:
                continue
            if home.games_played < self.min_games or away.games_played < self.min_games:
                continue
            wp_home = self._expected_win_pct(home)
            wp_away = self._expected_win_pct(away)
            if wp_home is None or wp_away is None:
                continue
            p_neutral = log5(wp_home, wp_away)
            margin = (margin_from_prob(p_neutral, data.league.margin_sigma)
                      + data.league.home_advantage_margin)
            out.append(_rating(self.name, m, margin, data.league, {
                "exponent": self.exponent,
                "home_expected_win_pct": round(wp_home, 4),
                "away_expected_win_pct": round(wp_away, 4),
                "p_neutral": round(p_neutral, 4),
            }))
        return out


# --------------------------------------------------------------------------
# Elo on recently completed games
# --------------------------------------------------------------------------
class EloProvider(RatingProvider):
    """Elo ratings replayed from the completed games the bot has collected.

    Plain Elo, no margin-of-victory multiplier and no pre-season carry-over: it
    starts everyone level and learns from results in chronological order, which
    makes it a recent-form measure that is genuinely different from a season
    record. Deliberately legible -- when it is wrong you can replay the same
    games by hand and see why.

    It abstains until every team in a matchup has `elo_min_games` games in the
    collected history, because a rating anchored at the 1500 default is not a
    prediction, it is a placeholder.
    """
    name = "elo"

    def rate(self, matchups: list[Matchup], data: LeagueData) -> list[ProviderRating]:
        if not data.completed:
            return []
        league = data.league
        ratings: dict[str, float] = {}
        games_seen: dict[str, int] = {}
        for g in sorted(data.completed, key=lambda g: g.start_ts):
            rh = ratings.get(g.home_code, 1500.0)
            ra = ratings.get(g.away_code, 1500.0)
            expected_home = self._expected(rh + league.elo_home_advantage, ra)
            actual_home = 1.0 if g.home_score > g.away_score else (
                0.5 if g.home_score == g.away_score else 0.0)
            delta = league.elo_k * (actual_home - expected_home)
            ratings[g.home_code] = rh + delta
            ratings[g.away_code] = ra - delta
            games_seen[g.home_code] = games_seen.get(g.home_code, 0) + 1
            games_seen[g.away_code] = games_seen.get(g.away_code, 0) + 1

        out: list[ProviderRating] = []
        for m in matchups:
            if (games_seen.get(m.home_code, 0) < league.elo_min_games
                    or games_seen.get(m.away_code, 0) < league.elo_min_games):
                continue
            rh, ra = ratings[m.home_code], ratings[m.away_code]
            p_home = self._expected(rh + league.elo_home_advantage, ra)
            margin = margin_from_prob(p_home, league.margin_sigma)
            out.append(_rating(self.name, m, margin, league, {
                "home_elo": round(rh, 1), "away_elo": round(ra, 1),
                "elo_home_advantage": league.elo_home_advantage,
                "elo_k": league.elo_k,
                "games_in_history": len(data.completed),
                "p_home_elo": round(p_home, 4),
            }))
        return out

    @staticmethod
    def _expected(rating_a: float, rating_b: float) -> float:
        return 1.0 / (1.0 + 10.0 ** (-(rating_a - rating_b) / 400.0))


# --------------------------------------------------------------------------
# Sportsbook line (opt-in; see module docstring)
# --------------------------------------------------------------------------
def devig_moneyline(home_ml: Optional[int], away_ml: Optional[int]) -> Optional[float]:
    """P(home wins) implied by a two-way moneyline, with the vig removed.

    American odds -> raw probability, then normalise so the two sides sum to 1.
    Skipping the normalisation would leave the book's margin in the number and
    systematically overstate both teams.
    """
    def implied(ml: Optional[int]) -> Optional[float]:
        if ml is None:
            return None
        return 100.0 / (ml + 100.0) if ml > 0 else (-ml) / ((-ml) + 100.0)

    p_home, p_away = implied(home_ml), implied(away_ml)
    if p_home is None or p_away is None or (p_home + p_away) <= 0:
        return None
    return p_home / (p_home + p_away)


class EspnBookmakerProvider(RatingProvider):
    """Sportsbook line via ESPN's `pickcenter`. One request per game.

    Prefers the point spread (already a margin, and the sharper of the two
    numbers) and falls back to the de-vigged moneyline.
    """
    name = "espn_bookmaker"

    def __init__(self, client: Optional[EspnClient] = None) -> None:
        self._client = client or EspnClient()

    def rate(self, matchups: list[Matchup], data: LeagueData) -> list[ProviderRating]:
        out: list[ProviderRating] = []
        for m in matchups:
            if not m.source_event_id:
                continue
            line: Optional[BookmakerLine] = self._client.bookmaker_line(
                data.league.espn_path, m.source_event_id)
            if line is None:
                continue
            margin = self._margin(line, data.league)
            if margin is None:
                continue
            out.append(_rating(self.name, m, margin, data.league, {
                "book": line.provider_name, "spread_home": line.spread_home,
                "moneyline_home": line.moneyline_home,
                "moneyline_away": line.moneyline_away,
            }))
        return out

    @staticmethod
    def _margin(line: BookmakerLine, league: LeagueConfig) -> Optional[float]:
        if line.spread_home is not None:
            # A spread is quoted home-relative: -1.5 means the home team is
            # favoured by 1.5, i.e. an expected margin of +1.5.
            return -float(line.spread_home)
        p_home = devig_moneyline(line.moneyline_home, line.moneyline_away)
        if p_home is None:
            return None
        return margin_from_prob(p_home, league.margin_sigma)


# --------------------------------------------------------------------------
# Observation provider: the live score
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class GameStateObservation:
    game_key: str
    obs_ts: int
    state: str                # "pre" | "in" | "post"
    completed: bool
    period: Optional[int]
    home_score: Optional[int]
    away_score: Optional[int]
    raw: dict[str, Any] = field(default_factory=dict)


class EspnScoreboardStateProvider:
    """Live score + period per game, from the same scoreboard feed used for the
    schedule. The sports counterpart of the METAR observation provider."""
    name = "espn_scoreboard"

    def __init__(self, client: Optional[EspnClient] = None) -> None:
        self._client = client or EspnClient()

    def states(self, league: LeagueConfig, matchups: list[Matchup],
               day) -> list[GameStateObservation]:
        by_event = {m.source_event_id: m for m in matchups if m.source_event_id}
        games = self._client.scoreboard(league.espn_path, league.name, day)
        fetched = now_ms()
        out: list[GameStateObservation] = []
        for g in games:
            m = by_event.get(g.event_id)
            if m is None:
                continue      # a game with no Kalshi market: nothing to record
            out.append(GameStateObservation(
                game_key=m.game_key,
                # The feed carries no observation timestamp, so fetch time is
                # the honest "known at" stamp -- as with Open-Meteo forecasts.
                obs_ts=fetched,
                state=g.state, completed=g.completed, period=g.period,
                home_score=g.home_score, away_score=g.away_score,
                raw=g.raw,
            ))
        return out


# --------------------------------------------------------------------------
# Build providers from config
# --------------------------------------------------------------------------
def build_rating_providers(names: list[str], *, pythagorean_exponent: float = 1.83,
                          client: Optional[EspnClient] = None) -> list[RatingProvider]:
    providers: list[RatingProvider] = []
    for name in names:
        if name == "elo":
            providers.append(EloProvider())
        elif name == "log5":
            providers.append(Log5Provider())
        elif name == "pythagorean":
            providers.append(PythagoreanProvider(pythagorean_exponent))
        elif name == "espn_bookmaker":
            providers.append(EspnBookmakerProvider(client))
        # Unknown names are ignored deliberately; config is the source of truth.
    return providers


def build_state_providers(names: list[str], *, client: Optional[EspnClient] = None
                          ) -> list[EspnScoreboardStateProvider]:
    return [EspnScoreboardStateProvider(client) for name in names
            if name == "espn_scoreboard"]
