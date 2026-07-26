"""Weather data providers behind one interface, so the model can ensemble them.

Forecast providers (predict the daily high):
  - nws                 : NWS gridpoint forecast (api.weather.gov)
  - open_meteo_best     : Open-Meteo's auto blend
  - open_meteo_hrrr     : NOAA HRRR, high-res short-range (via Open-Meteo, decoded)
  - open_meteo_gfs      : NOAA GFS, global (via Open-Meteo, decoded)
  - open_meteo_ecmwf    : ECMWF IFS open data (via Open-Meteo, decoded)

Observation providers (what has actually happened so far today):
  - metar               : airport METAR observations (aviationweather.gov)

Why HRRR/GFS/ECMWF come through Open-Meteo rather than raw NOMADS/ECMWF GRIB:
Open-Meteo already ingests those exact model runs and serves them as clean,
low-latency JSON. That gets the accuracy of all three models without GRIB
decoding, multi-hundred-MB downloads, or system libraries -- the right trade
for "accurate and quick" on a laptop. A raw-GRIB provider can be added behind
this same interface later if truly needed.
"""
from __future__ import annotations

import urllib.parse
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Optional

from ..util import iso_to_ms, now_ms
from .http_json import USER_AGENT as _UA
from .http_json import get_json as _get_json
from .nws_client import NwsClient


@dataclass(frozen=True)
class ProviderForecast:
    provider: str
    target_date: str          # YYYY-MM-DD
    high_f: Optional[float]
    issued_ts: int            # source issue time if known, else fetch time (ms)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderObservation:
    obs_ts: int               # ms
    temp_c: Optional[float]
    raw: dict[str, Any] = field(default_factory=dict)


def _c_to_f(c: Optional[float]) -> Optional[float]:
    return None if c is None else c * 9.0 / 5.0 + 32.0


# --------------------------------------------------------------------------
# Forecast providers
# --------------------------------------------------------------------------
class ForecastProvider(ABC):
    name: str

    @abstractmethod
    def forecast_highs(self, lat: float, lon: float, days: int) -> list[ProviderForecast]:
        """Daily-high forecasts (deg F) for the next `days` local days."""
        raise NotImplementedError


class NwsForecastProvider(ForecastProvider):
    name = "nws"

    def __init__(self, client: Optional[NwsClient] = None) -> None:
        self._nws = client or NwsClient(_UA)

    def forecast_highs(self, lat: float, lon: float, days: int) -> list[ProviderForecast]:
        # Errors deliberately propagate: the collector catches and LOGS them per
        # provider. Swallowing them here made real outages (expired certs, API
        # downtime) look like "no forecast available", which is much worse.
        horizon = {(date.today() + timedelta(days=d)).isoformat() for d in range(days)}
        return [ProviderForecast(self.name, fh.target_date, fh.high_f, fh.issued_ts, fh.raw)
                for fh in self._nws.all_forecast_highs(lat, lon)   # one request, all days
                if fh.high_f is not None and fh.target_date in horizon]


class OpenMeteoForecastProvider(ForecastProvider):
    """Open-Meteo daily max temperature. `model` selects a specific NWP model;
    None uses Open-Meteo's auto blend."""

    _BASE = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, name: str, model: Optional[str] = None) -> None:
        self.name = name
        self._model = model

    def forecast_highs(self, lat: float, lon: float, days: int) -> list[ProviderForecast]:
        params = {
            "latitude": f"{lat}", "longitude": f"{lon}",
            "daily": "temperature_2m_max", "temperature_unit": "fahrenheit",
            "timezone": "auto", "forecast_days": str(max(1, min(16, days))),
        }
        if self._model:
            params["models"] = self._model
        url = self._BASE + "?" + urllib.parse.urlencode(params)
        data = _get_json(url)
        daily = data.get("daily", {})
        dates = daily.get("time", []) or []
        highs = daily.get("temperature_2m_max", []) or []
        fetched = now_ms()
        out: list[ProviderForecast] = []
        for target, high in zip(dates, highs):
            if high is None:
                continue
            # Open-Meteo's daily response does not expose the model run time;
            # fetch time is the honest "known at" stamp for no-lookahead.
            out.append(ProviderForecast(self.name, target, float(high), fetched,
                                        {"source": "open-meteo", "model": self._model}))
        return out


# --------------------------------------------------------------------------
# Observation providers
# --------------------------------------------------------------------------
class ObservationProvider(ABC):
    name: str

    @abstractmethod
    def latest_observations(self, station: str, lat: float, lon: float
                            ) -> list[ProviderObservation]:
        raise NotImplementedError


class MetarObservationProvider(ObservationProvider):
    name = "metar"
    _BASE = "https://aviationweather.gov/api/data/metar"

    def latest_observations(self, station: str, lat: float, lon: float
                            ) -> list[ProviderObservation]:
        url = self._BASE + "?" + urllib.parse.urlencode(
            {"ids": station, "format": "json", "hours": "3"})
        # Errors propagate so the collector logs them (see NwsForecastProvider).
        data = _get_json(url)
        rows = data if isinstance(data, list) else data.get("data", [])
        out: list[ProviderObservation] = []
        for r in rows or []:
            temp_c = r.get("temp")
            obs = r.get("obsTime") or r.get("reportTime")
            if obs is None:
                continue
            # obsTime is Unix seconds; reportTime may be an ISO string.
            obs_ms = int(obs) * 1000 if isinstance(obs, (int, float)) else iso_to_ms(str(obs))
            out.append(ProviderObservation(obs_ms,
                                           float(temp_c) if temp_c is not None else None, r))
        return out


# --------------------------------------------------------------------------
# Build providers from config
# --------------------------------------------------------------------------
# Open-Meteo model identifiers. VERIFIED EMPIRICALLY against the live API
# (2026-07) by querying each candidate and confirming it returns real daily
# highs -- documentation summaries listed names the API rejects with HTTP 400.
# If a member ever starts failing, hit the API directly with ?models=<name> and
# read the JSON "reason" before changing anything here.
#
# Deliberately chosen so members are genuinely INDEPENDENT (same location, same
# day, at verification time: HRRR 77.6F, GFS 82.1F, ECMWF 79.8F). `gfs_seamless`
# is excluded on purpose: it blends HRRR into the near term and so duplicates
# the HRRR member, which would fake ensemble agreement.
_OPEN_METEO_MODELS = {
    "open_meteo_best": None,               # Open-Meteo's own auto blend
    "open_meteo_hrrr": "gfs_hrrr",         # NOAA HRRR, high-res short-range
    "open_meteo_gfs": "gfs_global",        # NOAA GFS, global
    "open_meteo_ecmwf": "ecmwf_ifs025",    # ECMWF IFS 0.25deg open data
    "open_meteo_ecmwf_ai": "ecmwf_aifs025_single",  # ECMWF AIFS (AI model), optional
}


def build_forecast_providers(names: list[str]) -> list[ForecastProvider]:
    providers: list[ForecastProvider] = []
    for name in names:
        if name == "nws":
            providers.append(NwsForecastProvider())
        elif name in _OPEN_METEO_MODELS:
            providers.append(OpenMeteoForecastProvider(name, _OPEN_METEO_MODELS[name]))
        # Unknown names are ignored deliberately; config is the source of truth.
    return providers


def build_observation_providers(names: list[str]) -> list[ObservationProvider]:
    providers: list[ObservationProvider] = []
    for name in names:
        if name == "metar":
            providers.append(MetarObservationProvider())
    return providers
