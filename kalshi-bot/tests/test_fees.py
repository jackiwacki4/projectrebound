"""Fee module tests -- the numbers that keep P&L honest.

Cross-check anchors (taker, 1 contract), computed from
fee = ceil_to_cent(0.07 * P * (1-P)):
   1c : 0.07*0.01*0.99 = 0.000693  -> 0.0693c -> ceil -> 1c
  25c : 0.07*0.25*0.75 = 0.013125  -> 1.3125c -> ceil -> 2c
  50c : 0.07*0.50*0.50 = 0.0175    -> 1.75c   -> ceil -> 2c   (boundary)
  75c : symmetric with 25c                     -> 2c
  99c : symmetric with 1c                       -> 1c
"""
import math
from decimal import Decimal

import pytest

from kalshibot.execution.fees import (
    MAKER_COEFF,
    TAKER_COEFF,
    fee_cents,
    maker_fee_cents,
    taker_fee_cents,
)


@pytest.mark.parametrize("price,expected", [
    (1, 1), (25, 2), (50, 2), (75, 2), (99, 1),
])
def test_taker_one_contract(price, expected):
    assert taker_fee_cents(price, 1) == expected


def test_50c_is_the_max_and_rounds_up_from_exactly_1_75():
    # 1.75c is exact; ceiling must push it to 2c, and nothing exceeds this.
    assert taker_fee_cents(50, 1) == 2
    for p in range(0, 101):
        assert taker_fee_cents(p, 1) <= 2


def test_maker_is_cheaper_than_taker_where_the_fee_is_material():
    # At 50c the taker pays 2c; the maker's raw fee is 0.4375c -> ceil 1c.
    assert maker_fee_cents(50, 1) == 1
    assert maker_fee_cents(50, 1) < taker_fee_cents(50, 1)


def test_maker_coeff_is_25pct_of_taker():
    assert MAKER_COEFF == TAKER_COEFF * Decimal("0.25")


def test_zero_and_hundred_cent_edges_have_zero_or_min_fee():
    # At P=0 or P=1 the P*(1-P) term is 0 -> fee 0.
    assert taker_fee_cents(0, 1) == 0
    assert taker_fee_cents(100, 1) == 0


def test_count_scales_before_rounding_not_after():
    # Fee is round-up of the WHOLE order, not per-contract rounding summed.
    # 10 contracts at 50c taker: 0.07*10*0.25 = 0.175 dollars = 17.5c -> 18c.
    assert taker_fee_cents(50, 10) == 18
    # Per-contract-rounded-then-summed would give 10*2 = 20c, which is wrong.
    assert taker_fee_cents(50, 10) != 20


def test_ceiling_direction_never_rounds_down():
    for p in range(1, 100):
        raw = float(TAKER_COEFF) * (p / 100) * (1 - p / 100) * 100
        assert taker_fee_cents(p, 1) >= math.floor(raw)
        assert taker_fee_cents(p, 1) == math.ceil(round(raw, 9))


def test_count_zero_is_free_and_negative_rejected():
    assert taker_fee_cents(50, 0) == 0
    with pytest.raises(ValueError):
        fee_cents(50, -1)


@pytest.mark.parametrize("bad", [-1, 101, 200])
def test_price_out_of_range_rejected(bad):
    with pytest.raises(ValueError):
        fee_cents(bad, 1)
