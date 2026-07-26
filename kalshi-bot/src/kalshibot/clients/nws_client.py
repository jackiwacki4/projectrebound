"""National Weather Service API client (api.weather.gov).

Free, no API key, but a descriptive User-Agent is required. Flow:
  1. /points/{lat},{lon}      -> office + grid coords + forecast URLs
  2. .../forecast             -> 12h periods; the daytime period's temperature
                                 is the forecast daily HIGH (deg F)
  3. /stations/{id}/observations?start&end -> station obs (temp in deg C)

Forecasts are stamped with NWS's own issue time (properties.updateTime) so the
storage layer can later answer "what did we know at decision time" without
lookahead. All temperatures normalised to Fahrenheit for the model.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from ..util import iso_to_ms
from .ssl_support import ssl_context

_USER_AGENT_DEFAULT = "(projectrebound-phase1, kalshibot@example.com)"


class NwsApiError(RuntimeError):
    pass


@dataclass
class ForecastHigh:
    target_date: str          # YYYY-MM-DD (local)
    high_f: Optional[float]
    issued_ts: int            # NWS updateTime, ms
    raw: dict[str, Any]


def _c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9.0 / 5.0 + 32.0


class NwsClient:
    def __init__(self, user_agent: str = _USER_AGENT_DEFAULT) -> None:
        self._ua = user_agent
        self._point_cache: dict[tuple[float, float], dict] = {}

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={
            "User-Agent": self._ua,
            "Accept": "application/geo+json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20, context=ssl_context()) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise NwsApiError(f"NWS {url} -> {e.code}: {e.read()[:200]!r}") from None
        except urllib.error.URLError as e:
            raise NwsApiError(f"NWS {url} -> {e.reason}") from None

    def _point(self, lat: float, lon: float) -> dict:
        key = (round(lat, 4), round(lon, 4))
        if key not in self._point_cache:
            self._point_cache[key] = self._get(
                f"https://api.weather.gov/points/{key[0]},{key[1]}"
            )["properties"]
        return self._point_cache[key]

    def forecast_high(self, lat: float, lon: float, target_date: str) -> Optional[ForecastHigh]:
        """Forecast daily high (deg F) for `target_date` (YYYY-MM-DD local)."""
        forecast_url = self._point(lat, lon)["forecast"]
        props = self._get(forecast_url)["properties"]
        issued_ts = iso_to_ms(props["updateTime"])
        for period in props.get("periods", []):
            start = period.get("startTime", "")
            # startTime is local ISO with offset, e.g. 2026-07-24T06:00:00-05:00
            if period.get("isDaytime") and start[:10] == target_date:
                temp = period.get("temperature")
                unit = (period.get("temperatureUnit") or "F").upper()
                high_f = float(temp) if unit == "F" else _c_to_f(float(temp))
                return ForecastHigh(target_date=target_date, high_f=high_f,
                                    issued_ts=issued_ts, raw=period)
        # No daytime period for that date (e.g. it's already evening).
        return ForecastHigh(target_date=target_date, high_f=None,
                            issued_ts=issued_ts, raw={})

    def observations(self, station: str, start_iso: str, end_iso: str) -> list[dict]:
        """Raw observations for a station over [start, end] (ISO-8601)."""
        url = (f"https://api.weather.gov/stations/{station}/observations"
               f"?start={start_iso}&end={end_iso}")
        return self._get(url).get("features", [])

    def observed_high_f(self, station: str, day: date) -> Optional[float]:
        """Highest observed temperature (deg F) for a local calendar day.

        Analysis helper only. Ground-truth market settlement comes from Kalshi's
        resolved market result, not from this -- Kalshi settles on the official
        NWS Climatological Report, which this does not attempt to reproduce.
        """
        start = f"{day.isoformat()}T00:00:00+00:00"
        end = f"{day.isoformat()}T23:59:59+00:00"
        temps_c = []
        for feat in self.observations(station, start, end):
            val = (feat.get("properties", {}).get("temperature", {}) or {}).get("value")
            if val is not None:
                temps_c.append(float(val))
        if not temps_c:
            return None
        return _c_to_f(max(temps_c))
