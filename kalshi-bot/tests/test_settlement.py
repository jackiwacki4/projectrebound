"""Settlement + P&L + circuit-breaker persistence tests."""
from kalshibot.reporting.calibration import _pnl_cents, _won
from kalshibot.risk.circuit_breaker import CircuitBreaker


def test_won_logic():
    assert _won("yes", "yes")
    assert _won("no", "no")
    assert not _won("yes", "no")
    assert not _won("no", "yes")


def test_pnl_winning_yes():
    # Buy YES at 30c, 1 contract, fee 1c, resolves YES -> gain 70c - 1c = 69c.
    assert _pnl_cents("yes", 30, 1, 1, "yes") == 69


def test_pnl_losing_yes():
    # Buy YES at 30c, resolves NO -> lose 30c stake + 1c fee = -31c.
    assert _pnl_cents("yes", 30, 1, 1, "no") == -31


def test_pnl_scales_with_count():
    assert _pnl_cents("yes", 30, 2, 10, "yes") == (70 * 10) - 2


def test_settlement_is_write_once(dao):
    dao.upsert_settlement("MKT", "yes", 1000)
    dao.upsert_settlement("MKT", "no", 2000)   # must be ignored
    row = dao.settlement_for("MKT")
    assert row["result"] == "yes"              # first write wins; append-only truth


def test_circuit_breaker_trips_persists_and_resets(tmp_path, dao):
    marker = str(tmp_path / "breaker")
    # A single YES bought at 90c that resolves NO loses 91c; a 50c limit trips.
    breaker = CircuitBreaker(marker, dao, daily_loss_limit_cents=50)

    # A settled live loss beyond the limit today.
    did = dao.insert_decision(
        decision_ts=1, ticker="MKT", model_name="m", probability=0.5, uncertainty=None,
        inputs={}, book_snapshot_id=None, best_yes_bid=None, best_yes_ask=None,
        intended_side="yes", intended_price_cents=90, edge_after_fees=0.1,
        gate_passed=True, blocked_by=None,
    )
    row_id = dao.insert_live_order(
        decision_id=did, ticker="MKT", side="yes", count=1, limit_price_cents=90,
        client_order_id="c", order_id="o", status="submitted", submitted_ts=1,
    )
    dao.update_live_order(row_id, status="filled", fill_type="taker",
                          fill_price_cents=90, fee_cents=1)
    dao.upsert_settlement("MKT", "no", None)   # YES bought at 90c, resolves NO -> -91c

    assert not breaker.is_tripped()
    reason = breaker.evaluate_and_maybe_trip()
    assert reason is not None
    assert breaker.is_tripped()

    # Persists: a fresh breaker instance still sees the marker.
    assert CircuitBreaker(marker, dao, 100).is_tripped()

    # Manual reset clears it.
    assert breaker.reset()
    assert not breaker.is_tripped()
