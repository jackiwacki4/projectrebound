"""Full decision pipeline against fixture data, live execution disabled.

Exercises the real chain: parse market -> model.predict (reading forecast + book
from the DB via an AsOfView) -> decide -> real risk gates -> paper record +
decision log. No network, no mocked gates, no live orders.
"""
from kalshibot.config import CityConfig
from kalshibot.execution.decider import decide
from kalshibot.execution.paper_engine import PaperEngine
from kalshibot.models.base import MarketContext
from kalshibot.models.weather import WeatherNormalModel, parse_temperature_market
from kalshibot.risk.circuit_breaker import CircuitBreaker
from kalshibot.risk.gates import GateContext, RiskGateChain
from kalshibot.risk.kill_switch import KillSwitch
from kalshibot.util import now_ms

CITY = CityConfig(name="Chicago", kalshi_series="KXHIGHCHI", nws_station="KMDW",
                  lat=41.786, lon=-87.752)
RAW_MARKET = {"ticker": "KXHIGHCHI-26JUL24-B88", "floor_strike": 88,
              "target_date": "2026-07-24", "title": "Chicago high above 88F"}


def _seed(dao):
    # Use real wall-clock time: insert_forecast stamps fetched_ts with now_ms(),
    # and the decision `now` (returned below) is set just after, so the AsOfView
    # can see this fixture while still enforcing no-lookahead in general.
    base = now_ms()
    dao.insert_forecast(station="KMDW", city="Chicago", issued_ts=base - 2000,
                        target_date="2026-07-24", forecast_high_f=90.0, raw={})
    # Book: YES ask = 100 - best_no_bid = 30c (cheap YES, big edge).
    dao.insert_book_snapshot("KXHIGHCHI-26JUL24-B88", captured_ts=base,
                             yes_levels=[[25, 100]], no_levels=[[70, 100]])
    return base + 1000  # decision time, safely after the seeded fetched/captured stamps


def _run_pipeline(dao, tmp_path, now, *, stale=False, balance=100_000):
    model = WeatherNormalModel(sigma_f=3.0)
    paper = PaperEngine(dao)
    kill = KillSwitch(str(tmp_path / "HALT"))
    breaker = CircuitBreaker(str(tmp_path / "breaker"), dao, 500)
    gates = RiskGateChain(
        {"min_edge_after_fees": 0.05, "price_band_reject_low": 0.40,
         "price_band_reject_high": 0.60, "max_open_markets": 3,
         "max_total_exposure_pct": 0.02, "max_book_age_seconds": 120,
         "max_forecast_age_seconds": 21600, "daily_loss_limit_cents": 500},
        kill, breaker,
    )

    market = parse_temperature_market(RAW_MARKET, CITY)
    view = dao.as_of(now)
    book = view.latest_book(market.ticker)
    prediction = model.predict(market, MarketContext(view=view, decision_ts=now))
    intent = decide(market, prediction, book, now)

    fc = view.latest_forecast("KMDW", "2026-07-24")
    ctx = GateContext(now_ms=now, book_captured_ts=book.captured_ts,
                      forecast_fetched_ts=fc.fetched_ts, open_live_markets=0,
                      account_balance_cents=balance, open_exposure_cents=0,
                      state_is_stale=stale)
    gate = gates.evaluate(intent, ctx)

    decision_id = dao.insert_decision(
        decision_ts=now, ticker=market.ticker, model_name=model.name,
        probability=prediction.probability, uncertainty=prediction.uncertainty,
        inputs=prediction.inputs, book_snapshot_id=None,
        best_yes_bid=book.best_yes_bid, best_yes_ask=book.best_yes_ask,
        intended_side=intent.side, intended_price_cents=intent.limit_price_cents,
        edge_after_fees=intent.edge_after_fees, gate_passed=gate.passed,
        blocked_by=gate.blocked_by,
    )
    paper.record(decision_id, intent)
    return intent, gate


def test_pipeline_records_paper_and_passes_gates_but_places_no_live_order(dao, tmp_path):
    now = _seed(dao)
    intent, gate = _run_pipeline(dao, tmp_path, now)

    assert intent.side == "yes"
    assert intent.limit_price_cents == 30      # 100 - best_no_bid(70)
    assert gate.passed

    # Model probability ~0.7475 for high>=88 given mean 90, sigma 3.
    p = dao.conn.execute("SELECT probability FROM decisions").fetchone()["probability"]
    assert 0.74 < p < 0.76

    paper = dao.conn.execute("SELECT * FROM paper_fills").fetchall()
    assert len(paper) == 1
    assert paper[0]["side"] == "yes" and paper[0]["price_cents"] == 30
    assert paper[0]["fee_cents"] == 2          # taker fee at 30c

    # Live path is not invoked in this harness: no live orders exist.
    assert dao.conn.execute("SELECT COUNT(*) AS c FROM live_orders").fetchone()["c"] == 0


def test_pipeline_blocks_live_when_stale_but_still_records_paper(dao, tmp_path):
    now = _seed(dao)
    intent, gate = _run_pipeline(dao, tmp_path, now, stale=True)

    assert not gate.passed and gate.blocked_by == "staleness"
    # Paper record is written even though the live order would be blocked.
    assert dao.conn.execute("SELECT COUNT(*) AS c FROM paper_fills").fetchone()["c"] == 1
    d = dao.conn.execute("SELECT gate_passed, blocked_by FROM decisions").fetchone()
    assert d["gate_passed"] == 0 and d["blocked_by"] == "staleness"
