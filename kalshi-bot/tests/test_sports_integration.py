"""Full sports decision pipeline against fixture data, live execution disabled.

The sports twin of test_integration_loop.py, exercising the real chain: parse
market -> model.predict (reading ratings, the linked game, and the book from the
DB via an AsOfView) -> decide -> real risk gates -> paper record + decision log.
No network, no mocked gates, no live orders.
"""
from kalshibot.config import LeagueConfig
from kalshibot.execution.decider import decide
from kalshibot.execution.paper_engine import PaperEngine
from kalshibot.models.base import MarketContext
from kalshibot.models.sports import SportsEnsembleModel, parse_game_market
from kalshibot.risk.circuit_breaker import CircuitBreaker
from kalshibot.risk.gates import GateContext, RiskGateChain
from kalshibot.risk.kill_switch import KillSwitch
from kalshibot.runtime.families import SportsFamily
from kalshibot.util import now_ms

MLB = LeagueConfig(name="mlb", kalshi_series="KXMLBGAME", espn_path="baseball/mlb",
                   margin_sigma=4.4, home_advantage_margin=0.25, regulation_periods=9)

GAME_KEY = "KXMLBGAME-26JUL282210SEALAD"
# Verbatim shape from the live API; the YES team is the ticker suffix (LAD).
RAW_MARKET = {"ticker": f"{GAME_KEY}-LAD", "event_ticker": GAME_KEY,
              "title": "Seattle vs Los Angeles D Winner?",
              "yes_sub_title": "Los Angeles D", "no_sub_title": "Los Angeles D"}

GATE_CFG = {"min_edge_after_fees": 0.05, "price_band_reject_low": 0.40,
            "price_band_reject_high": 0.60, "max_open_markets": 3,
            "max_total_exposure_pct": 0.02, "max_book_age_seconds": 120,
            "max_forecast_age_seconds": 21600, "daily_loss_limit_cents": 500}


def _seed(dao):
    base = now_ms()
    dao.upsert_sports_game(game_key=GAME_KEY, league="mlb", series="KXMLBGAME",
                           away_code="SEA", home_code="LAD",
                           start_ts=base + 3_600_000, source_event_id="401816999")
    # Three members agreeing the home side is roughly 2.5 runs better: about a
    # 71% favourite once ensemble spread is folded in.
    for provider, margin in [("elo", 2.3), ("log5", 2.5), ("pythagorean", 2.7)]:
        dao.insert_sports_rating(provider=provider, game_key=GAME_KEY, league="mlb",
                                 issued_ts=base - 2000, margin_home=margin,
                                 p_home=0.7, raw={})
    # Book: YES ask = 100 - best_no_bid = 30c (cheap YES on a 70% favourite).
    dao.insert_book_snapshot(f"{GAME_KEY}-LAD", captured_ts=base,
                             yes_levels=[[25, 100]], no_levels=[[70, 100]])
    return base + 1000     # decide just after the seeded stamps, as production does


def _run_pipeline(dao, tmp_path, now, *, stale=False, balance=100_000):
    model = SportsEnsembleModel([MLB])
    paper = PaperEngine(dao)
    gates = RiskGateChain(GATE_CFG, KillSwitch(str(tmp_path / "HALT")),
                          CircuitBreaker(str(tmp_path / "breaker"), dao, 500))

    market = parse_game_market(RAW_MARKET, MLB)
    view = dao.as_of(now)
    book = view.latest_book(market.ticker)
    prediction = model.predict(market, MarketContext(view=view, decision_ts=now))
    intent = decide(market, prediction, book, now)

    members = view.latest_sports_ratings_by_provider(market.game_key)
    ctx = GateContext(now_ms=now, book_captured_ts=book.captured_ts,
                      forecast_fetched_ts=max(r.fetched_ts for r in members.values()),
                      open_live_markets=0, account_balance_cents=balance,
                      open_exposure_cents=0, state_is_stale=stale)
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
    return prediction, intent, gate


