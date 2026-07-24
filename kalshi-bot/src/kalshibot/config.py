"""Configuration loading.

Credentials come from the environment / a gitignored .env file and are NEVER
read from the YAML config (which may be committed as an example). Operational
limits come from the YAML file. The two are kept strictly separate so a config
file can never leak a key and a key file can never silently change a limit.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no dependency on python-dotenv).

    Only sets variables that are not already present in the real environment,
    so an explicitly-exported var always wins.
    """
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Credentials:
    api_key_id: str
    private_key_path: str
    environment: str  # "demo" | "prod"

    @property
    def rest_base_url(self) -> str:
        return (
            "https://api.elections.kalshi.com/trade-api/v2"
            if self.environment == "prod"
            else "https://demo-api.kalshi.co/trade-api/v2"
        )

    @property
    def ws_base_url(self) -> str:
        return (
            "wss://api.elections.kalshi.com/trade-api/ws/v2"
            if self.environment == "prod"
            else "wss://demo-api.kalshi.co/trade-api/ws/v2"
        )


@dataclass(frozen=True)
class CityConfig:
    name: str
    kalshi_series: str
    nws_station: str
    lat: float
    lon: float


@dataclass(frozen=True)
class Config:
    market_family: str
    raw: dict[str, Any] = field(repr=False)

    # --- typed accessors for the sections the code actually uses ---
    @property
    def db_path(self) -> str:
        return self.raw["storage"]["db_path"]

    @property
    def logging(self) -> dict[str, Any]:
        return self.raw.get("logging", {})

    @property
    def cities(self) -> list[CityConfig]:
        return [CityConfig(**c) for c in self.raw.get("weather", {}).get("cities", [])]

    @property
    def weather(self) -> dict[str, Any]:
        return self.raw.get("weather", {})

    @property
    def collection(self) -> dict[str, Any]:
        return self.raw.get("collection", {})

    @property
    def risk(self) -> dict[str, Any]:
        return self.raw.get("risk", {})

    @property
    def model(self) -> dict[str, Any]:
        return self.raw.get("model", {})

    @property
    def live_trading(self) -> dict[str, Any]:
        return self.raw.get("live_trading", {})

    @property
    def live_trading_enabled(self) -> bool:
        return bool(self.live_trading.get("enabled", False))


def load_credentials() -> Credentials:
    _load_dotenv()
    api_key_id = os.environ.get("KALSHI_API_KEY_ID", "")
    private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
    environment = os.environ.get("KALSHI_ENVIRONMENT", "demo").lower()
    if environment not in ("demo", "prod"):
        raise ValueError(f'KALSHI_ENVIRONMENT must be "demo" or "prod", got "{environment}"')
    return Credentials(
        api_key_id=api_key_id,
        private_key_path=private_key_path,
        environment=environment,
    )


def load_config(path: str = "config/config.yaml") -> Config:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Copy config/config.example.yaml to {path}."
        )
    raw = yaml.safe_load(p.read_text())
    family = raw.get("market_family", "weather")
    if family != "weather":
        raise ValueError(
            f'Phase 1 only implements market_family "weather", got "{family}".'
        )
    return Config(market_family=family, raw=raw)
