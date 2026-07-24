"""Main runtime loop.

Collects data continuously and, when data is fresh, runs the decision cycle:
model -> candidate -> paper record (always) -> live order (only if enabled AND
every risk gate passes). Designed to survive sleep/wake, network loss, and API
outages, and to FAIL CLOSED -- any unhandled error in the trading path halts
live trading for the rest of the process while data collection continues.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..clients.kalshi_client import KalshiClient
from ..clients.nws_client import NwsClient
from ..config import CityConfig, Config, Credentials
from ..execution.decider import decide
from ..execution.live_engine import LiveEngine
from ..execution.paper_engine import PaperEngine
from ..logging_setup import log
from ..models.weather import WeatherNormalModel, parse_temperature_market
from ..models.base import MarketContext
from ..risk.circuit_breaker import CircuitBreaker
from ..risk.gates import GateContext, RiskGateChain
from ..risk.kill_switch import KillSwitch
from ..storage.dao import Dao
from ..storage.db import connect
from ..util import now_ms
from .collectors import MarketCollector, WeatherCollector
from .recovery import StaleState


class TradingSystem:
    def __init__(self, config: Config, creds: Credentials, logger: logging.Logger) -> None:
        self.cfg = config
        self.log = logger
        self.dao = Dao(connect(config.db_path))
        self.client = KalshiClient(creds)
        self.nws = NwsClient()
        self.market_collector = MarketCollector(self.dao, self.client, logger)
        self.weather_collector = WeatherCollector(self.dao, self.nws, logger)
        self.paper = PaperEngine(self.dao)
        self.live = LiveEngine(self.dao, self.client)
        self.model = WeatherNormalModel(sigma_f=float(config.model.get("forecast_sigma_f", 3.0)))
        self.kill = KillSwitch(config.risk.get("kill_switch_file", "./HALT"))
        self.breaker = CircuitBreaker(
            config.raw.get("storage", {}).get("db_path", "./data/kalshibot.db") + ".breaker",
            self.dao, int(config.risk.get("daily_loss_limit_cents", 500)),
        )
        self.gates = RiskGateChain(config.risk, self.kill, self.breaker)
        self.stale = StaleState()
        self.series_by_city = {c.kalshi_series: c for c in config.cities}
        self.series_list = [c.kalshi_series for c in config.cities]
        self._live_halted = False  # set on unhandled trading-path error (fail closed)

    # ---- scheduling ----
    def run(self) -> None:
        col = self.cfg.collection
        book_period = int(col.get("book_poll_seconds", 60))
        settle_period = int(col.get("settlement_poll_seconds", 300))
        fc_period = int(self.cfg.weather.get("forecast_poll_seconds", 3600))
        tick = min(book_period, int(col.get("book_poll_seconds_near_settlement", 15)))

        next_book = next_settle = next_fc = 0.0
        log(self.log, logging.INFO, "trading system starting",
            live_enabled=self.cfg.live_trading_enabled, family=self.cfg.market_family)

        while True:
            start = time.monotonic()
            self.stale.check_wake_gap(tick)
            now = time.time()
            try:
                if now >= next_fc:
                    self._safe(self.weather_collector.poll_forecasts, self.cfg.cities)
                    next_fc = now + fc_period
                if now >= next_book:
                    ok = self._safe(self.market_collector.poll_books, self.series_list)
                    self._safe(self.market_collector.poll_trades, self.series_list)
                    if ok is not None:
                        self.stale.mark_fresh()   # a clean sweep = fresh state
                        self._decision_cycle()
                    next_book = now + book_period
                if now >= next_settle:
                    self._safe(self.market_collector.poll_settlements)
                    self._safe(self.breaker.evaluate_and_maybe_trip)
                    next_settle = now + settle_period
            except Exception as e:  # never crash-loop; log and continue collecting
                log(self.log, logging.ERROR, "loop iteration error", error=str(e))

            elapsed = time.monotonic() - start
            time.sleep(max(0.0, tick - elapsed))

    def _safe(self, fn, *args):
        """Run a collector, converting failure into a stale-mark instead of a crash."""
        try:
            return fn(*args)
        except Exception as e:
            self.stale.mark_stale(f"{fn.__name__} failed: {e}")
            log(self.log, logging.WARNING, "collector failed; state marked stale",
                fn=fn.__name__, error=str(e))
            return None

    # ---- decision cycle ----
    def _decision_cycle(self) -> None:
        try:
            balance = self._read_balance()
            for city in self.cfg.cities:
                markets = self.client.list_markets(series_ticker=city.kalshi_series, status="open")
                for raw in markets:
                    self._decide_one(raw, city, balance)
        except Exception as e:
            # Fail closed: halt live trading for the rest of the process.
            self._live_halted = True
            log(self.log, logging.ERROR,
                "unhandled error in trading path -- HALTING LIVE, data collection continues",
                error=str(e))

    def _decide_one(self, raw: dict, city: CityConfig, balance: Optional[int]) -> None:
        market = parse_temperature_market(raw, city)
        if market is None:
            return
        now = now_ms()
        view = self.dao.as_of(now)
        book = view.latest_book(market.ticker)
        if book is None:
            return
        prediction = self.model.predict(market, MarketContext(view=view, decision_ts=now))
        if prediction is None:
            return
        intent = decide(market, prediction, book, now)
        if intent is None:
            return

        fc = view.latest_forecast(market.nws_station, market.target_date) if market.target_date else None
        ctx = GateContext(
            now_ms=now,
            book_captured_ts=book.captured_ts,
            forecast_fetched_ts=fc.fetched_ts if fc else None,
            open_live_markets=self._open_live_markets(),
            account_balance_cents=balance,
            open_exposure_cents=self._open_exposure(),
            state_is_stale=self.stale.is_stale,
        )
        gate = self.gates.evaluate(intent, ctx)

        decision_id = self.dao.insert_decision(
            decision_ts=now, ticker=market.ticker, model_name=self.model.name,
            probability=prediction.probability, uncertainty=prediction.uncertainty,
            inputs=prediction.inputs, book_snapshot_id=None,
            best_yes_bid=book.best_yes_bid, best_yes_ask=book.best_yes_ask,
            intended_side=intent.side, intended_price_cents=intent.limit_price_cents,
            edge_after_fees=intent.edge_after_fees,
            gate_passed=gate.passed, blocked_by=gate.blocked_by,
        )
        # Paper path: always recorded.
        self.paper.record(decision_id, intent)

        # Live path: only if enabled, not halted, and every gate passed.
        if self.cfg.live_trading_enabled and not self._live_halted and gate.passed:
            try:
                self.live.submit(decision_id, intent)
                log(self.log, logging.INFO, "micro-live order submitted (1 contract)",
                    ticker=market.ticker, side=intent.side, price=intent.limit_price_cents)
            except Exception as e:
                self._live_halted = True
                log(self.log, logging.ERROR, "live submit failed -- HALTING LIVE",
                    ticker=market.ticker, error=str(e))
        elif not gate.passed:
            log(self.log, logging.INFO, "live order blocked by risk gate",
                ticker=market.ticker, gate=gate.blocked_by, reason=gate.reason)

    # ---- helpers ----
    def _read_balance(self) -> Optional[int]:
        try:
            return int(self.client.get_balance().get("balance"))
        except Exception:
            return None  # balance-sanity gate will block live on None

    def _open_live_markets(self) -> int:
        row = self.dao.conn.execute(
            "SELECT COUNT(DISTINCT o.ticker) AS c FROM live_orders o "
            "WHERE o.status IN ('submitted','filled') "
            "AND o.ticker NOT IN (SELECT ticker FROM settlements)"
        ).fetchone()
        return row["c"]

    def _open_exposure(self) -> int:
        row = self.dao.conn.execute(
            "SELECT COALESCE(SUM(limit_price_cents),0) AS s FROM live_orders o "
            "WHERE o.status IN ('submitted','filled') "
            "AND o.ticker NOT IN (SELECT ticker FROM settlements)"
        ).fetchone()
        return int(row["s"])
