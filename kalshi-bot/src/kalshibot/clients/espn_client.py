"""ESPN public data client -- the sports family's data source.

Free, no API key, no registration. Three endpoints, all VERIFIED against the
live API (2026-07):

  scoreboard  https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates=...
              schedule + live score + final score. `dates` takes YYYYMMDD or a
              YYYYMMDD-YYYYMMDD range; a range is capped at roughly a hundred
              events per response, so long histories must be fetched in chunks
              (see `scoreboard_range`). This is why the bot accumulates results
              in its own table instead of re-deriving a season on demand.
  standings   https://site.api.espn.com/apis/v2/sports/{path}/standings
              wins/losses and points (runs) for/against per team. NOTE the path
              is `apis/v2`, NOT `apis/site/v2` -- the latter 404s.
  summary     https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={id}
              per-game detail; `pickcenter` carries sportsbook lines. Only used
              by the optional bookmaker member.

Everything here returns plain typed rows. Deciding what a number *means* is the
providers' job (`sports_providers.py`), and deciding what to trade is the
model's -- this file only fetches and normalises.
"""
from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from ..util import iso_to_ms
from .http_json import get_json

_SITE = "https://site.api.espn.com/apis/site/v2/sports"
_CORE = "https://site.api.espn.com/apis/v2/sports"

# ESPN abbreviation -> Kalshi ticker suffix, only where the two disagree.
#
# VERIFIED EMPIRICALLY (2026-07) by pulling every open KXMLBGAME / KXNFLGAME
# market suffix and diffing it against ESPN's team list for that league: 30/30
# MLB codes agreed except AZ and CWS, 32/32 NFL codes agreed except JAC and WAS.
# The table is per-league on purpose -- the same ESPN abbreviation maps to
# different Kalshi codes in different sports (ESPN's WSH is Kalshi WSH in MLB
# but WAS in the NFL), so a single flat table would silently mis-link games.
#
# A league absent from this table is assumed to use identical codes. That is an
# assumption, not a verified fact: an unverified league shows up as repeated
# "no ESPN game matched" warnings, which is the signal to diff its codes the
# same way (log every market suffix, log ESPN's team list, compare).
_ESPN_TO_KALSHI_BY_LEAGUE: dict[str, dict[str, str]] = {
    "mlb": {"ARI": "AZ", "CHW": "CWS"},      # Diamondbacks, White Sox
    "nfl": {"JAX": "JAC", "WSH": "WAS"},     # Jaguars, Commanders
}


def to_kalshi_code(espn_abbr: str, league: str) -> str:
    """Translate an ESPN team abbreviation into the code Kalshi uses in tickers."""
    abbr = (espn_abbr or "").upper()
    return _ESPN_TO_KALSHI_BY_LEAGUE.get(league.lower(), {}).get(abbr, abbr)


@dataclass(frozen=True)
class EspnGame:
    """One scheduled, live, or finished game."""
    event_id: str
    start_ts: int                 # scheduled start (ms)
    away_code: str                # Kalshi codes, so it can be matched to a ticker
    home_code: str
    state: str                    # "pre" | "in" | "post"
    completed: bool
    period: Optional[int]         # inning / quarter, 1-indexed
    away_score: Optional[int]
    home_score: Optional[int]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def pair_code(self) -> str:
        """Away+home concatenated, the form Kalshi uses in its event tickers."""
        return f"{self.away_code}{self.home_code}"


@dataclass(frozen=True)
class TeamStanding:
    code: str                     # Kalshi code
    wins: int
    losses: int
    points_for: float             # runs / points scored
    points_against: float

    @property
    def games_played(self) -> int:
        return self.wins + self.losses


