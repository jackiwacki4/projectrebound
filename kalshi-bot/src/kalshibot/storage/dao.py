"""Data access layer.

Two distinct reader roles, kept as separate types on purpose:

- `Dao`   : writers, plus "latest" readers used by collectors and the
            reporting CLI. These legitimately need current data.
- `AsOfView` : the ONLY thing a prediction model is ever handed. Every method
            filters on `<= as_of_ms`, and there is deliberately no method that
            returns unfiltered/latest rows. This makes silent lookahead
            structurally impossible rather than a matter of discipline.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from ..util import now_ms


# --------------------------------------------------------------------------
# As-of view: no-lookahead enforcement lives here.
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ForecastRow:
    station: str
    issued_ts: int
    fetched_ts: int
    target_date: str
    forecast_high_f: Optional[float]
    raw: dict[str, Any]
    provider: str = "nws"


@dataclass(frozen=True)
class BookRow:
    ticker: str
    captured_ts: int
    yes_levels: list[list[int]]
    no_levels: list[list[int]]
    best_yes_bid: Optional[int]
    best_yes_ask: Optional[int]
    best_no_bid: Optional[int]
    best_no_ask: Optional[int]


@dataclass(frozen=True)
class SportsGameRow:
    game_key: str
    league: str
    series: str
    away_code: str
    home_code: str
    start_ts: Optional[int]
    source_event_id: Optional[str]


@dataclass(frozen=True)
class SportsRatingRow:
    provider: str
    game_key: str
    league: str
    issued_ts: int
    fetched_ts: int
    margin_home: Optional[float]
    p_home: Optional[float]
    raw: dict[str, Any]


@dataclass(frozen=True)
class SportsStateRow:
    game_key: str
    obs_ts: int
    captured_ts: int
    state: str                  # "pre" | "in" | "post"
    completed: bool
    period: Optional[int]
    home_score: Optional[int]
    away_score: Optional[int]
    raw: dict[str, Any]


class AsOfView:
    """A read-only window into the DB as it was known at `as_of_ms`.

    Construct via `Dao.as_of(ts)`. Nothing here can surface a row whose
    "known at" timestamp is after `as_of_ms`.
    """

    def __init__(self, conn: sqlite3.Connection, as_of_ms: int) -> None:
        self._conn = conn
        self._as_of = int(as_of_ms)

    @property
    def as_of_ms(self) -> int:
        return self._as_of

    def _row_to_forecast(self, row) -> ForecastRow:
        return ForecastRow(
            station=row["station"], issued_ts=row["issued_ts"],
            fetched_ts=row["fetched_ts"], target_date=row["target_date"],
            forecast_high_f=row["forecast_high_f"], raw=json.loads(row["raw"]),
            provider=row["provider"] if "provider" in row.keys() else "nws",
        )

    def latest_forecast(self, station: str, target_date: str) -> Optional[ForecastRow]:
        # Most recent forecast across ALL providers. We could only "know" a
        # forecast once we had fetched it, so the binding no-lookahead
        # constraint is fetched_ts <= as_of.
        row = self._conn.execute(
            """
            SELECT provider, station, issued_ts, fetched_ts, target_date, forecast_high_f, raw
            FROM forecasts
            WHERE station = ? AND target_date = ? AND fetched_ts <= ?
            ORDER BY issued_ts DESC, fetched_ts DESC
            LIMIT 1
            """,
            (station, target_date, self._as_of),
        ).fetchone()
        return self._row_to_forecast(row) if row else None

    def latest_forecasts_by_provider(self, station: str,
                                     target_date: str) -> dict[str, ForecastRow]:
        """Each provider's most recent forecast for the target, as-of now.

        This is what the ensemble model consumes: one member per source, all
        filtered by fetched_ts <= as_of so no future data can leak in.
        """
        rows = self._conn.execute(
            """
            SELECT provider, station, issued_ts, fetched_ts, target_date, forecast_high_f, raw
            FROM forecasts
            WHERE station = ? AND target_date = ? AND fetched_ts <= ?
            ORDER BY issued_ts DESC, fetched_ts DESC
            """,
            (station, target_date, self._as_of),
        ).fetchall()
        out: dict[str, ForecastRow] = {}
        for row in rows:
            fc = self._row_to_forecast(row)
            out.setdefault(fc.provider, fc)  # first seen = most recent per provider
        return out

    def observed_max_c_today(self, station: str, day_start_ms: int,
                             day_end_ms: int) -> Optional[float]:
        """Highest observed temperature (deg C) so far for the target local day,
        counting only observations known as-of now. Used to clamp the model:
        a daily high can only be at or above what has already been observed."""
        end = min(day_end_ms, self._as_of)
        row = self._conn.execute(
            """
            SELECT MAX(temp_c) AS m FROM observations
            WHERE station = ? AND obs_ts BETWEEN ? AND ? AND captured_ts <= ?
            """,
            (station, day_start_ms, end, self._as_of),
        ).fetchone()
        return row["m"] if row and row["m"] is not None else None

    def latest_book(self, ticker: str) -> Optional[BookRow]:
        row = self._conn.execute(
            """
            SELECT ticker, captured_ts, yes_levels, no_levels,
                   best_yes_bid, best_yes_ask, best_no_bid, best_no_ask
            FROM book_snapshots
            WHERE ticker = ? AND captured_ts <= ?
            ORDER BY captured_ts DESC
            LIMIT 1
            """,
            (ticker, self._as_of),
        ).fetchone()
        if row is None:
            return None
        return BookRow(
            ticker=row["ticker"],
            captured_ts=row["captured_ts"],
            yes_levels=json.loads(row["yes_levels"]),
            no_levels=json.loads(row["no_levels"]),
            best_yes_bid=row["best_yes_bid"],
            best_yes_ask=row["best_yes_ask"],
            best_no_bid=row["best_no_bid"],
            best_no_ask=row["best_no_ask"],
        )

    def count_visible_forecasts(self, station: str) -> int:
        """Diagnostic used by the lookahead test."""
        return self._conn.execute(
            "SELECT COUNT(*) AS c FROM forecasts WHERE station = ? AND fetched_ts <= ?",
            (station, self._as_of),
        ).fetchone()["c"]

    # ---- sports family (same as-of discipline as above) ----
    def sports_game(self, game_key: str) -> Optional[SportsGameRow]:
        """Which teams, and which is at home. Filtered on first_seen_ts so a
        game we had not yet resolved is invisible to an earlier as-of instant."""
        row = self._conn.execute(
            """
            SELECT game_key, league, series, away_code, home_code, start_ts, source_event_id
            FROM sports_games
            WHERE game_key = ? AND first_seen_ts <= ?
            """,
            (game_key, self._as_of),
        ).fetchone()
        if row is None:
            return None
        return SportsGameRow(
            game_key=row["game_key"], league=row["league"], series=row["series"],
            away_code=row["away_code"], home_code=row["home_code"],
            start_ts=row["start_ts"], source_event_id=row["source_event_id"],
        )

    def latest_sports_ratings_by_provider(self, game_key: str) -> dict[str, SportsRatingRow]:
        """Each rating provider's most recent prediction for the game, as-of now.

        The sports counterpart of `latest_forecasts_by_provider`: one ensemble
        member per source, all filtered by fetched_ts <= as_of.
        """
        rows = self._conn.execute(
            """
            SELECT provider, game_key, league, issued_ts, fetched_ts,
                   margin_home, p_home, raw
            FROM sports_ratings
            WHERE game_key = ? AND fetched_ts <= ?
            ORDER BY fetched_ts DESC, id DESC
            """,
            (game_key, self._as_of),
        ).fetchall()
        out: dict[str, SportsRatingRow] = {}
        for row in rows:
            out.setdefault(row["provider"], SportsRatingRow(
                provider=row["provider"], game_key=row["game_key"], league=row["league"],
                issued_ts=row["issued_ts"], fetched_ts=row["fetched_ts"],
                margin_home=row["margin_home"], p_home=row["p_home"],
                raw=json.loads(row["raw"]),
            ))  # first seen = most recent per provider
        return out

    def latest_game_state(self, game_key: str) -> Optional[SportsStateRow]:
        """The most recent live state we had captured for the game, as-of now."""
        row = self._conn.execute(
            """
            SELECT game_key, obs_ts, captured_ts, state, completed, period,
                   home_score, away_score, raw
            FROM sports_game_states
            WHERE game_key = ? AND captured_ts <= ?
            ORDER BY captured_ts DESC, id DESC
            LIMIT 1
            """,
            (game_key, self._as_of),
        ).fetchone()
        if row is None:
            return None
        return SportsStateRow(
            game_key=row["game_key"], obs_ts=row["obs_ts"], captured_ts=row["captured_ts"],
            state=row["state"], completed=bool(row["completed"]), period=row["period"],
            home_score=row["home_score"], away_score=row["away_score"],
            raw=json.loads(row["raw"]),
        )


# --------------------------------------------------------------------------
# Full DAO: writers + latest readers (collectors, reporting).
# --------------------------------------------------------------------------
class Dao:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def as_of(self, as_of_ms: int) -> AsOfView:
        return AsOfView(self.conn, as_of_ms)

    # ---- markets (reference data; upsert) ----
    def upsert_market(self, ticker: str, series: str, city: Optional[str],
                      title: Optional[str], close_ts: Optional[int],
                      status: Optional[str], result: Optional[str] = None) -> None:
        ts = now_ms()
        self.conn.execute(
            """
            INSERT INTO markets(ticker, series, city, title, close_ts, status, result,
                                first_seen_ts, updated_ts)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker) DO UPDATE SET
                series=excluded.series, city=excluded.city, title=excluded.title,
                close_ts=excluded.close_ts, status=excluded.status,
                result=COALESCE(excluded.result, markets.result),
                updated_ts=excluded.updated_ts
            """,
            (ticker, series, city, title, close_ts, status, result, ts, ts),
        )

    def open_markets(self, series: Optional[str] = None) -> list[sqlite3.Row]:
        if series:
            return self.conn.execute(
                "SELECT * FROM markets WHERE status='open' AND series=?", (series,)
            ).fetchall()
        return self.conn.execute("SELECT * FROM markets WHERE status='open'").fetchall()

    # ---- append-only observations ----
    def insert_book_snapshot(self, ticker: str, captured_ts: int,
                             yes_levels: list, no_levels: list) -> int:
        def best_bid(levels):  # best (highest) bid price
            return max((p for p, _ in levels), default=None)

        def best_ask_from_other(other_levels):
            # On Kalshi, the YES ask equals 100 - best NO bid, etc. We store the
            # raw levels and derive the crossing prices for convenience.
            b = best_bid(other_levels)
            return (100 - b) if b is not None else None

        cur = self.conn.execute(
            """
            INSERT INTO book_snapshots(ticker, captured_ts, yes_levels, no_levels,
                                       best_yes_bid, best_yes_ask, best_no_bid, best_no_ask)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                ticker, captured_ts,
                json.dumps(yes_levels), json.dumps(no_levels),
                best_bid(yes_levels), best_ask_from_other(no_levels),
                best_bid(no_levels), best_ask_from_other(yes_levels),
            ),
        )
        return cur.lastrowid

    def insert_trade(self, ticker: str, trade_ts: int, price_cents: int,
                     count: int, taker_side: Optional[str],
                     trade_id: Optional[str] = None) -> None:
        # OR IGNORE + unique trade_id makes repeated polling idempotent without
        # dropping genuinely distinct trades (trade_id NULL rows are not deduped).
        self.conn.execute(
            "INSERT OR IGNORE INTO trades(trade_id, ticker, trade_ts, captured_ts, "
            "price_cents, count, taker_side) VALUES(?,?,?,?,?,?,?)",
            (trade_id, ticker, trade_ts, now_ms(), price_cents, count, taker_side),
        )

    def insert_forecast(self, station: str, city: Optional[str], issued_ts: int,
                        target_date: str, forecast_high_f: Optional[float],
                        raw: dict, provider: str = "nws") -> None:
        self.conn.execute(
            "INSERT INTO forecasts(provider, station, city, issued_ts, fetched_ts, "
            "target_date, forecast_high_f, raw) VALUES(?,?,?,?,?,?,?,?)",
            (provider, station, city, issued_ts, now_ms(), target_date,
             forecast_high_f, json.dumps(raw)),
        )

    def insert_observation(self, station: str, obs_ts: int, temp_c: Optional[float],
                           raw: dict) -> None:
        # OR IGNORE: the same METAR is re-seen on every poll; dedup on (station, obs_ts).
        self.conn.execute(
            "INSERT OR IGNORE INTO observations(station, obs_ts, captured_ts, temp_c, raw) "
            "VALUES(?,?,?,?,?)",
            (station, obs_ts, now_ms(), temp_c, json.dumps(raw)),
        )

    # ---- sports family writers ----
    def upsert_sports_game(self, *, game_key: str, league: str, series: str,
                           away_code: str, home_code: str, start_ts: Optional[int],
                           source_event_id: Optional[str]) -> None:
        ts = now_ms()
        self.conn.execute(
            """
            INSERT INTO sports_games(game_key, league, series, away_code, home_code,
                                     start_ts, source_event_id, first_seen_ts, updated_ts)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(game_key) DO UPDATE SET
                league=excluded.league, series=excluded.series,
                away_code=excluded.away_code, home_code=excluded.home_code,
                start_ts=COALESCE(excluded.start_ts, sports_games.start_ts),
                source_event_id=COALESCE(excluded.source_event_id,
                                         sports_games.source_event_id),
                updated_ts=excluded.updated_ts
            """,
            (game_key, league, series, away_code, home_code, start_ts,
             source_event_id, ts, ts),
        )

    def insert_sports_rating(self, *, provider: str, game_key: str, league: str,
                             issued_ts: int, margin_home: Optional[float],
                             p_home: Optional[float], raw: dict) -> None:
        self.conn.execute(
            "INSERT INTO sports_ratings(provider, game_key, league, issued_ts, fetched_ts, "
            "margin_home, p_home, raw) VALUES(?,?,?,?,?,?,?,?)",
            (provider, game_key, league, issued_ts, now_ms(), margin_home, p_home,
             json.dumps(raw)),
        )

    def insert_sports_game_state(self, *, game_key: str, obs_ts: int, state: str,
                                 completed: bool, period: Optional[int],
                                 home_score: Optional[int], away_score: Optional[int],
                                 raw: dict) -> None:
        self.conn.execute(
            "INSERT INTO sports_game_states(game_key, obs_ts, captured_ts, state, "
            "completed, period, home_score, away_score, raw) VALUES(?,?,?,?,?,?,?,?,?)",
            (game_key, obs_ts, now_ms(), state, 1 if completed else 0, period,
             home_score, away_score, json.dumps(raw)),
        )

    def upsert_sports_result(self, *, source_event_id: str, league: str, start_ts: int,
                             away_code: str, home_code: str, away_score: int,
                             home_score: int) -> None:
        # Scores are final, so the first record wins; re-polling is a no-op.
        self.conn.execute(
            "INSERT INTO sports_results(source_event_id, league, start_ts, away_code, "
            "home_code, away_score, home_score, recorded_ts) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_event_id) DO NOTHING",
            (source_event_id, league, start_ts, away_code, home_code, away_score,
             home_score, now_ms()),
        )

    def completed_games(self, league: str, since_ts: int = 0) -> list[sqlite3.Row]:
        """Finished games in chronological order -- the Elo member's input."""
        return self.conn.execute(
            "SELECT * FROM sports_results WHERE league=? AND start_ts >= ? "
            "ORDER BY start_ts",
            (league, since_ts),
        ).fetchall()

    def insert_decision(self, *, decision_ts: int, ticker: str, model_name: str,
                        probability: float, uncertainty: Optional[float], inputs: dict,
                        book_snapshot_id: Optional[int], best_yes_bid: Optional[int],
                        best_yes_ask: Optional[int], intended_side: Optional[str],
                        intended_price_cents: Optional[int], edge_after_fees: Optional[float],
                        gate_passed: bool, blocked_by: Optional[str]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO decisions(decision_ts, ticker, model_name, probability, uncertainty,
                inputs, book_snapshot_id, best_yes_bid, best_yes_ask, intended_side,
                intended_price_cents, edge_after_fees, gate_passed, blocked_by)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (decision_ts, ticker, model_name, probability, uncertainty, json.dumps(inputs),
             book_snapshot_id, best_yes_bid, best_yes_ask, intended_side,
             intended_price_cents, edge_after_fees, 1 if gate_passed else 0, blocked_by),
        )
        return cur.lastrowid

    def insert_paper_fill(self, *, decision_id: int, ticker: str, side: str, count: int,
                          price_cents: int, fee_cents: int, filled_ts: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO paper_fills(decision_id, ticker, side, count, price_cents, "
            "fee_cents, filled_ts) VALUES(?,?,?,?,?,?,?)",
            (decision_id, ticker, side, count, price_cents, fee_cents, filled_ts),
        )
        return cur.lastrowid

    def insert_live_order(self, *, decision_id: int, ticker: str, side: str, count: int,
                          limit_price_cents: int, client_order_id: str, order_id: Optional[str],
                          status: str, submitted_ts: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO live_orders(decision_id, ticker, side, count, limit_price_cents, "
            "client_order_id, order_id, status, submitted_ts) VALUES(?,?,?,?,?,?,?,?,?)",
            (decision_id, ticker, side, count, limit_price_cents, client_order_id,
             order_id, status, submitted_ts),
        )
        return cur.lastrowid

    def update_live_order(self, order_row_id: int, *, status: str,
                          fill_type: Optional[str] = None, fill_price_cents: Optional[int] = None,
                          fee_cents: Optional[int] = None, resolved_ts: Optional[int] = None) -> None:
        self.conn.execute(
            "UPDATE live_orders SET status=?, fill_type=?, fill_price_cents=?, fee_cents=?, "
            "resolved_ts=? WHERE id=?",
            (status, fill_type, fill_price_cents, fee_cents, resolved_ts, order_row_id),
        )

    def upsert_settlement(self, ticker: str, result: str, settled_ts: Optional[int]) -> None:
        self.conn.execute(
            "INSERT INTO settlements(ticker, result, settled_ts, recorded_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(ticker) DO NOTHING",
            (ticker, result, settled_ts, now_ms()),
        )

    def settlement_for(self, ticker: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM settlements WHERE ticker=?", (ticker,)
        ).fetchone()

    # ---- reporting reads (latest / full history; not for models) ----
    def resolved_decisions(self) -> list[sqlite3.Row]:
        """Decisions whose market has since settled, joined to the outcome."""
        return self.conn.execute(
            """
            SELECT d.*, s.result AS settled_result
            FROM decisions d
            JOIN settlements s ON s.ticker = d.ticker
            ORDER BY d.decision_ts
            """
        ).fetchall()
