"""Data collectors: persist Kalshi market data and NWS forecasts continuously.

All writes are append-only observations (except market metadata + settlement
status, which are reference data). Collection continues even when trading is
halted -- data is the primary product of Phase 1.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Optional

from ..clients.forecast_providers import ForecastProvider, ObservationProvider
from ..clients.kalshi_client import KalshiClient
from ..config import CityConfig
from ..logging_setup import log
from ..storage.dao import Dao
from ..util import iso_to_ms, now_ms

FORECAST_HORIZON_DAYS = 7


class MarketCollector:
    def __init__(self, dao: Dao, client: KalshiClient, logger: logging.Logger) -> None:
        self._dao = dao
        self._client = client
        self._log = logger

    def poll_books(self, series_list: list[str]) -> int:
        """Snapshot the full order book for every open market in the families."""
        snapshots = 0
        for series in series_list:
            markets = self._client.list_markets(series_ticker=series, status="open")
            for m in markets:
                ticker = m.get("ticker")
                if not ticker:
                    continue
                close_ts = iso_to_ms(m["close_time"]) if m.get("close_time") else None
                self._dao.upsert_market(
                    ticker=ticker, series=series, city=None, title=m.get("title"),
                    close_ts=close_ts, status=m.get("status", "open"),
                )
                try:
                    ob = self._client.get_orderbook(ticker)
                    self._dao.insert_book_snapshot(ticker, ob.captured_ts,
                                                   ob.yes_levels, ob.no_levels)
                    snapshots += 1
                except Exception as e:  # one bad market must not stop the sweep
                    log(self._log, logging.WARNING, "orderbook fetch failed",
                        ticker=ticker, error=str(e))
        return snapshots

    def poll_trades(self, series_list: list[str]) -> int:
        """Record observed trades for open markets in the families."""
        recorded = 0
        for series in series_list:
            for m in self._client.list_markets(series_ticker=series, status="open"):
                ticker = m.get("ticker")
                if not ticker:
                    continue
                try:
                    for t in self._client.get_trades(ticker):
                        created = t.get("created_time")
                        trade_ts = iso_to_ms(created) if created else now_ms()
                        self._dao.insert_trade(
                            ticker=ticker, trade_ts=trade_ts,
                            price_cents=int(t.get("yes_price", 0)),
                            count=int(t.get("count", 0)),
                            taker_side=t.get("taker_side"),
                            trade_id=t.get("trade_id"),
                        )
                        recorded += 1
                except Exception as e:
                    log(self._log, logging.WARNING, "trade fetch failed",
                        ticker=ticker, error=str(e))
        return recorded

    def poll_settlements(self) -> int:
        """Record settlement results for markets that have resolved."""
        rows = self._dao.conn.execute(
            "SELECT ticker FROM markets WHERE ticker NOT IN (SELECT ticker FROM settlements)"
        ).fetchall()
        recorded = 0
        for row in rows:
            ticker = row["ticker"]
            try:
                m = self._client.get_market(ticker)
            except Exception:
                continue
            result = (m.get("result") or "").lower()
            status = m.get("status", "")
            if result in ("yes", "no"):
                settled_ts = iso_to_ms(m["close_time"]) if m.get("close_time") else None
                self._dao.upsert_settlement(ticker, result, settled_ts)
                self._dao.upsert_market(
                    ticker=ticker, series=m.get("series_ticker", ""), city=None,
                    title=m.get("title"), close_ts=settled_ts, status=status or "settled",
                    result=result,
                )
                recorded += 1
        return recorded


class WeatherCollector:
    def __init__(self, dao: Dao, forecast_providers: list[ForecastProvider],
                 observation_providers: list[ObservationProvider],
                 logger: logging.Logger) -> None:
        self._dao = dao
        self._forecast_providers = forecast_providers
        self._observation_providers = observation_providers
        self._log = logger

    def poll_forecasts(self, cities: list[CityConfig]) -> int:
        """Capture every provider's forecast highs across the horizon, each
        stored as its own ensemble member stamped with its source."""
        stored = 0
        for city in cities:
            for provider in self._forecast_providers:
                try:
                    forecasts = provider.forecast_highs(city.lat, city.lon,
                                                        FORECAST_HORIZON_DAYS)
                except Exception as e:
                    log(self._log, logging.WARNING, "forecast provider failed",
                        provider=provider.name, city=city.name, error=str(e))
                    continue
                for fc in forecasts:
                    if fc.high_f is None:
                        continue
                    self._dao.insert_forecast(
                        station=city.nws_station, city=city.name, issued_ts=fc.issued_ts,
                        target_date=fc.target_date, forecast_high_f=fc.high_f,
                        raw=fc.raw, provider=fc.provider,
                    )
                    stored += 1
        return stored

    def poll_observations(self, cities: list[CityConfig]) -> int:
        """Capture station observations (e.g. METAR) -- what has actually
        happened so far today, used to clamp the model near settlement."""
        stored = 0
        for city in cities:
            for provider in self._observation_providers:
                try:
                    obs = provider.latest_observations(city.nws_station, city.lat, city.lon)
                except Exception as e:
                    log(self._log, logging.WARNING, "observation provider failed",
                        provider=provider.name, city=city.name, error=str(e))
                    continue
                for o in obs:
                    self._dao.insert_observation(
                        station=city.nws_station, obs_ts=o.obs_ts,
                        temp_c=o.temp_c, raw=o.raw,
                    )
                    stored += 1
        return stored
