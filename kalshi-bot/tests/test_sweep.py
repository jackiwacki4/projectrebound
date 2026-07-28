"""Parameter sweep over collected decisions.

The sweep exists so that choosing a threshold does not require another week of
waiting. These tests pin the two properties that make it honest: one entry per
market (not one per poll), and no silent pass for rows that predate spread
recording.
"""
from kalshibot.reporting.sweep import generate_sweep, load_candidates, simulate


def _seed(dao, ticker, *, price, edge, spread, result, ts, side="yes", close=None):
    dao.upsert_market(ticker=ticker, series="S", city=None, title="t",
                      close_ts=close or ts + 1000, status="settled")
    dao.insert_decision(decision_ts=ts, ticker=ticker, model_name="m", probability=0.7,
                        uncertainty=None, inputs={}, book_snapshot_id=None,
                        best_yes_bid=None, best_yes_ask=None, intended_side=side,
                        intended_price_cents=price, edge_after_fees=edge,
                        spread_cents=spread, depth_at_price=100,
                        gate_passed=False, blocked_by="min_edge")
    dao.upsert_settlement(ticker, result, close or ts + 1000)


def test_one_entry_per_market_however_many_times_it_was_repriced(dao):
    """The same market polled 500 times is one opportunity. Counting each poll
    would multiply both the wins and the fees by how long it stayed open."""
    for i in range(500):
        _seed(dao, "MKT-A", price=30, edge=0.10, spread=2, result="yes", ts=1000 + i)
    cands = load_candidates(dao)
    assert len(cands) == 500                     # every candidate is loaded
    out = simulate(cands, min_edge=0.05, max_spread=5, stake_cents=1000)
    assert out.entered == 1                      # but only one entry is taken
    assert out.pnl_1c == 70 - 2                  # 70c win less the 2c taker fee


def test_the_first_qualifying_candidate_is_the_entry(dao):
    """Entering at the best price seen in hindsight would be lookahead. Note the
    first price is 70c, not 60c: 60c sits inside the 40-60 reject band and would
    be skipped for that reason instead, which would not test anything."""
    _seed(dao, "MKT-A", price=70, edge=0.10, spread=2, result="yes", ts=1000)
    _seed(dao, "MKT-A", price=20, edge=0.30, spread=2, result="yes", ts=2000)
    out = simulate(load_candidates(dao), min_edge=0.05, max_spread=5, stake_cents=1000)
    assert out.entered == 1
    assert out.pnl_1c == (100 - 70) - 2          # the 70c entry, not the cheaper later one


def test_a_wider_spread_limit_admits_more_markets(dao):
    _seed(dao, "TIGHT", price=30, edge=0.10, spread=2, result="yes", ts=1000)
    _seed(dao, "WIDE", price=30, edge=0.10, spread=8, result="yes", ts=1000)
    cands = load_candidates(dao)
    assert simulate(cands, min_edge=0.05, max_spread=3, stake_cents=1000).entered == 1
    assert simulate(cands, min_edge=0.05, max_spread=9, stake_cents=1000).entered == 2
    assert simulate(cands, min_edge=0.05, max_spread=None, stake_cents=1000).entered == 2


def test_rows_without_a_recorded_spread_never_satisfy_a_spread_limit(dao):
    """Two days of history predate spread recording. Treating a missing
    measurement as "narrow enough" would invent a result out of nothing."""
    _seed(dao, "OLD", price=30, edge=0.10, spread=None, result="yes", ts=1000)
    cands = load_candidates(dao)
    assert simulate(cands, min_edge=0.05, max_spread=5, stake_cents=1000).entered == 0
    assert simulate(cands, min_edge=0.05, max_spread=None, stake_cents=1000).entered == 1


def test_price_band_is_respected(dao):
    _seed(dao, "MID", price=50, edge=0.10, spread=1, result="yes", ts=1000)
    out = simulate(load_candidates(dao), min_edge=0.05, max_spread=5, stake_cents=1000)
    assert out.entered == 0


def test_losses_are_counted(dao):
    _seed(dao, "MKT-A", price=80, edge=0.10, spread=1, result="no", ts=1000)
    out = simulate(load_candidates(dao), min_edge=0.05, max_spread=5, stake_cents=1000)
    assert out.entered == 1 and out.wins == 0
    assert out.pnl_1c == -(80 + 2)


def test_sweep_report_splits_the_history_in_half(dao):
    """A setting that only works in one half is overfitting, and the report has
    to make that visible rather than presenting one flattering table."""
    for i in range(20):
        _seed(dao, f"MKT-{i}", price=30, edge=0.10, spread=2,
              result="yes" if i % 2 else "no", ts=1000 + i * 1000)
    text = generate_sweep(dao)
    assert "-- All history --" in text
    assert "-- First half of the history --" in text
    assert "Second half" in text
    assert "The best cell is not a discovery" in text


def test_sweep_says_so_when_no_spread_has_been_recorded_yet(dao):
    _seed(dao, "OLD", price=30, edge=0.10, spread=None, result="yes", ts=1000)
    text = generate_sweep(dao)
    assert "no candidate has a recorded spread" in text


def test_sweep_survives_an_empty_database(dao):
    assert "nothing to sweep" in generate_sweep(dao)
