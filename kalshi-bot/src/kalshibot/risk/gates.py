"""Risk gate chain.

Every OrderIntent passes through this chain before a live order may be placed.
If ANY gate blocks, the live order is suppressed (the paper record is still
written by the caller), and the blocking gate's name + reason are returned so
the decision log records exactly why. Gates are evaluated in order; the first
block wins.

The 1-contract position ceiling is NOT a gate here -- it is a hard-coded
constant in the live engine that config cannot override. The gates below are
the *additional* conditions on top of that ceiling.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from ..execution.intent import OrderIntent
from .circuit_breaker import CircuitBreaker
from .kill_switch import KillSwitch


@dataclass
class GateContext:
    now_ms: int
    book_captured_ts: Optional[int]
    forecast_fetched_ts: Optional[int]
    open_live_markets: int
    account_balance_cents: Optional[int]   # None => balance read failed
    open_exposure_cents: int
    state_is_stale: bool                   # set True right after any reconnect


@dataclass
class GateResult:
    passed: bool
    blocked_by: Optional[str] = None
    reason: Optional[str] = None


class RiskGateChain:
    def __init__(self, risk_cfg: dict, kill_switch: KillSwitch,
                 circuit_breaker: CircuitBreaker) -> None:
        self.cfg = risk_cfg
        self.kill = kill_switch
        self.breaker = circuit_breaker
        # (name, predicate) -- predicate returns a reason string to BLOCK, or None to pass.
        self._gates: list[tuple[str, Callable[[OrderIntent, GateContext], Optional[str]]]] = [
            ("kill_switch", self._kill_switch),
            ("circuit_breaker", self._circuit_breaker),
            ("min_edge", self._min_edge),
            ("price_band", self._price_band),
            ("max_open_markets", self._max_open_markets),
            ("max_exposure", self._max_exposure),
            ("staleness", self._staleness),
            ("balance_sanity", self._balance_sanity),
        ]

    def evaluate(self, intent: OrderIntent, ctx: GateContext) -> GateResult:
        for name, gate in self._gates:
            reason = gate(intent, ctx)
            if reason is not None:
                return GateResult(passed=False, blocked_by=name, reason=reason)
        return GateResult(passed=True)

    # --- individual gates ---
    def _kill_switch(self, intent, ctx) -> Optional[str]:
        return f"kill switch file present at {self.kill.path}" if self.kill.engaged() else None

    def _circuit_breaker(self, intent, ctx) -> Optional[str]:
        return "daily-loss circuit breaker tripped" if self.breaker.is_tripped() else None

    def _min_edge(self, intent, ctx) -> Optional[str]:
        min_edge = float(self.cfg.get("min_edge_after_fees", 0.05))
        if intent.edge_after_fees < min_edge:
            return f"edge {intent.edge_after_fees:.3f} < min {min_edge:.3f}"
        return None

    def _price_band(self, intent, ctx) -> Optional[str]:
        lo = float(self.cfg.get("price_band_reject_low", 0.40)) * 100
        hi = float(self.cfg.get("price_band_reject_high", 0.60)) * 100
        if lo <= intent.limit_price_cents <= hi:
            return f"price {intent.limit_price_cents}c in reject band [{lo:.0f},{hi:.0f}]"
        return None

    def _max_open_markets(self, intent, ctx) -> Optional[str]:
        cap = int(self.cfg.get("max_open_markets", 3))
        if ctx.open_live_markets >= cap:
            return f"open live markets {ctx.open_live_markets} >= cap {cap}"
        return None

    def _max_exposure(self, intent, ctx) -> Optional[str]:
        if ctx.account_balance_cents is None:
            return "balance unknown; cannot check exposure cap"
        if ctx.account_balance_cents == 0:
            # Say the actual cause. "cap 0c" is arithmetically true but reads
            # like a misconfigured limit rather than an unfunded account.
            return ("account balance is $0.00, so any exposure exceeds the cap -- "
                    "fund the account manually in Kalshi before enabling live trading")
        pct = float(self.cfg.get("max_total_exposure_pct", 0.02))
        cap_cents = pct * ctx.account_balance_cents
        prospective = ctx.open_exposure_cents + intent.limit_price_cents  # 1 contract
        if prospective > cap_cents:
            return f"exposure {prospective}c > cap {cap_cents:.0f}c ({pct*100:.1f}% of balance)"
        return None

    def _staleness(self, intent, ctx) -> Optional[str]:
        if ctx.state_is_stale:
            return "state marked stale (post-reconnect); awaiting resync"
        max_book = int(self.cfg.get("max_book_age_seconds", 120)) * 1000
        max_fc = int(self.cfg.get("max_forecast_age_seconds", 21600)) * 1000
        if ctx.book_captured_ts is None or (ctx.now_ms - ctx.book_captured_ts) > max_book:
            return "order book snapshot too old (or missing)"
        if ctx.forecast_fetched_ts is None or (ctx.now_ms - ctx.forecast_fetched_ts) > max_fc:
            return "forecast data too old (or missing)"
        return None

    def _balance_sanity(self, intent, ctx) -> Optional[str]:
        if ctx.account_balance_cents is None:
            return "account balance read failed; refusing to trade on assumed balance"
        if ctx.account_balance_cents < 0:
            return f"implausible balance {ctx.account_balance_cents}c"
        return None
