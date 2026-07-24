"""Turn a model Prediction + current order book into a candidate OrderIntent.

Edge is computed as expected value per contract in DOLLARS, net of the fee, so
it is directly comparable to the risk gate's min_edge_after_fees threshold
(also in dollars, e.g. 0.05 = 5 cents = 5 percentage points on a $1 contract).

Fees are charged at the TAKER rate here on purpose: it is the conservative
assumption for gating (a resting maker order, if it fills, only does better).
"""
from __future__ import annotations

from typing import Optional

from ..models.base import Market, Prediction
from ..storage.dao import BookRow
from .fees import taker_fee_cents
from .intent import OrderIntent


def _ev_dollars(win_prob: float, price_cents: Optional[int]) -> Optional[float]:
    if price_cents is None or price_cents <= 0 or price_cents >= 100:
        return None
    fee = taker_fee_cents(price_cents, 1) / 100.0
    return win_prob * 1.0 - price_cents / 100.0 - fee


def decide(market: Market, prediction: Prediction, book: BookRow,
           decision_ts: int) -> Optional[OrderIntent]:
    """Pick the better side (YES vs NO) by fee-adjusted EV, or None if neither
    side is tradable from the current book."""
    p_yes = prediction.probability

    ev_yes = _ev_dollars(p_yes, book.best_yes_ask)
    ev_no = _ev_dollars(1.0 - p_yes, book.best_no_ask)

    candidates = []
    if ev_yes is not None:
        candidates.append(("yes", book.best_yes_ask, ev_yes, p_yes))
    if ev_no is not None:
        candidates.append(("no", book.best_no_ask, ev_no, 1.0 - p_yes))
    if not candidates:
        return None

    side, price, ev, win_prob = max(candidates, key=lambda c: c[2])
    return OrderIntent(
        ticker=market.ticker,
        side=side,
        limit_price_cents=price,
        model_probability=win_prob,
        edge_after_fees=ev,
        decision_ts=decision_ts,
        inputs=prediction.inputs,
        uncertainty=prediction.uncertainty,
    )