def test_pipeline_records_paper_and_passes_gates_but_places_no_live_order(dao, tmp_path):
    now = _seed(dao)
    prediction, intent, gate = _run_pipeline(dao, tmp_path, now)

    assert intent.side == "yes"
    assert intent.limit_price_cents == 30      # 100 - best_no_bid(70)
    assert gate.passed
    assert 0.68 < prediction.probability < 0.74

    paper = dao.conn.execute("SELECT * FROM paper_fills").fetchall()
    assert len(paper) == 1
    assert paper[0]["side"] == "yes" and paper[0]["price_cents"] == 30
    assert paper[0]["fee_cents"] == 2          # taker fee at 30c

    # The decision log keeps the members that produced the call, so the report
    # can explain it months later.
    import json
    inputs = json.loads(dao.conn.execute("SELECT inputs FROM decisions").fetchone()["inputs"])
    assert set(inputs["ensemble_members"]) == {"elo", "log5", "pythagorean"}

    # Live path is not invoked in this harness: no live orders exist.
    assert dao.conn.execute("SELECT COUNT(*) AS c FROM live_orders").fetchone()["c"] == 0


def test_pipeline_blocks_live_when_stale_but_still_records_paper(dao, tmp_path):
    now = _seed(dao)
    _, _, gate = _run_pipeline(dao, tmp_path, now, stale=True)

    assert not gate.passed and gate.blocked_by == "staleness"
    assert dao.conn.execute("SELECT COUNT(*) AS c FROM paper_fills").fetchone()["c"] == 1
    d = dao.conn.execute("SELECT gate_passed, blocked_by FROM decisions").fetchone()
    assert d["gate_passed"] == 0 and d["blocked_by"] == "staleness"


# --------------------------------------------------------------------------
# The family seam
# --------------------------------------------------------------------------
class _StubConfig:
    """Only the config surface the family actually reads."""
    market_family = "sports"

    def __init__(self, sports=None, model=None):
        self.sports = sports if sports is not None else {}
        self.model = model if model is not None else {}
        self.raw = {}

    @property
    def leagues(self):
        return [MLB]


def test_sports_family_exposes_the_series_and_parses_its_markets(dao):
    import logging
    family = SportsFamily(_StubConfig(), dao, client=None, logger=logging.getLogger("t"))
    assert family.series_list == ["KXMLBGAME"]
    assert family.parse(RAW_MARKET, "KXMLBGAME").yes_team == "LAD"
    assert family.parse(RAW_MARKET, "KXNFLGAME") is None        # not a series we watch
    assert [t.name for t in family.data_tasks()] == \
        ["schedule", "results", "ratings", "game_states"]


def test_family_input_age_switches_to_the_live_score_once_a_game_starts(dao):
    """Before the first pitch the ratings are the model's freshest input; after
    it, a stale score is the thing that would poison a decision, so the staleness
    gate has to see the score's age instead."""
    import logging
    family = SportsFamily(_StubConfig(), dao, client=None, logger=logging.getLogger("t"))
    market = family.parse(RAW_MARKET, "KXMLBGAME")
    base = now_ms()

    start_ts = base + 60_000
    dao.upsert_sports_game(game_key=GAME_KEY, league="mlb", series="KXMLBGAME",
                           away_code="SEA", home_code="LAD", start_ts=start_ts,
                           source_event_id="401816999")
    dao.insert_sports_rating(provider="elo", game_key=GAME_KEY, league="mlb",
                             issued_ts=base, margin_home=1.0, p_home=0.6, raw={})
    rating_ts = dao.conn.execute("SELECT fetched_ts FROM sports_ratings").fetchone()[0]

    # Before the start: the rating's own age.
    assert family.input_fetched_ts(dao.as_of(base + 1000), market) == rating_ts
    # After the start with no score at all: no usable input.
    assert family.input_fetched_ts(dao.as_of(start_ts + 60_000), market) is None

    dao.insert_sports_game_state(game_key=GAME_KEY, obs_ts=base, state="in",
                                 completed=False, period=2, home_score=1, away_score=0,
                                 raw={})
    state_ts = dao.conn.execute("SELECT captured_ts FROM sports_game_states").fetchone()[0]
    assert family.input_fetched_ts(dao.as_of(start_ts + 60_000), market) == \
        min(rating_ts, state_ts)


def test_family_reports_no_input_when_nothing_has_been_collected(dao):
    import logging
    family = SportsFamily(_StubConfig(), dao, client=None, logger=logging.getLogger("t"))
    market = family.parse(RAW_MARKET, "KXMLBGAME")
    assert family.input_fetched_ts(dao.as_of(now_ms()), market) is None
