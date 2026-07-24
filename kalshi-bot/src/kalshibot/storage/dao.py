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

    def latest_forecast(self, station: str, target_date: str) -> Optional[ForecastRow]:
        # We could only "know" a forecast once we had fetched it, so the
        # binding no-lookahead constraint is fetched_ts <= as_of.
        row = self._conn.execute(
            """
            SELECT station, issued_ts, fetched_ts, target_date, forecast_high_f, raw
            FROM forecasts
            WHERE station = ? AND target_date = ? AND fetched_ts <= ?
            ORDER BY issued_ts DESC, fetched_ts DESC
            LIMIT 1
            """,
            (station, target_date, self._as_of),
        ).fetchone()
        if row is None:
            return None
        return ForecastRow(
            station=row["station"],
            issued_ts=row["issued_ts"],
            fetched_ts=row["fetched_ts"],
            target_date=row["target_date"],
            forecast_high_f=row["forecast_high_f"],
            raw=json.loads(row["raw"]),
        )

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
                        raw: dict) -> None:
        self.conn.execute(
            "INSERT INTO forecasts(station, city, issued_ts, fetched_ts, target_date, "
            "forecast_high_f, raw) VALUES(?,?,?,?,?,?,?)",
            (station, city, issued_ts, now_ms(), target_date, forecast_high_f,
             json.dumps(raw)),
        )

    def insert_observation(self, station: str, obs_ts: int, temp_c: Optional[float],
                           raw: dict) -> None:
        self.conn.execute(
            "INSERT INTO observations(station, obs_ts, captured_ts, temp_c, raw) "
            "VALUES(?,?,?,?,?)",
            (station, obs_ts, now_ms(), temp_c, json.dumps(raw)),
        )

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
