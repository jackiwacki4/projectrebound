"""The shared order-intent type: one decision produces one OrderIntent, which
is fanned out to both the paper engine and (if gates pass) the live engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class OrderIntent:
    ticker: str
    side: str                 # "yes" | "no"
    limit_price_cents: int    # the price we would pay per contract
    model_probability: float  # model's probability that `side` resolves true
    edge_after_fees: float    # model_probability - implied_price - fee, in prob units
    decision_ts: int
    inputs: dict[str, Any] = field(default_factory=dict)
    uncertainty: Optional[float] = None
    # What it costs to get in, and whether anyone is there to trade with.
    # `edge_after_fees` is computed at the ask, so it already pays the spread --
    # but without recording the spread there is no way to tell afterwards
    # whether an "edge" was a real disagreement with the market or just a wide
    # quote. Live MLB books (2026-07, n=95): 1c median within 48h of first
    # pitch, 5c median beyond it -- so the answer depends entirely on WHICH
    # markets a result came from, which is why it is stored per decision.
    spread_cents: Optional[int] = None
    depth_at_price: Optional[int] = None
    # The full intended size for the PAPER path. The live path always uses 1
    # contract regardless of this value (hard-coded in the live engine).
    intended_paper_count: int = 1
