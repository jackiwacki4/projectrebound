"""Spread and depth: recording them, and refusing to cross a wide book.

Edge is computed at the ask, which means a wide quote manufactures apparent
edge -- buying at 61 on a market quoted 54/61 is already 3.5c worse than fair
before the model has been right about anything.

Measured across 95 live MLB books (2026-07): within 48 hours of first pitch the
median spread was 1c and nothing exceeded 3c; beyond 48 hours the median was 5c
with 27% wider than 5c. The wide books are the far-dated ones nobody trades yet,
so recording the spread per decision is what lets a later result be attributed
to the right kind of market.
"""
import pytest

from kalshibot.execution.decider import decide
from kalshibot.execution.intent import OrderIntent
from kalshibot.models.base import Market, Prediction
from kalshibot.risk.circuit_breaker import CircuitBreaker
from kalshibot.risk.gates import GateContext, RiskGateChain
from kalshibot.risk.kill_switch import KillSwitch
from kalshibot.storage.dao import BookRow

MARKET = Market(ticker="M", series="S", city=None, threshold_kind="above",
                lower_f=88.0, upper_f=None, target_date="2026-07-24",
                nws_station="KMDW", lat=1.0, lon=2.0)


def _book(yes_levels, no_levels):
    def best_bid(levels):
        return max((p for p, _ in levels), default=None)

    def ask_from(other):
        b = best_bid(other)
        return (100 - b) if b is not None else None

    return BookRow(ticker="M", captured_ts=1, yes_levels=yes_levels, no_levels=no_levels,
                   best_yes_bid=best_bid(yes_levels), best_yes_ask=ask_from(no_levels),
                   best_no_bid=best_bid(no_levels), best_no_ask=ask_from(yes_levels))


def _decide(p, yes_levels, no_levels):
    return decide(MARKET, Prediction(probability=p, uncertainty=None),
                  _book(yes_levels, no_levels), decision_ts=1)


def test_spread_is_recorded_and_is_the_same_from_either_side():
    """A real book: bid 54, ask 61. The YES ask is 100 - best NO bid, so the
    two sides must report one identical spread -- if they disagreed, half the
    decisions would be gated on the wrong number."""
    intent = _decide(0.95, [[54, 500]], [[39, 500]])     # yes 54/61
    assert intent.spread_cents == 7
    # Forcing the other side must produce the same spread.
    other = _decide(0.05, [[54, 500]], [[39, 500]])
    assert other.side == "no" and other.spread_cents == 7


def test_depth_is_read_from_the_side_that_would_fill_us():
    """Buying YES lifts resting NO bids. Reading depth from our own side would
    report the size of the queue we are joining, not the size we can hit."""
    intent = _decide(0.95, [[54, 111]], [[39, 2933], [38, 10]])
    assert intent.side == "yes"
    assert intent.limit_price_cents == 61
    assert intent.depth_at_price == 2933          # the 39c NO bid, not the 54c YES bid


def test_a_one_sided_book_reports_no_spread_rather_than_a_wrong_one():
    intent = _decide(0.95, [], [[39, 500]])
    assert intent is not None
    assert intent.spread_cents is None


def _gates(cfg, tmp_path, dao):
    return RiskGateChain(cfg, KillSwitch(str(tmp_path / "HALT")),
                         CircuitBreaker(str(tmp_path / "breaker"), dao, 500))


BASE_CFG = {"min_edge_after_fees": 0.05, "price_band_reject_low": 0.40,
            "price_band_reject_high": 0.60, "max_open_markets": 3,
            "max_total_exposure_pct": 0.02, "max_book_age_seconds": 120,
            "max_forecast_age_seconds": 21600, "daily_loss_limit_cents": 500}


def _ctx(now=1_000_000):
    return GateContext(now_ms=now, book_captured_ts=now - 1000,
                       forecast_fetched_ts=now - 1000, open_live_markets=0,
                       account_balance_cents=100_000, open_exposure_cents=0,
                       state_is_stale=False)


def _intent(spread, price=30, edge=0.10):
    return OrderIntent(ticker="M", side="yes", limit_price_cents=price,
                       model_probability=0.8, edge_after_fees=edge, decision_ts=1,
                       spread_cents=spread, depth_at_price=500)


def test_spread_gate_blocks_a_book_wider_than_the_limit(dao, tmp_path):
    gates = _gates({**BASE_CFG, "max_spread_cents": 3}, tmp_path, dao)
    blocked = gates.evaluate(_intent(spread=7), _ctx())
    assert not blocked.passed and blocked.blocked_by == "spread"
    assert "7c > max 3c" in blocked.reason
    assert gates.evaluate(_intent(spread=3), _ctx()).passed      # at the limit, allowed


def test_spread_gate_is_off_unless_configured(dao, tmp_path):
    """Existing configs must not change behaviour under them without being edited."""
    gates = _gates(BASE_CFG, tmp_path, dao)
    assert gates.evaluate(_intent(spread=40), _ctx()).passed


def test_unknown_spread_is_treated_as_too_wide_not_as_acceptable(dao, tmp_path):
    """A missing measurement must fail closed -- assuming the permissive value
    is how an untested assumption turns into a loss."""
    gates = _gates({**BASE_CFG, "max_spread_cents": 5}, tmp_path, dao)
    result = gates.evaluate(_intent(spread=None), _ctx())
    assert not result.passed and result.blocked_by == "spread"


def test_min_edge_still_runs_before_the_spread_gate(dao, tmp_path):
    """Gate order matters for the blocked_by attribution the report groups on."""
    gates = _gates({**BASE_CFG, "max_spread_cents": 1}, tmp_path, dao)
    result = gates.evaluate(_intent(spread=9, edge=0.001), _ctx())
    assert result.blocked_by == "min_edge"


def test_decision_row_persists_spread_and_depth(dao):
    """The columns are worthless if the runtime does not fill them in."""
    did = dao.insert_decision(
        decision_ts=1, ticker="M", model_name="m", probability=0.7, uncertainty=None,
        inputs={}, book_snapshot_id=None, best_yes_bid=54, best_yes_ask=61,
        intended_side="yes", intended_price_cents=61, edge_after_fees=0.08,
        spread_cents=7, depth_at_price=2933, gate_passed=False, blocked_by="spread")
    row = dao.conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
    assert row["spread_cents"] == 7
    assert row["depth_at_price"] == 2933


def test_existing_databases_migrate_without_losing_history(tmp_path):
    """Two databases are already collecting. Opening them with the new schema
    must add the columns, not start over."""
    import sqlite3
    from kalshibot.storage.db import connect

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, decision_ts INTEGER NOT NULL,
            ticker TEXT NOT NULL, model_name TEXT NOT NULL, probability REAL NOT NULL,
            uncertainty REAL, inputs TEXT NOT NULL, book_snapshot_id INTEGER,
            best_yes_bid INTEGER, best_yes_ask INTEGER, intended_side TEXT,
            intended_price_cents INTEGER, edge_after_fees REAL,
            gate_passed INTEGER NOT NULL, blocked_by TEXT);
        INSERT INTO decisions(decision_ts, ticker, model_name, probability, inputs,
                              gate_passed) VALUES(1, 'OLD-MKT', 'm', 0.5, '{}', 1);
        """
    )
    old.commit()
    old.close()

    conn = connect(path)                      # runs the migration
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(decisions)").fetchall()}
    assert {"spread_cents", "depth_at_price"} <= cols
    row = conn.execute("SELECT * FROM decisions WHERE ticker='OLD-MKT'").fetchone()
    assert row is not None                    # history survived
    assert row["spread_cents"] is None        # and is honestly blank, not zero
