"""Kalshi trading-fee model.

Source: Kalshi published fee schedule (verified July 2026). The general
trading fee is a function of price, not a flat rate:

    fee = round_up_to_next_cent( coeff * C * P * (1 - P) )

where C = number of contracts, P = price in DOLLARS (0..1), and the result is
rounded UP to the next whole cent (ceiling), computed on the whole order.

  - Taker coefficient: 0.07   (peaks at 1.75c/contract at P=0.50)
  - Maker coefficient: 0.0175 (25% of taker; applies to resting, unmatched orders)
  - No settlement fee. No membership fee.
  - Some premium categories (e.g. crypto/index) carry a higher multiplier.
    Weather markets -- the Phase 1 family -- use the standard 0.07 multiplier.

The 1.75c peak at 50c is the cross-check that pins the coefficient (0.07)
and the ceiling rounding direction: 0.07 * 0.5 * 0.5 = 0.0175 dollars = 1.75c.

IMPORTANT: the live PDF (kalshi.com/docs/kalshi-fee-schedule.pdf) rate-limited
automated fetches when this was written. Re-verify the coefficients and any
per-category multiplier against the current PDF before enabling live trading.
Every EV calc, paper fill, and reported P&L number must be net of this fee.
"""
from __future__ import annotations

from decimal import ROUND_CEILING, Decimal

# Standard weather-market coefficients. Kept as module constants so there is a
# single source of truth; do not scatter magic numbers through the codebase.
TAKER_COEFF = Decimal("0.07")
MAKER_COEFF = Decimal("0.0175")

_HUNDRED = Decimal(100)


def fee_cents(price_cents: int, count: int = 1, *, maker: bool = False,
              coefficient: Decimal | None = None) -> int:
    """Trading fee in whole cents for `count` contracts at `price_cents`.

    price_cents must be in [0, 100]. Returns an integer number of cents,
    rounded UP as the published schedule specifies.
    """
    if not 0 <= price_cents <= 100:
        raise ValueError(f"price_cents must be in [0, 100], got {price_cents}")
    if count < 0:
        raise ValueError(f"count must be non-negative, got {count}")
    if count == 0:
        return 0

    coeff = coefficient if coefficient is not None else (MAKER_COEFF if maker else TAKER_COEFF)

    p = Decimal(price_cents) / _HUNDRED           # price in dollars
    fee_dollars = coeff * Decimal(count) * p * (Decimal(1) - p)
    fee_in_cents = fee_dollars * _HUNDRED         # convert dollars -> cents
    return int(fee_in_cents.quantize(Decimal(1), rounding=ROUND_CEILING))


def taker_fee_cents(price_cents: int, count: int = 1) -> int:
    return fee_cents(price_cents, count, maker=False)


def maker_fee_cents(price_cents: int, count: int = 1) -> int:
    return fee_cents(price_cents, count, maker=True)
