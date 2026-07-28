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
    assert "Every candidate considered" in text
    assert "NOT a strategy" in text
    assert "WON $1.00" in text
    assert "LOST $.00" in text
    assert "Model accuracy by time before settlement" in text
    assert "at $10 each" in text          # the stake-scaling column still renders
    assert report.resolved_count == 2


def test_report_survives_empty_db(dao):
    report = generate_report(dao)
    assert report.resolved_count == 0
    assert "SAMPLE TOO SMALL" in report.text


# --------------------------------------------------------------------------
# Strategy section: the one number that describes something someone would run.
#
# The paper ledger cannot answer "is there edge" -- `decide()` has no minimum-EV
# filter and a paper record is written every polling cycle regardless of the
# gates, so the same market appears hundreds of times and pays the fee each
# time. These tests pin the section that collapses that back to one entry per
# market, gates enforced.
# --------------------------------------------------------------------------
def _seed_decision(dao, ticker, *, side, price, decision_ts, gate_passed,
                   prob=0.7, edge=0.10, blocked_by=None):
    did = dao.insert_decision(
        decision_ts=decision_ts, ticker=ticker, model_name="m", probability=prob,
        uncertainty=1.0, inputs={}, book_snapshot_id=None, best_yes_bid=None,
        best_yes_ask=None, intended_side=side, intended_price_cents=price,
        edge_after_fees=edge, gate_passed=gate_passed, blocked_by=blocked_by)
    dao.insert_paper_fill(decision_id=did, ticker=ticker, side=side, count=1,
                          price_cents=price, fee_cents=2, filled_ts=decision_ts)


def test_strategy_counts_each_market_once_however_often_it_was_repriced(dao):
    """900 polls of one game is one bet, not 900 -- and the fee is paid once."""
    close = 5_000_000_000_000
    dao.upsert_market(ticker="MKT-A", series="S", city=None, title="t",
                      close_ts=close, status="settled")
    for i in range(900):
        _seed_decision(dao, "MKT-A", side="yes", price=30 + (i % 5),
                       decision_ts=close - 900_000 + i * 1000, gate_passed=True)
    dao.upsert_settlement("MKT-A", "yes", close)

    text = generate_report(dao).text
    assert "markets entered : 1 of 1" in text
    assert "win rate        : 100.0%  (1/1)" in text
    # Entry is the FIRST qualifying decision (30c), so P&L is 70c less the fee.
    assert "P&L at 1 each   : $0.68" in text


def test_strategy_ignores_markets_the_gates_blocked(dao):
    close = 5_000_000_000_000
    for ticker, passed in (("MKT-PASS", True), ("MKT-BLOCKED", False)):
        dao.upsert_market(ticker=ticker, series="S", city=None, title="t",
                          close_ts=close, status="settled")
        _seed_decision(dao, ticker, side="yes", price=40, decision_ts=close - 1000,
                       gate_passed=passed, blocked_by=None if passed else "min_edge")
        dao.upsert_settlement(ticker, "yes", close)

    text = generate_report(dao).text
    assert "markets entered : 1 of 2" in text
    assert "(1 never cleared the gates)" in text


def test_strategy_says_so_plainly_when_nothing_cleared_the_gates(dao):
    """The sports family's real result on day one: zero qualifying trades. That
    is the gates working, and the report must not read like a malfunction."""
    close = 5_000_000_000_000
    dao.upsert_market(ticker="MKT-A", series="S", city=None, title="t",
                      close_ts=close, status="settled")
    _seed_decision(dao, "MKT-A", side="yes", price=50, decision_ts=close - 1000,
                   gate_passed=False, blocked_by="min_edge")
    dao.upsert_settlement("MKT-A", "yes", close)

    text = generate_report(dao).text
    assert "No market cleared every risk gate, out of 1 settled." in text
    assert "the correct number of trades is zero" in text


def test_strategy_losses_are_counted_at_the_entry_price(dao):
    close = 5_000_000_000_000
    dao.upsert_market(ticker="MKT-A", series="S", city=None, title="t",
                      close_ts=close, status="settled")
    _seed_decision(dao, "MKT-A", side="yes", price=80, decision_ts=close - 1000,
                   gate_passed=True)
    dao.upsert_settlement("MKT-A", "no", close)      # lost

    text = generate_report(dao).text
    assert "win rate        : 0.0%  (0/1)" in text
    assert "P&L at 1 each   : -$0.82" in text        # 80c stake + 2c taker fee


def test_ledger_shows_enough_of_the_ticker_to_tell_both_sides_apart(dao):
    """Truncating at 20 chars made KXMLBGAME-...HOULAA-HOU and -LAA identical,
    so the ledger looked like it had taken both sides of the same bet."""
    close = 5_000_000_000_000
    for ticker in ("KXMLBGAME-26JUL272138HOULAA-HOU", "KXMLBGAME-26JUL272138HOULAA-LAA"):
        dao.upsert_market(ticker=ticker, series="S", city=None, title="t",
                          close_ts=close, status="settled")
        _seed_decision(dao, ticker, side="yes", price=50, decision_ts=close - 1000,
                       gate_passed=True)
        dao.upsert_settlement(ticker, "yes", close)

    text = generate_report(dao).text
    assert "KXMLBGAME-26JUL272138HOULAA-HOU" in text
    assert "KXMLBGAME-26JUL272138HOULAA-LAA" in text
