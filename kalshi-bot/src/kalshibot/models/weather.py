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
import re
import statistics
from datetime import date, datetime
from typing import Any, Optional

from ..config import CityConfig
from .base import Market, MarketContext, Prediction, PredictionModel


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    if sigma <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (sigma * math.sqrt(2.0))))


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _local_day_bounds_ms(target_date: str) -> tuple[int, int]:
    """Local-time [midnight, 23:59:59.999] for a YYYY-MM-DD, in Unix ms."""
    start = datetime.fromisoformat(target_date + "T00:00:00")
    end = datetime.fromisoformat(target_date + "T23:59:59")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_TICKER_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})-")


def target_date_from_ticker(ticker: str) -> Optional[str]:
    """Extract the settlement date from a Kalshi weather ticker.

    Kalshi does NOT expose a target/expiration date field on these markets
    (verified against live data: both are null) -- the date lives in the middle
    ticker segment, e.g. KXHIGHCHI-26JUL26-T96 -> 2026-07-26.
    """
    m = _TICKER_DATE_RE.search(ticker)
    if not m:
        return None
    yy, mon, dd = m.group(1), m.group(2).upper(), m.group(3)
    month = _MONTHS.get(mon)
    if month is None:
        return None
    try:
        return date(2000 + int(yy), month, int(dd)).isoformat()
    except ValueError:
        return None


def parse_temperature_market(raw: dict[str, Any], city: CityConfig) -> Optional[Market]:
    """Parse a raw Kalshi temperature market into a typed Market.

    Field semantics verified against live KXHIGHCHI/KXHIGHNY market data
    (2026-07). `strike_type` is authoritative; `floor_strike`/`cap_strike` are
    the numeric bounds. Returns None if the market can't be interpreted, so the
    model declines to have an opinion rather than guessing.
    """
    ticker = raw.get("ticker")
    if not ticker:
        return None
    floor = raw.get("floor_strike")
    cap = raw.get("cap_strike")
    strike_type = (raw.get("strike_type") or "").lower()

    kind: Optional[str] = None
    lower = upper = None
    if strike_type == "greater" or (not strike_type and floor is not None and cap is None):
        if floor is None:
            return None
        kind, lower = "above", float(floor)
    elif strike_type == "less" or (not strike_type and cap is not None and floor is None):
        if cap is None:
            return None
        kind, upper = "below", float(cap)
    elif strike_type == "between" or (not strike_type and floor is not None and cap is not None):
        if floor is None or cap is None:
            return None
        kind, lower, upper = "between", float(floor), float(cap)
    else:
        return None  # unrecognised strike type -> no opinion

    target_date = (raw.get("target_date") or raw.get("expiration_date")
                   or target_date_from_ticker(ticker))
    if target_date is None:
        return None  # without a date the model has nothing to forecast against

    return Market(
        ticker=ticker, series=city.kalshi_series, city=city.name,
        threshold_kind=kind, lower_f=lower, upper_f=upper,
        target_date=target_date, nws_station=city.nws_station,
        lat=city.lat, lon=city.lon,
    )


class WeatherNormalModel(PredictionModel):
    """Ensemble model.

    Combines every forecast provider (NWS + Open-Meteo HRRR/GFS/ECMWF/best) into
    a single temperature distribution: the mean of their predicted highs, with a
    spread that GROWS when the models disagree. Then it clamps the result with
    what has actually been observed so far today (METAR): a daily high can only
    be at or above what has already happened.

    effective_sigma = sqrt(base_sigma^2 + ensemble_spread^2)
      base_sigma      = each model's own forecast error (config forecast_sigma_f)
      ensemble_spread = std-dev of the providers' predicted highs (model disagreement)
    """
    name = "weather_ensemble"

    def __init__(self, sigma_f: float = 3.0) -> None:
        self.sigma_f = float(sigma_f)

    def predict(self, market: Market, context: MarketContext) -> Optional[Prediction]:
        if not (market.nws_station and market.target_date and market.threshold_kind):
            return None

        members = context.view.latest_forecasts_by_provider(
            market.nws_station, market.target_date)
        highs = {p: fc.forecast_high_f for p, fc in members.items()
                 if fc.forecast_high_f is not None}
        if not highs:
            return None  # no provider has an opinion as-of now

        values = list(highs.values())
        mean = statistics.fmean(values)
        spread = statistics.stdev(values) if len(values) >= 2 else 0.0
        sigma = math.sqrt(self.sigma_f ** 2 + spread ** 2)

        # Settlement temperature is a whole number of degrees, and Kalshi's
        # thresholds are EXCLUSIVE of the strike (verified against live data):
        #   strike_type=greater, floor=96  -> "97 or above"  -> YES iff X >= 97
        #   strike_type=less,    cap=89    -> "88 or below"  -> YES iff X <= 88
        #   strike_type=between, 95..96    -> "95 to 96"     -> YES iff 95<=X<=96
        # So integrate a continuous distribution at half-degree boundaries.
        # Getting this wrong shifts every probability by a full degree of
        # forecast error, which is enormous relative to the edge we're hunting.
        k = market.threshold_kind
        if k == "above":
            prob = 1.0 - _normal_cdf(market.lower_f + 0.5, mean, sigma)
        elif k == "below":
            prob = _normal_cdf(market.upper_f - 0.5, mean, sigma)
        elif k == "between":
            prob = (_normal_cdf(market.upper_f + 0.5, mean, sigma)
                    - _normal_cdf(market.lower_f - 0.5, mean, sigma))
        else:
            return None
        prob = min(1.0, max(0.0, prob))

        # Observation clamp: the day's high can't be below what we've already seen.
        day_start, day_end = _local_day_bounds_ms(market.target_date)
        observed_c = context.view.observed_max_c_today(market.nws_station, day_start, day_end)
        observed_f = _c_to_f(observed_c) if observed_c is not None else None
        # Same exclusive-threshold semantics as above: "above 96" needs 97.
        if observed_f is not None:
            if k == "above" and observed_f >= market.lower_f + 1:
                prob = 1.0                       # threshold already reached today
            elif k == "below" and observed_f > market.upper_f - 1:
                prob = 0.0                       # already too hot for "below"
            elif k == "between" and observed_f > market.upper_f:
                prob = 0.0                       # already above the bracket's top

        inputs = {
            "ensemble_members": highs,           # {provider: predicted_high_f}
            "ensemble_mean_f": round(mean, 2),
            "ensemble_spread_f": round(spread, 2),
            "base_sigma_f": self.sigma_f,
            "effective_sigma_f": round(sigma, 2),
            "observed_max_f_so_far": round(observed_f, 2) if observed_f is not None else None,
            "threshold_kind": k,
            "lower_f": market.lower_f,
            "upper_f": market.upper_f,
            "as_of_ms": context.decision_ts,
            "station": market.nws_station,
        }
        return Prediction(probability=prob, uncertainty=sigma, inputs=inputs)