@dataclass(frozen=True)
class BookmakerLine:
    provider_name: str            # e.g. "DraftKings"
    spread_home: Optional[float]  # home-relative: -1.5 = home favoured by 1.5
    moneyline_home: Optional[int]
    moneyline_away: Optional[int]
    raw: dict[str, Any] = field(default_factory=dict)


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class EspnClient:
    """Thin, cache-free wrapper. Errors propagate: the collector logs them per
    source, exactly as the weather providers do -- a swallowed failure looks
    identical to "no data available", which is far worse."""

    def scoreboard(self, espn_path: str, league: str, day: date) -> list[EspnGame]:
        return self.scoreboard_range(espn_path, league, day, day)

    def scoreboard_range(self, espn_path: str, league: str,
                         start: date, end: date) -> list[EspnGame]:
        """Games between two local dates, inclusive.

        ESPN caps a range response (~100 events), so callers that want a long
        history must walk it in short windows rather than asking for a season.
        """
        dates = (start.strftime("%Y%m%d") if start == end
                 else f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}")
        url = (f"{_SITE}/{espn_path}/scoreboard?"
               + urllib.parse.urlencode({"dates": dates, "limit": "500"}))
        data = get_json(url)
        return [g for g in (self._parse_event(e, league) for e in data.get("events") or [])
                if g is not None]

    @staticmethod
    def _parse_event(event: dict, league: str) -> Optional[EspnGame]:
        comps = event.get("competitions") or []
        if not comps:
            return None
        comp = comps[0]
        home = away = None
        for c in comp.get("competitors") or []:
            if c.get("homeAway") == "home":
                home = c
            elif c.get("homeAway") == "away":
                away = c
        if not home or not away:
            return None
        home_abbr = (home.get("team") or {}).get("abbreviation")
        away_abbr = (away.get("team") or {}).get("abbreviation")
        start = event.get("date")
        if not (home_abbr and away_abbr and start):
            return None
        status = event.get("status") or {}
        stype = status.get("type") or {}
        return EspnGame(
            event_id=str(event.get("id")),
            start_ts=iso_to_ms(start),
            away_code=to_kalshi_code(away_abbr, league),
            home_code=to_kalshi_code(home_abbr, league),
            state=str(stype.get("state") or "pre"),
            completed=bool(stype.get("completed")),
            period=_as_int(status.get("period")),
            away_score=_as_int(away.get("score")),
            home_score=_as_int(home.get("score")),
            raw={"id": event.get("id"), "shortName": event.get("shortName"),
                 "date": start, "status": stype.get("name")},
        )

    def standings(self, espn_path: str, league: str) -> list[TeamStanding]:
        """Season win-loss and runs/points for-and-against, per team.

        The response is a tree of groups (league -> conference -> division);
        entries can hang off any node, so it is walked recursively.
        """
        data = get_json(f"{_CORE}/{espn_path}/standings")
        out: dict[str, TeamStanding] = {}
        self._collect_standings(data, league, out)
        return list(out.values())

    @classmethod
    def _collect_standings(cls, node: Any, league: str,
                           out: dict[str, TeamStanding]) -> None:
        if not isinstance(node, dict):
            return
        entries = ((node.get("standings") or {}).get("entries")
                   if isinstance(node.get("standings"), dict) else None)
        for entry in entries or []:
            team = entry.get("team") or {}
            abbr = team.get("abbreviation")
            if not abbr:
                continue
            stats = {s.get("name"): s.get("value") for s in (entry.get("stats") or [])
                     if s.get("value") is not None}
            wins, losses = stats.get("wins"), stats.get("losses")
            # `pointsFor`/`pointsAgainst` are runs in MLB, points elsewhere.
            pf, pa = stats.get("pointsFor"), stats.get("pointsAgainst")
            if wins is None or losses is None or pf is None or pa is None:
                continue
            code = to_kalshi_code(abbr, league)
            out.setdefault(code, TeamStanding(code=code, wins=int(wins),
                                              losses=int(losses),
                                              points_for=float(pf),
                                              points_against=float(pa)))
        for child in node.get("children") or []:
            cls._collect_standings(child, league, out)

    def bookmaker_line(self, espn_path: str, event_id: str) -> Optional[BookmakerLine]:
        """Highest-priority sportsbook line for one game, or None if unposted.

        Used only by the optional `espn_bookmaker` member -- see
        sports_providers.py for why it is off by default.
        """
        url = f"{_SITE}/{espn_path}/summary?" + urllib.parse.urlencode({"event": event_id})
        picks = get_json(url).get("pickcenter") or []
        if not picks:
            return None
        pick = min(picks, key=lambda p: (p.get("provider") or {}).get("priority", 99))
        spread = pick.get("spread")
        return BookmakerLine(
            provider_name=((pick.get("provider") or {}).get("name") or "unknown"),
            spread_home=float(spread) if spread is not None else None,
            moneyline_home=_as_int((pick.get("homeTeamOdds") or {}).get("moneyLine")),
            moneyline_away=_as_int((pick.get("awayTeamOdds") or {}).get("moneyLine")),
            raw={"provider": (pick.get("provider") or {}).get("name"),
                 "details": pick.get("details"), "spread": spread,
                 "overUnder": pick.get("overUnder")},
        )
