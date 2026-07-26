"""Main runtime loop.

Collects data continuously and, when data is fresh, runs the decision cycle:
model -> candidate -> paper record (always) -> live order (only if enabled AND
every risk gate passes). Designed to survive sleep/wake, network loss, and API
outages, and to FAIL CLOSED -- any unhandled error in the trading path halts
live trading for the rest of the process while data collection continues.

The loop itself is family-agnostic: which markets to watch, how to parse them,
and which collectors feed the model all come from the MarketFamily selected by
`market_family` in the config (see runtime/families.py).
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from ..clients.kalshi_client import KalshiClient
from ..config import Config, Credentials
from ..execution.decider import decide
from ..execution.live_engine import LiveEngine
from ..execution.paper_engine import PaperEngine
from ..logging_setup import log
from ..models.base import MarketBase, MarketContext
from ..risk.circuit_breaker import CircuitBreaker
from ..risk.gates import GateContext, RiskGateChain
from ..risk.kill_switch import KillSwitch
from ..storage.dao import Dao
from ..storage.db import connect
from ..util import now_ms
from .collectors import MarketCollector
from .families import build_family
from .recovery import StaleState


class TradingSystem:
    def __init__(self, config: Config, creds: Credentials, logger: logging.Logger) -> None:
        self.cfg = config
        self.log = logger
        self.dao = Dao(connect(config.db_path))
        self.client = KalshiClient(creds)
        self.family = build_family(config, self.dao, self.client, logger)
        logger.info(self.family.describe())
        self.market_collector = MarketCollector(self.dao, self.client, logger)
        self.paper = PaperEngine(self.dao)
        self.live = LiveEngine(self.dao, self.client)
        self.model = self.family.model
        self.kill = KillSwitch(config.risk.get("kill_switch_file", "./HALT"))
        self.breaker = CircuitBreaker(
            config.raw.get("storage", {}).get("db_path", "./data/kalshibot.db") + ".breaker",
            self.dao, int(config.risk.get("daily_loss_limit_cents", 500)),
        )
        self.gates = RiskGateChain(config.risk, self.kill, self.breaker)
        self.stale = StaleState()
        self.series_list = self.family.series_list
        self._live_halted = False  # set on unhandled trading-path error (fail closed)
        self._cycle_counts: dict[str, int] = {"decisions": 0, "passed": 0}
        self._cycle_blocks: dict[str, int] = {}

    # ---- scheduling ----
    def run(self) -> None:
        col = self.cfg.collection
        book_period = int(col.get("book_poll_seconds", 60))
        settle_period = int(col.get("settlement_poll_seconds", 300))
        data_tasks = self.family.data_tasks()
        tick = min([book_period, int(col.get("book_poll_seconds_near_settlement", 15))]
                   + [t.period_seconds for t in data_tasks])

        next_book = next_settle = 0.0
        next_data = {t.name: 0.0 for t in data_tasks}
        log(self.log, logging.INFO, "trading system starting",
            live_enabled=self.cfg.live_trading_enabled, family=self.cfg.market_family)
        # State the account balance once at startup rather than implying it via
        # a per-market gate message every cycle.
        bal = self._read_balance()
        if bal is None:
            log(self.log, logging.WARNING, "could not read account balance "
                "(read-only check); live trading would be blocked until this succeeds")
        else:
            log(self.log, logging.INFO, f"account balance ${bal/100:,.2f}"
                + ("  (unfunded -- fine for paper mode; fund manually in Kalshi "
                   "before enabling live trading)" if bal == 0 else ""))

        while True:
            start = time.monotonic()
            self.stale.check_wake_gap(tick)
            now = time.time()
            try:
                for task in data_tasks:
                    if now >= next_data[task.name]:
                        self._safe(task.fn, label=task.name)
                        next_data[task.name] = now + task.period_seconds
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

    def _safe(self, fn, *args, label: Optional[str] = None):
        """Run a collector, converting failure into a stale-mark instead of a crash."""
        name = label or getattr(fn, "__name__", "collector")
        try:
            return fn(*args)
        except Exception as e:
            self.stale.mark_stale(f"{name} failed: {e}")
            log(self.log, logging.WARNING, "collector failed; state marked stale",
                fn=name, error=str(e))
            return None

    # ---- decision cycle ----
    def _decision_cycle(self) -> None:
        try:
            balance = self._read_balance()
            self._cycle_counts = {"decisions": 0, "passed": 0}
            self._cycle_blocks: dict[str, int] = {}
            for series in self.series_list:
                markets = self.client.list_markets(series_ticker=series, status="open")
                for raw in markets:
                    self._decide_one(raw, series, balance)
            self._log_cycle_summary(balance)
        except Exception as e:
            # Fail closed: halt live trading for the rest of the process.
            self._live_halted = True
            log(self.log, logging.ERROR,
                "unhandled error in trading path -- HALTING LIVE, data collection continues",
                error=str(e))

    def _decide_one(self, raw: dict, series: str, balance: Optional[int]) -> None:
        market: Optional[MarketBase] = self.family.parse(raw, series)
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

        ctx = GateContext(
            now_ms=now,
            book_captured_ts=book.captured_ts,
            forecast_fetched_ts=self.family.input_fetched_ts(view, market),
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
        # Paper path: always recorded, regardless of the gates. The gates
        # govern real orders; the paper record is the research data.
        self.paper.record(decision_id, intent)

        self._cycle_counts["decisions"] += 1
        if gate.passed:
            self._cycle_counts["passed"] += 1
        else:
            self._cycle_blocks[gate.blocked_by or "?"] = \
                self._cycle_blocks.get(gate.blocked_by or "?", 0) + 1

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
            # DEBUG, not INFO: this fires for every market on every cycle, which
            # buried real warnings under thousands of routine lines per day. The
            # per-cycle summary below carries the same information, and every
            # block is permanently recorded in the decisions table for the report.
            log(self.log, logging.DEBUG, "gate blocked a would-be live order",
                ticker=market.ticker, gate=gate.blocked_by, reason=gate.reason)

    def _log_cycle_summary(self, balance: Optional[int]) -> None:
        counts, blocks = self._cycle_counts, self._cycle_blocks
        if counts["decisions"] == 0:
            return
        detail = ", ".join(f"{k} x{v}" for k, v in
                           sorted(blocks.items(), key=lambda kv: -kv[1])) or "none"
        mode = "live-enabled" if self.cfg.live_trading_enabled else "paper-only"
        log(self.log, logging.INFO, "scan complete",
            decisions=counts["decisions"], would_pass_gates=counts["passed"],
            blocked_by=detail, mode=mode)

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
