"""Market-family seam.

The runtime loop does the same four things whatever the markets are about:
snapshot order books, collect the model's inputs, run the model as-of now, and
route candidates through the risk gates. Only three things differ per family --
which Kalshi series to watch, how to turn a raw market into a typed one, and
which collectors feed the model -- so those are what a family provides here.

Adding a third family (crypto, elections) means adding a class in this file plus
a model and its collectors. It does not mean touching the loop, the gates, the
paper/live engines, or the reporting.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..clients.espn_client import EspnClient
from ..clients.forecast_providers import build_forecast_providers, build_observation_providers
from ..clients.kalshi_client import KalshiClient
from ..clients.sports_providers import build_rating_providers, build_state_providers
from ..config import Config
from ..models.base import MarketBase, PredictionModel
from ..models.sports import SportsEnsembleModel, parse_game_market
from ..models.weather import WeatherNormalModel, parse_temperature_market
from ..storage.dao import AsOfView, Dao
from .collectors import SportsCollector, WeatherCollector


@dataclass(frozen=True)
class PollTask:
    """One of a family's data collectors, with the cadence it should run at."""
    name: str
    fn: Callable[[], Any]
    period_seconds: int


class MarketFamily(ABC):
    name: str

    @property
    @abstractmethod
    def series_list(self) -> list[str]:
        """Kalshi series tickers to watch."""

    @property
    @abstractmethod
    def model(self) -> PredictionModel:
        ...

    @abstractmethod
    def parse(self, raw: dict, series: str) -> Optional[MarketBase]:
        """Typed market, or None to decline having an opinion about it."""

    @abstractmethod
    def data_tasks(self) -> list[PollTask]:
        """The family's own collectors. Order-book, trade, and settlement
        collection is shared and lives in the runtime loop."""

    @abstractmethod
    def input_fetched_ts(self, view: AsOfView, market: MarketBase) -> Optional[int]:
        """When the model's primary input was last known, for the staleness gate.
        None means "no input", which the gate treats as too old."""

    def describe(self) -> str:
        return self.name


class WeatherFamily(MarketFamily):
    name = "weather"

    def __init__(self, cfg: Config, dao: Dao, client: KalshiClient,
                 logger: logging.Logger) -> None:
        self._cfg = cfg
        self._cities = cfg.cities
        self._by_series = {c.kalshi_series: c for c in self._cities}
        default_fps = ["nws", "open_meteo_best", "open_meteo_hrrr",
                       "open_meteo_gfs", "open_meteo_ecmwf"]
        self._fps = build_forecast_providers(cfg.weather.get("forecast_providers", default_fps))
        self._ops = build_observation_providers(
            cfg.weather.get("observation_providers", ["metar"]))
        self._collector = WeatherCollector(dao, self._fps, self._ops, logger)
        self._model = WeatherNormalModel(
            sigma_f=float(cfg.model.get("forecast_sigma_f", 3.0)))

    @property
    def series_list(self) -> list[str]:
        return [c.kalshi_series for c in self._cities]

    @property
    def model(self) -> PredictionModel:
        return self._model

    def parse(self, raw: dict, series: str) -> Optional[MarketBase]:
        city = self._by_series.get(series)
        return parse_temperature_market(raw, city) if city else None

    def data_tasks(self) -> list[PollTask]:
        w = self._cfg.weather
        return [
            PollTask("forecasts", lambda: self._collector.poll_forecasts(self._cities),
                     int(w.get("forecast_poll_seconds", 3600))),
            PollTask("observations", lambda: self._collector.poll_observations(self._cities),
                     int(w.get("observation_poll_seconds", 900))),
        ]

    def input_fetched_ts(self, view: AsOfView, market: MarketBase) -> Optional[int]:
        station = getattr(market, "nws_station", None)
        target_date = getattr(market, "target_date", None)
        if not (station and target_date):
            return None
        fc = view.latest_forecast(station, target_date)
        return fc.fetched_ts if fc else None

    def describe(self) -> str:
        return (f"weather: {len(self._cities)} cities, "
                f"forecast providers {[p.name for p in self._fps]}, "
                f"observation providers {[p.name for p in self._ops]}")


