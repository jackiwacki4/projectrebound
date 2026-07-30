"""Data collectors: persist Kalshi market data and model inputs continuously.

All writes are append-only observations (except market/game metadata and
settlement status, which are reference data). Collection continues even when
trading is halted -- data is the primary product of Phase 1.

`MarketCollector` is family-agnostic: order books, trades, and settlements are
the same job whatever the market is about. `WeatherCollector` and
`SportsCollector` gather each family's model inputs.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from ..clients.espn_client import EspnClient, EspnGame
from ..clients.forecast_providers import ForecastProvider, ObservationProvider
from ..clients.kalshi_client import KalshiClient
from ..clients.sports_providers import (
    CompletedGame,
    EspnScoreboardStateProvider,
    LeagueData,
    Matchup,
    RatingProvider,
)
from ..config import CityConfig, LeagueConfig
from ..logging_setup import log
from ..models.sports import parse_sports_ticker
from ..storage.dao import Dao
from ..util import iso_to_ms, now_ms

FORECAST_HORIZON_DAYS = 7


class MarketCollector:
    # Only the top few price levels are kept. The model reads best bid and best
    # ask; the rest was stored "for research" and cost ~2.4 KB per snapshot,
    # which at 60s across ~136 markets came to ~240 MB/day (measured on a live
    # database after two days). Ten levels a side is far more depth than any
    # 1-contract decision needs and still shows the shape of the queue for
    # later liquidity analysis. Set to 0 in config to keep everything.
    DEFAULT_DEPTH_LEVELS = 10

    # Markets that never settle -- a rained-out game rescheduled past the
    # window, a voided market -- would otherwise be re-polled every five
    # minutes forever, one request each, for the life of the database.
    SETTLEMENT_LOOKBACK_DAYS = 30

    def __init__(self, dao: Dao, client: KalshiClient, logger: logging.Logger,
                 depth_levels: Optional[int] = None) -> None:
        self._dao = dao
        self._client = client
        self._log = logger
        self._depth = (self.DEFAULT_DEPTH_LEVELS if depth_levels is None
                       else int(depth_levels))

    def _trim(self, levels: list) -> list:
        """Keep the best N price levels (they arrive best-first)."""
        return levels if self._depth <= 0 else levels[:self._depth]

    def poll_books(self, series_list: list[str]) -> int:
        """Snapshot the order book for every open market in the families."""
        snapshots = 0
        for series in series_list:
            markets = self._client.list_markets(series_ticker=series, status="open")
            for m in markets:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                close_ts = iso_to_ms(m["close_time"]) if m.get("close_time") else None
                self._dao.upsert_market(
                    ticker=ticker, series=series, city=None, title=m.get("title"),
                    close_ts=close_ts, status=m.get("status", "open"),
                )
                try:
                    ob = self._client.get_orderbook(ticker)
                    self._dao.insert_book_snapshot(ticker, ob.captured_ts,
                                                   self._trim(ob.yes_levels),
                                                   self._trim(ob.no_levels))
                    snapshots += 1
                except Exception as e:  # one bad market must not stop the sweep
                    log(self._log, logging.WARNING, "orderbook fetch failed",
                        ticker=ticker, error=str(e))
        return snapshots

    def poll_trades(self, series_list: list[str]) -> int:
        """Record observed trades for open markets in the families."""
        recorded = 0
        for series in series_list:
            for m in self._client.list_markets(series_ticker=series, status="open"):
                ticker = m.get("ticker")
                if not ticker:
                    continue
                try:
                    for t in self._client.get_trades(ticker):
                        created = t.get("created_time")
                        trade_ts = iso_to_ms(created) if created else now_ms()
                        self._dao.insert_trade(
                            ticker=ticker, trade_ts=trade_ts,
                            price_cents=int(t.get("yes_price", 0)),
                            count=int(t.get("count", 0)),
                            taker_side=t.get("taker_side"),
                            trade_id=t.get("trade_id"),
                        )
                        recorded += 1
                except Exception as e:
                    log(self._log, logging.WARNING, "trade fetch failed",
                        ticker=ticker, error=str(e))
        return recorded

    def poll_settlements(self) -> int:
        """Record settlement results for markets that have resolved."""
        cutoff = now_ms() - self.SETTLEMENT_LOOKBACK_DAYS * 86_400_000
        rows = self._dao.conn.execute(
            "SELECT ticker FROM markets "
            "WHERE ticker NOT IN (SELECT ticker FROM settlements) "
            "  AND (close_ts IS NULL OR close_ts >= ?)",
            (cutoff,),
        ).fetchall()
        recorded = 0
        for row in rows:
            ticker = row["ticker"]
            try:
                m = self._client.get_market(ticker)
            except Exception:
                continue
            result = (m.get("result") or "").lower()
            status = m.get("status", "")
            if result in ("yes", "no"):
                settled_ts = iso_to_ms(m["close_time"]) if m.get("close_time") else None
                self._dao.upsert_settlement(ticker, result, settled_ts)
                self._dao.upsert_market(
                    ticker=ticker, series=m.get("series_ticker", ""), city=None,
                    title=m.get("title"), close_ts=settled_ts, status=status or "settled",
                    result=result,
                )
                recorded += 1
        return recorded


class WeatherCollector:
    def __init__(self, dao: Dao, forecast_providers: list[ForecastProvider],
                 observation_providers: list[ObservationProvider],
                 logger: logging.Logger) -> None:
        self._dao = dao
        self._forecast_providers = forecast_providers
        self._observation_providers = observation_providers
        self._log = logger

    def poll_forecasts(self, cities: list[CityConfig]) -> int:
        """Capture every provider's forecast highs across the horizon, each
        stored as its own ensemble member stamped with its source."""
        stored = 0
        for city in cities:
            for provider in self._forecast_providers:
                try:
                    forecasts = provider.forecast_highs(city.lat, city.lon,
                                                        FORECAST_HORIZON_DAYS)
                except Exception as e:
                    log(self._log, logging.WARNING, "forecast provider failed",
                        provider=provider.name, city=city.name, error=str(e))
                    continue
                for fc in forecasts:
                    if fc.high_f is None:
                        continue
                    self._dao.insert_forecast(
                        station=city.nws_station, city=city.name, issued_ts=fc.issued_ts,
                        target_date=fc.target_date, forecast_high_f=fc.high_f,
                        raw=fc.raw, provider=fc.provider,
                    )
                    stored += 1
        return stored

    def poll_observations(self, cities: list[CityConfig]) -> int:
        """Capture station observations (e.g. METAR) -- what has actually
        happened so far today, used to clamp the model near settlement."""
        stored = 0
        for city in cities:
            for provider in self._observation_providers:
                try:
                    obs = provider.latest_observations(city.nws_station, city.lat, city.lon)
                except Exception as e:
                    log(self._log, logging.WARNING, "observation provider failed",
                        provider=provider.name, city=city.name, error=str(e))
                    continue
                for o in obs:
                    self._dao.insert_observation(
                        station=city.nws_station, obs_ts=o.obs_ts,
                        temp_c=o.temp_c, raw=o.raw,
                    )
                    stored += 1
        return stored


# --------------------------------------------------------------------------
# Sports
# --------------------------------------------------------------------------
# Kalshi's sports tickers date games by their Eastern calendar date; ESPN dates
# them in UTC. Converting with a fixed -4h (EDT) can be an hour off in winter,
# which only ever matters for games starting within an hour of midnight -- and
# the schedule window below is widened by a day on each side to absorb exactly
# that. See models/sports.py for the same note on the ticker parser.
_EASTERN_UTC_OFFSET_HOURS = 4


def _eastern_date(ts_ms: int) -> date:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return (dt - timedelta(hours=_EASTERN_UTC_OFFSET_HOURS)).date()


class SportsCollector:
    """Collects the sports family's model inputs.

    Four jobs on three cadences, mirroring the weather collector's split between
    slow-moving forecasts and fast-moving observations:

      link      : match each open Kalshi game market to a real ESPN game, so
                  every later row can be keyed by game and we know who is home.
      results   : completed games, accumulated over time. ESPN caps a range
                  request at about a hundred events, so a season cannot be
                  fetched in one call -- the bot walks a rolling window and lets
                  its own table become the history.
      ratings   : every ensemble member's predicted margin per upcoming game.
      states    : the live score, polled fast, which is what lets the model
                  sharpen as a game runs out.
    """

    def __init__(self, dao: Dao, client: KalshiClient,
                 rating_providers: list[RatingProvider],
                 state_providers: list[EspnScoreboardStateProvider],
                 espn: EspnClient, logger: logging.Logger, *,
                 schedule_horizon_days: int = 4,
                 results_lookback_days: int = 45) -> None:
        self._dao = dao
        self._client = client
        self._rating_providers = rating_providers
        self._state_providers = state_providers
        self._espn = espn
        self._log = logger
        self._horizon = int(schedule_horizon_days)
        self._lookback = int(results_lookback_days)

    # ---- link Kalshi markets to real games ----
    def poll_schedule(self, leagues: list[LeagueConfig]) -> int:
        """Resolve every open game market to an ESPN game and record the pairing.

        Until a market is linked, the model has no idea which side is at home and
        returns no opinion -- so this is the gate on the whole sports path, and
        an unmatched game is logged rather than silently skipped.
        """
        linked = 0
        for league in leagues:
            wanted = self._open_game_markets(league)
            if not wanted:
                continue
            espn_games = self._espn_games_for(league, sorted({d for _, d in wanted.values()}))
            unmatched: list[str] = []
            for game_key, (parts, target_date) in wanted.items():
                match = self._match(parts.pair_code, parts.start_ts, target_date, espn_games)
                if match is None:
                    # One summary line per league per poll, not one per market:
                    # a league whose team codes are wrong would otherwise emit
                    # thousands of identical warnings a day and bury real ones.
                    unmatched.append(f"{parts.pair_code}@{target_date.isoformat()}")
                    continue
                self._dao.upsert_sports_game(
                    game_key=game_key, league=league.name, series=league.kalshi_series,
                    away_code=match.away_code, home_code=match.home_code,
                    start_ts=match.start_ts, source_event_id=match.event_id,
                )
                linked += 1
            if unmatched:
                log(self._log, logging.WARNING,
                    "no ESPN game matched some Kalshi markets (team codes or schedule "
                    "window -- see clients/espn_client.py)",
                    league=league.name, unmatched=len(unmatched),
                    examples=", ".join(sorted(unmatched)[:5]))
        return linked

    def _open_game_markets(self, league: LeagueConfig) -> dict:
        """{game_key: (TickerParts, eastern_date)} for the league's open markets."""
        out: dict = {}
        for m in self._client.list_markets(series_ticker=league.kalshi_series, status="open"):
            ticker = m.get("ticker")
            parts = parse_sports_ticker(ticker) if ticker else None
            if parts is None:
                continue      # not a game-winner market (totals, futures, ...)
            game_key = m.get("event_ticker") or ticker.rsplit("-", 1)[0]
            out[game_key] = (parts, date.fromisoformat(parts.target_date))
        return out

    def _espn_games_for(self, league: LeagueConfig, days: list[date]) -> list[EspnGame]:
        """Every ESPN game on the days a market refers to, plus a day either side.

        Fetched one day at a time: a range request is capped at roughly a hundred
        events, and a silently truncated response would look exactly like "that
        game does not exist".
        """
        if not days:
            return []
        span = {d + timedelta(days=offset) for d in days for offset in (-1, 0, 1)}
        games: list[EspnGame] = []
        for day in sorted(span):
            try:
                games.extend(self._espn.scoreboard(league.espn_path, league.name, day))
            except Exception as e:
                log(self._log, logging.WARNING, "ESPN scoreboard fetch failed",
                    league=league.name, date=day.isoformat(), error=str(e))
        return games

    @staticmethod
    def _match(pair_code: str, start_ts: Optional[int], target_date: date,
               games: list[EspnGame]) -> Optional[EspnGame]:
        """Pick the ESPN game a Kalshi ticker refers to.

        The pair code (away+home) identifies the fixture; a doubleheader repeats
        it on the same day, so ties are broken by scheduled start time when the
        ticker carries one. Nothing within a day of the ticker's date is accepted
        without the codes matching exactly -- a near-miss is a mapping bug to be
        surfaced, not smoothed over.
        """
        candidates = [g for g in games if g.pair_code == pair_code]
        if not candidates:
            return None
        if start_ts is not None:
            best = min(candidates, key=lambda g: abs(g.start_ts - start_ts))
            # 12h: enough for a rain delay or a ticker/feed timezone quirk, far
            # short of the next day's game between the same two teams.
            return best if abs(best.start_ts - start_ts) <= 12 * 3_600_000 else None
        same_day = [g for g in candidates if _eastern_date(g.start_ts) == target_date]
        if len(same_day) == 1:
            return same_day[0]
        return None      # ambiguous (or nothing on that date) -> no opinion

    # ---- completed games (the Elo member's input) ----
    # Once the history exists there is no reason to re-walk it: results are
    # immutable and stored idempotently. Only the first pass for a league pays
    # the full lookback (one request per day), and steady state costs three.
    _RESULTS_CATCHUP_DAYS = 3

    def poll_results(self, leagues: list[LeagueConfig]) -> int:
        stored = 0
        today = date.today()
        for league in leagues:
            have_history = bool(self._dao.completed_games(
                league.name, now_ms() - self._lookback * 86_400_000))
            days_back = self._RESULTS_CATCHUP_DAYS if have_history else self._lookback
            for offset in range(days_back, -1, -1):
                day = today - timedelta(days=offset)
                try:
                    games = self._espn.scoreboard(league.espn_path, league.name, day)
                except Exception as e:
                    log(self._log, logging.WARNING, "ESPN results fetch failed",
                        league=league.name, date=day.isoformat(), error=str(e))
                    continue
                for g in games:
                    if not g.completed or g.home_score is None or g.away_score is None:
                        continue
                    self._dao.upsert_sports_result(
                        source_event_id=g.event_id, league=league.name,
                        start_ts=g.start_ts, away_code=g.away_code,
                        home_code=g.home_code, away_score=g.away_score,
                        home_score=g.home_score,
                    )
                    stored += 1
        return stored

    # ---- ensemble member ratings ----
    def poll_ratings(self, leagues: list[LeagueConfig]) -> int:
        stored = 0
        for league in leagues:
            matchups = self._pending_matchups(league)
            if not matchups:
                continue
            try:
                standings = {s.code: s for s in
                             self._espn.standings(league.espn_path, league.name)}
            except Exception as e:
                standings = {}
                log(self._log, logging.WARNING, "ESPN standings fetch failed",
                    league=league.name, error=str(e))
            data = LeagueData(league=league, standings=standings,
                              completed=self._completed_games(league))
            for provider in self._rating_providers:
                try:
                    ratings = provider.rate(matchups, data)
                except Exception as e:
                    log(self._log, logging.WARNING, "rating provider failed",
                        provider=provider.name, league=league.name, error=str(e))
                    continue
                for r in ratings:
                    self._dao.insert_sports_rating(
                        provider=r.provider, game_key=r.game_key, league=league.name,
                        issued_ts=r.issued_ts, margin_home=r.margin_home,
                        p_home=r.p_home, raw=r.raw,
                    )
                    stored += 1
        return stored

    # ---- live score ----
    def poll_states(self, leagues: list[LeagueConfig]) -> int:
        stored = 0
        today = date.today()
        for league in leagues:
            matchups = self._pending_matchups(league)
            if not matchups:
                continue
            for provider in self._state_providers:
                # Yesterday too: a game that started late Eastern is still in
                # progress after UTC midnight, and its state matters most then.
                for day in (today - timedelta(days=1), today):
                    try:
                        observations = provider.states(league, matchups, day)
                    except Exception as e:
                        log(self._log, logging.WARNING, "game-state provider failed",
                            provider=provider.name, league=league.name,
                            date=day.isoformat(), error=str(e))
                        continue
                    for o in observations:
                        self._dao.insert_sports_game_state(
                            game_key=o.game_key, obs_ts=o.obs_ts, state=o.state,
                            completed=o.completed, period=o.period,
                            home_score=o.home_score, away_score=o.away_score,
                            raw=o.raw,
                        )
                        stored += 1
        return stored

    # ---- helpers ----
    def _pending_matchups(self, league: LeagueConfig) -> list[Matchup]:
        """Linked games that have not finished and start within the horizon."""
        horizon_ms = now_ms() + self._horizon * 86_400_000
        rows = self._dao.conn.execute(
            """
            SELECT g.game_key, g.away_code, g.home_code, g.start_ts, g.source_event_id
            FROM sports_games g
            WHERE g.league = ?
              AND (g.start_ts IS NULL OR g.start_ts <= ?)
              AND g.game_key NOT IN (
                  SELECT game_key FROM sports_game_states WHERE completed = 1)
            ORDER BY g.start_ts
            """,
            (league.name, horizon_ms),
        ).fetchall()
        return [Matchup(game_key=r["game_key"], away_code=r["away_code"],
                        home_code=r["home_code"], start_ts=r["start_ts"],
                        source_event_id=r["source_event_id"]) for r in rows]

    def _completed_games(self, league: LeagueConfig) -> list[CompletedGame]:
        since = now_ms() - self._lookback * 86_400_000
        return [CompletedGame(away_code=r["away_code"], home_code=r["home_code"],
                              away_score=r["away_score"], home_score=r["home_score"],
                              start_ts=r["start_ts"])
                for r in self._dao.completed_games(league.name, since)]
