"""Risk gate chain tests. The real gates are exercised -- never mocked away."""
import pytest

from kalshibot.execution.intent import OrderIntent
from kalshibot.risk.circuit_breaker import CircuitBreaker
from kalshibot.risk.gates import GateContext, RiskGateChain
from kalshibot.risk.kill_switch import KillSwitch


def make_chain(tmp_path, dao, risk_overrides=None):
    risk = {
        "min_edge_after_fees": 0.05,
        "price_band_reject_low": 0.40,
        "price_band_reject_high": 0.60,
        "max_open_markets": 3,
        "max_total_exposure_pct": 0.02,
        "max_book_age_seconds": 120,
        "max_forecast_age_seconds": 21600,
        "daily_loss_limit_cents": 500,
    }
    risk.update(risk_overrides or {})
    kill = KillSwitch(str(tmp_path / "HALT"))
    breaker = CircuitBreaker(str(tmp_path / "breaker"), dao, 500)
    return RiskGateChain(risk, kill, breaker), kill, breaker


def good_intent(price=30, edge=0.10):
    return OrderIntent(ticker="MKT", side="yes", limit_price_cents=price,
                       model_probability=0.7, edge_after_fees=edge, decision_ts=1000)


def fresh_ctx(now=1_000_000, **over):
    ctx = dict(now_ms=now, book_captured_ts=now - 1000, forecast_fetched_ts=now - 1000,
               open_live_markets=0, account_balance_cents=100_000,
               open_exposure_cents=0, state_is_stale=False)
    ctx.update(over)
    return GateContext(**ctx)


def test_all_gates_pass_for_a_clean_intent(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    assert chain.evaluate(good_intent(), fresh_ctx()).passed


def test_kill_switch_blocks(tmp_path, dao):
    chain, kill, _ = make_chain(tmp_path, dao)
    (tmp_path / "HALT").write_text("stop")
    res = chain.evaluate(good_intent(), fresh_ctx())
    assert not res.passed and res.blocked_by == "kill_switch"


def test_circuit_breaker_blocks_when_tripped(tmp_path, dao):
    chain, _, breaker = make_chain(tmp_path, dao)
    (tmp_path / "breaker").write_text("tripped")
    res = chain.evaluate(good_intent(), fresh_ctx())
    assert not res.passed and res.blocked_by == "circuit_breaker"


def test_min_edge_blocks_thin_edge(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    res = chain.evaluate(good_intent(edge=0.04), fresh_ctx())
    assert not res.passed and res.blocked_by == "min_edge"


def test_price_band_blocks_midrange_prices(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    res = chain.evaluate(good_intent(price=50), fresh_ctx())
    assert not res.passed and res.blocked_by == "price_band"


def test_max_open_markets_blocks(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    res = chain.evaluate(good_intent(), fresh_ctx(open_live_markets=3))
    assert not res.passed and res.blocked_by == "max_open_markets"


def test_exposure_cap_blocks(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    # 2% of 100000c = 2000c cap; already 1990c open + 30c order = 2020c > cap.
    res = chain.evaluate(good_intent(price=30), fresh_ctx(open_exposure_cents=1990))
    assert not res.passed and res.blocked_by == "max_exposure"


def test_staleness_blocks_after_reconnect_flag(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    res = chain.evaluate(good_intent(), fresh_ctx(state_is_stale=True))
    assert not res.passed and res.blocked_by == "staleness"


def test_staleness_blocks_old_book(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    res = chain.evaluate(good_intent(), fresh_ctx(book_captured_ts=1_000_000 - 500_000))
    assert not res.passed and res.blocked_by == "staleness"


def test_balance_read_failure_blocks(tmp_path, dao):
    chain, _, _ = make_chain(tmp_path, dao)
    # None balance trips max_exposure first (it needs balance to compute the cap).
    res = chain.evaluate(good_intent(), fresh_ctx(account_balance_cents=None))
    assert not res.passed and res.blocked_by in ("max_exposure", "balance_sanity")