class SportsFamily(MarketFamily):
    name = "sports"

    def __init__(self, cfg: Config, dao: Dao, client: KalshiClient,
                 logger: logging.Logger) -> None:
        self._cfg = cfg
        self._leagues = cfg.leagues
        self._by_series = {lg.kalshi_series: lg for lg in self._leagues}
        s = cfg.sports
        espn = EspnClient()
        self._rating_providers = build_rating_providers(
            s.get("rating_providers", ["elo", "log5", "pythagorean"]),
            pythagorean_exponent=float(s.get("pythagorean_exponent", 1.83)),
            client=espn,
        )
        self._state_providers = build_state_providers(
            s.get("state_providers", ["espn_scoreboard"]), client=espn)
        self._collector = SportsCollector(
            dao, client, self._rating_providers, self._state_providers, espn, logger,
            schedule_horizon_days=int(s.get("schedule_horizon_days", 4)),
            results_lookback_days=int(s.get("results_lookback_days", 45)),
        )
        self._max_state_age_seconds = int(s.get("max_state_age_seconds", 900))
        self._model = SportsEnsembleModel(
            self._leagues,
            min_sigma_floor=float(cfg.model.get("min_sigma_floor", 0.0)),
            max_state_age_seconds=self._max_state_age_seconds,
        )

    @property
    def series_list(self) -> list[str]:
        return [lg.kalshi_series for lg in self._leagues]

    @property
    def model(self) -> PredictionModel:
        return self._model

    def parse(self, raw: dict, series: str) -> Optional[MarketBase]:
        league = self._by_series.get(series)
        return parse_game_market(raw, league) if league else None

    def data_tasks(self) -> list[PollTask]:
        s = self._cfg.sports
        return [
            PollTask("schedule", lambda: self._collector.poll_schedule(self._leagues),
                     int(s.get("schedule_poll_seconds", 1800))),
            PollTask("results", lambda: self._collector.poll_results(self._leagues),
                     int(s.get("results_poll_seconds", 21600))),
            PollTask("ratings", lambda: self._collector.poll_ratings(self._leagues),
                     int(s.get("rating_poll_seconds", 1800))),
            PollTask("game_states", lambda: self._collector.poll_states(self._leagues),
                     int(s.get("state_poll_seconds", 60))),
        ]

    def input_fetched_ts(self, view: AsOfView, market: MarketBase) -> Optional[int]:
        """Freshest rating, but the live score once the game is under way.

        The model already declines to predict on an underway game with a stale
        score feed; returning the score's age here makes the risk gate a second,
        independent barrier rather than trusting one check.
        """
        game_key = getattr(market, "game_key", None)
        if not game_key:
            return None
        members = view.latest_sports_ratings_by_provider(game_key)
        if not members:
            return None
        rating_ts = max(r.fetched_ts for r in members.values())
        game = view.sports_game(game_key)
        start_ts = game.start_ts if game else getattr(market, "start_ts", None)
        if start_ts is None or view.as_of_ms < start_ts:
            return rating_ts
        state = view.latest_game_state(game_key)
        if state is None:
            return None
        return min(rating_ts, state.captured_ts)

    def describe(self) -> str:
        return (f"sports: leagues {[lg.name for lg in self._leagues]}, "
                f"rating providers {[p.name for p in self._rating_providers]}, "
                f"state providers {[p.name for p in self._state_providers]}")


def build_family(cfg: Config, dao: Dao, client: KalshiClient,
                 logger: logging.Logger) -> MarketFamily:
    if cfg.market_family == "weather":
        return WeatherFamily(cfg, dao, client, logger)
    if cfg.market_family == "sports":
        return SportsFamily(cfg, dao, client, logger)
    # config.load_config validates the family, so reaching here means a family
    # was added to SUPPORTED_FAMILIES without being wired up.
    raise ValueError(f"no family implementation for market_family={cfg.market_family!r}")
