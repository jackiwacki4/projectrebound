"""Market parsing + threshold boundaries, against REAL Kalshi payloads.

Every fixture below is a verbatim field-set captured from the live Kalshi API
(KXHIGHCHI, 2026-07). Three bugs these tests exist to prevent regressing:

1. Kalshi exposes NO target/expiration date field on weather markets -- both
   are null. The date lives only in the ticker. Without parsing it, the model
   silently declines every market and collects data forever with zero decisions.
2. Thresholds are EXCLUSIVE of the strike ("floor_strike 96" means "97 or
   above"), so probabilities must integrate at half-degree boundaries. Being a
   degree off is enormous next to the edge being hunted.
3. `strike_type` is authoritative for which comparison applies.
"""
import pytest

from kalshibot.config import CityConfig
from kalshibot.models.base import MarketContext
from kalshibot.models.weather import (
    WeatherNormalModel,
    parse_temperature_market,
    target_date_from_ticker,
)
from kalshibot.util import now_ms

CITY = CityConfig(name="Chicago", kalshi_series="KXHIGHCHI",
                  nws_station="KMDW", lat=41.786, lon=-87.752)

GREATER = {"ticker": "KXHIGHCHI-26JUL26-T96", "floor_strike": 96, "cap_strike": None,
           "strike_type": "greater", "yes_sub_title": "97° or above",
           "target_date": None, "expiration_date": None}
LESS = {"ticker": "KXHIGHCHI-26JUL26-T89", "floor_strike": None, "cap_strike": 89,
        "strike_type": "less", "yes_sub_title": "88° or below",
        "target_date": None, "expiration_date": None}
BETWEEN = {"ticker": "KXHIGHCHI-26JUL26-B95.5", "floor_strike": 95, "cap_strike": 96,
           "strike_type": "between", "yes_sub_title": "95° to 96°",
           "target_date": None, "expiration_date": None}


@pytest.mark.parametrize("ticker,expected", [
    ("KXHIGHCHI-26JUL26-T96", "2026-07-26"),
    ("KXHIGHNY-26JUL26-T88", "2026-07-26"),
    ("KXHIGHCHI-26JAN01-B10.5", "2026-01-01"),
    ("KXHIGHCHI-25DEC31-T40", "2025-12-31"),
])
def test_target_date_parsed_from_ticker(ticker, expected):
    assert target_date_from_ticker(ticker) == expected


@pytest.mark.parametrize("ticker", ["NODATE-T96", "KXHIGHCHI-26XXX26-T96", "", "KXHIGHCHI"])
def test_bad_tickers_yield_no_date(ticker):
    assert target_date_from_ticker(ticker) is None


def test_real_markets_parse_with_a_date_despite_null_date_fields():
    for raw in (GREATER, LESS, BETWEEN):
        m = parse_temperature_market(raw, CITY)
        assert m is not None, f"failed to parse {raw['ticker']}"
        assert m.target_date == "2026-07-26"


def test_strike_types_map_to_the_right_comparison():
    assert parse_temperature_market(GREATER, CITY).threshold_kind == "above"
    assert parse_temperature_market(LESS, CITY).threshold_kind == "below"
    assert parse_temperature_market(BETWEEN, CITY).threshold_kind == "between"


def test_unparseable_market_declines_rather_than_guessing():
    assert parse_temperature_market({"ticker": "X-26JUL26-T1"}, CITY) is None   # no strikes
    assert parse_temperature_market({"floor_strike": 90}, CITY) is None          # no ticker


def _predict(dao, raw, forecast_high, sigma=3.0):
    now = now_ms()
    dao.insert_forecast(station="KMDW", city="Chicago", issued_ts=now - 1000,
                        target_date="2026-07-26", forecast_high_f=forecast_high,
                        raw={}, provider="nws")
    market = parse_temperature_market(raw, CITY)
    ctx = MarketContext(view=dao.as_of(now + 1000), decision_ts=now + 1000)
    return WeatherNormalModel(sigma_f=sigma).predict(market, ctx)


def test_greater_uses_exclusive_boundary(dao):
    """floor_strike=96 means "97 or above". With a forecast of exactly 96.5 the
    answer must be 50% -- if the boundary were read as 96 it would be ~57%."""
    pred = _predict(dao, GREATER, forecast_high=96.5)
    assert pred.probability == pytest.approx(0.5, abs=1e-6)


def test_less_uses_exclusive_boundary(dao):
    """cap_strike=89 means "88 or below": the 50/50 point is 88.5, not 89."""
    pred = _predict(dao, LESS, forecast_high=88.5)
    assert pred.probability == pytest.approx(0.5, abs=1e-6)


def test_between_spans_the_full_inclusive_bracket(dao):
    """95..96 inclusive spans 94.5->96.5, i.e. a full 2 degrees of width --
    naive cdf(96)-cdf(95) would cover only 1 degree and halve the probability."""
    pred = _predict(dao, BETWEEN, forecast_high=95.5, sigma=3.0)
    from kalshibot.models.weather import _normal_cdf
    expected = _normal_cdf(96.5, 95.5, 3.0) - _normal_cdf(94.5, 95.5, 3.0)
    assert pred.probability == pytest.approx(expected, abs=1e-9)
    assert pred.probability > 0.25   # sanity: meaningfully more than the naive value


def test_observation_clamp_respects_exclusive_threshold(dao):
    """Observed 96 does NOT settle ">96" -- that needs 97."""
    now = now_ms()
    dao.insert_forecast(station="KMDW", city="Chicago", issued_ts=now - 1000,
                        target_date="2026-07-26", forecast_high_f=95.0, raw={}, provider="nws")
    market = parse_temperature_market(GREATER, CITY)
    # 96F observed; the clamp must not force certainty.
    dao.insert_observation("KMDW", obs_ts=now - 500, temp_c=(96 - 32) * 5 / 9, raw={})
    ctx = MarketContext(view=dao.as_of(now + 1000), decision_ts=now + 1000)
    pred = WeatherNormalModel(sigma_f=3.0).predict(market, ctx)
    assert pred.probability < 1.0
