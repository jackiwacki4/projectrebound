"""Weather prediction model + temperature-market parsing.

The model is deliberately simple and fully inspectable: treat the settlement
daily-high temperature as Normally distributed around the NWS forecast high,
with a configurable standard deviation, then integrate that distribution over
each market's temperature threshold to get P(YES). When it is wrong, you can
read exactly why from the persisted inputs.

Threshold parsing reads Kalshi's `floor_strike` / `cap_strike` fields. These
field names/semantics must be verified against real weather-market metadata
before trusting live decisions -- see README. If a market cannot be parsed into
a threshold, the model returns None (no opinion) rather than guessing.
"""
from __future__ import annotations

import math
from typing import Any, Optional

from ..config import CityConfig
from .base import Market, MarketContext, Prediction, PredictionModel


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def parse_temperature_market(raw: dict[str, Any], city: CityConfig) -> Optional[Market]:
    """Best-effort parse of a raw Kalshi temperature market into a typed Market.

    Returns None if the threshold can't be determined. UNVERIFIED field schema:
    confirm floor_strike/cap_strike against live market metadata before relying
    on live decisions.
    """
    ticker = raw.get("ticker")
    if not ticker:
        return None
    floor = raw.get("floor_strike")
    cap = raw.get("cap_strike")

    kind: Optional[str] = None
    lower = upper = None
    if floor is not None and cap is not None:
        kind, lower, upper = "between", float(floor), float(cap)
    elif floor is not None:
        kind, lower = "above", float(floor)
    elif cap is not None:
        kind, upper = "below", float(cap)
    else:
        return None  # can't determine threshold -> no opinion

    # Target settlement date: prefer an explicit field, else parse trailing date
    # from the ticker if present. Left as-is when unknown; the model needs it and
    # will return None without it.
    target_date = raw.get("target_date") or raw.get("expiration_date")

    return Market(
        ticker=ticker, series=city.kalshi_series, city=city.name,
        threshold_kind=kind, lower_f=lower, upper_f=upper,
        target_date=target_date, nws_station=city.nws_station,
        lat=city.lat, lon=city.lon,
    )


class WeatherNormalModel(PredictionModel):
    name = "weather_normal"

    def __init__(self, sigma_f: float = 3.0) -> None:
        self.sigma_f = float(sigma_f)

    def predict(self, market: Market, context: MarketContext) -> Optional[Prediction]:
        if not (market.nws_station and market.target_date and market.threshold_kind):
            return None
        fc = context.view.latest_forecast(market.nws_station, market.target_date)
        if fc is None or fc.forecast_high_f is None:
            return None  # nothing known as-of now -> no opinion

        mean = fc.forecast_high_f
        sigma = self.sigma_f
        k = market.threshold_kind
        if k == "above":
            prob = 1.0 - _normal_cdf(market.lower_f, mean, sigma)
        elif k == "below":
            prob = _normal_cdf(market.upper_f, mean, sigma)
        elif k == "between":
            prob = _normal_cdf(market.upper_f, mean, sigma) - _normal_cdf(market.lower_f, mean, sigma)
        else:
            return None
        prob = min(1.0, max(0.0, prob))

        inputs = {
            "forecast_high_f": mean,
            "sigma_f": sigma,
            "threshold_kind": k,
            "lower_f": market.lower_f,
            "upper_f": market.upper_f,
            "forecast_issued_ts": fc.issued_ts,
            "forecast_fetched_ts": fc.fetched_ts,
            "as_of_ms": context.decision_ts,
            "station": market.nws_station,
        }
        return Prediction(probability=prob, uncertainty=sigma, inputs=inputs)
