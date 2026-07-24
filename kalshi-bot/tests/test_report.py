"""Report ledger + dollar-stake scaling + time-to-settlement section."""
from kalshibot.reporting.calibration import (
    _contracts_for_stake,
    _scaled_pnl_cents,
    generate_report,
)


def test_contracts_for_stake():
    assert _contracts_for_stake(30, 1000) == 33     # $10 / 30c
    assert _contracts_for_stake(50, 1000) == 20
    assert _contracts_for_stake(1, 1000) == 1000
    assert _contracts_for_stake(0, 1000) == 0       # guard


def test_scaled_pnl_winning_trade():
    # YES at 30c wins: 33 contracts, gross 70c each = 2310c, taker fee(30,33)=49c.
    contracts, pnl = _scaled_pnl_cents("yes", 30, "yes", 1000)
    assert contracts == 33
    assert pnl == 2310 - 49


def test_scaled_pnl_losing_trade():
    # YES at 30c loses: lose 30c stake * 33 = 990c, plus 49c fee.
    contracts, pnl = _scaled_pnl_cents("yes", 30, "no", 1000)
    assert contracts == 33
    assert pnl == -(990 + 49)


def _seed_trade(dao, ticker, side, price, result, close_ts, decision_ts, prob):
    dao.upsert_market(ticker=ticker, series="KXHIGHCHI", city="Chicago", title="t",
                      close_ts=close_ts, status="settled")
    did = dao.insert_decision(
        decision_ts=decision_ts, ticker=ticker, model_name="weather_ensemble",
        probability=prob, uncertainty=3.0, inputs={}, book_snapshot_id=None,
        best_yes_bid=None, best_yes_ask=None, intended_side=side,
        intended_price_cents=price, edge_after_fees=0.1, gate_passed=True, blocked_by=None,
    )
    dao.insert_paper_fill(decision_id=did, ticker=ticker, side=side, count=1,
                          price_cents=price, fee_cents=2, filled_ts=decision_ts)
    dao.upsert_settlement(ticker, result, close_ts)


def test_report_includes_ledger_and_time_section(dao):
    close = 5_000_000_000_000
    _seed_trade(dao, "MKT-A", "yes", 30, "yes", close, close - 3_600_000, 0.75)   # 1h before, won
    _seed_trade(dao, "MKT-B", "yes", 40, "no", close, close - 100_000_000, 0.55)  # long before, lost

    report = generate_report(dao, stake_dollars=10.0)
    text = report.text
    assert "Trade ledger" in text
    assert "WON $1.00" in text
    assert "LOST $.00" in text
    assert "Model accuracy by time before settlement" in text
    assert "at $10/trade" in text
    assert report.resolved_count == 2


def test_report_survives_empty_db(dao):
    report = generate_report(dao)
    assert report.resolved_count == 0
    assert "SAMPLE TOO SMALL" in report.text
