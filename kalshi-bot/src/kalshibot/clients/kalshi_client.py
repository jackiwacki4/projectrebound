"""Kalshi REST client.

Auth (verified against Kalshi docs, 2026): every request carries three headers
  KALSHI-ACCESS-KEY        the API key id
  KALSHI-ACCESS-TIMESTAMP  current time in Unix MILLISECONDS
  KALSHI-ACCESS-SIGNATURE  base64 RSA-PSS(SHA-256) over
                           f"{timestamp}{METHOD}{path}", path WITHOUT query string
Signature uses RSA-PSS, MGF1-SHA256, salt length = digest length (32 bytes).

Every request path is run through the funding denylist guard (spec 1.1) and the
token-bucket limiter before it is sent. On HTTP 429 we back off; we never retry
in a tight loop.

READ methods (markets, order book, balance, positions) are safe to use freely.
The order-placement methods are primitives; the 1-contract Phase 1 ceiling is
enforced by the live engine, not here, and never runs unless live trading is
explicitly enabled.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

# NOTE: `cryptography` is imported lazily inside the signing methods, not at
# module load. That keeps the funding denylist guard (and everything else in
# this module) importable and testable without the crypto backend present --
# the guard must never depend on optional libraries.
from ..config import Credentials
from ..util import now_ms
from .http_guard import assert_path_allowed
from .rate_limiter import TokenBucket


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, body: str, path: str) -> None:
        super().__init__(f"Kalshi {path} -> {status}: {body[:300]}")
        self.status = status
        self.body = body


@dataclass
class OrderBook:
    ticker: str
    captured_ts: int
    yes_levels: list[list[int]]   # [[price_cents, count], ...] best-first
    no_levels: list[list[int]]


class KalshiClient:
    def __init__(self, creds: Credentials, rate_per_sec: float = 8.0,
                 capacity: float = 8.0) -> None:
        self._creds = creds
        self._base = creds.rest_base_url
        self._path_prefix = "/trade-api/v2"
        self._bucket = TokenBucket(rate_per_sec, capacity)
        self._private_key = self._load_key(creds.private_key_path)

    @staticmethod
    def _load_key(path: str):
        if not path:
            raise ValueError("KALSHI_PRIVATE_KEY_PATH is not set.")
        from cryptography.hazmat.primitives import serialization
        with open(path, "rb") as fh:
            return serialization.load_pem_private_key(fh.read(), password=None)

    # ---- signing ----
    def _sign(self, timestamp_ms: str, method: str, path_no_query: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        message = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("ascii")

    def _headers(self, method: str, full_path: str) -> dict[str, str]:
        ts = str(now_ms())
        path_no_query = full_path.split("?", 1)[0]
        return {
            "KALSHI-ACCESS-KEY": self._creds.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path_no_query),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ---- low-level request ----
    def _request(self, method: str, endpoint: str, body: Optional[dict] = None,
                 _retries: int = 3) -> dict[str, Any]:
        # endpoint is like "/markets?limit=100"; full signed path includes prefix.
        full_path = self._path_prefix + endpoint
        assert_path_allowed(full_path)   # HARD funding/transfer guard
        self._bucket.acquire()

        url = self._base + endpoint
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method,
                                     headers=self._headers(method, full_path))
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            payload = e.read().decode("utf-8", "replace")
            if e.code == 429 and _retries > 0:
                # Respect the limit: back off, then retry a bounded number of times.
                time.sleep(2.0 * (4 - _retries))
                return self._request(method, endpoint, body, _retries - 1)
            raise KalshiApiError(e.code, payload, endpoint) from None
        except urllib.error.URLError as e:
            raise KalshiApiError(0, str(e.reason), endpoint) from None

    # ---- read: markets ----
    def list_markets(self, series_ticker: Optional[str] = None,
                     status: str = "open", limit: int = 200) -> list[dict]:
        markets: list[dict] = []
        cursor: Optional[str] = None
        while True:
            q = f"/markets?limit={limit}&status={status}"
            if series_ticker:
                q += f"&series_ticker={series_ticker}"
            if cursor:
                q += f"&cursor={cursor}"
            page = self._request("GET", q)
            markets.extend(page.get("markets", []))
            cursor = page.get("cursor") or None
            if not cursor:
                break
        return markets

    def get_market(self, ticker: str) -> dict:
        return self._request("GET", f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str) -> OrderBook:
        raw = self._request("GET", f"/markets/{ticker}/orderbook").get("orderbook", {})
        yes = [[int(p), int(c)] for p, c in (raw.get("yes") or [])]
        no = [[int(p), int(c)] for p, c in (raw.get("no") or [])]
        return OrderBook(ticker=ticker, captured_ts=now_ms(), yes_levels=yes, no_levels=no)

    def get_trades(self, ticker: str, limit: int = 100) -> list[dict]:
        return self._request("GET", f"/markets/trades?ticker={ticker}&limit={limit}"
                             ).get("trades", [])

    # ---- read: account (read-only per spec 1.1) ----
    def get_balance(self) -> dict:
        # Returns e.g. {"balance": <cents>, "portfolio_value": <cents>}.
        return self._request("GET", "/portfolio/balance")

    def get_positions(self) -> list[dict]:
        return self._request("GET", "/portfolio/positions").get("market_positions", [])

    # ---- order primitives (used only by the live engine, only when enabled) ----
    def create_order(self, body: dict) -> dict:
        # UNVERIFIED order-body schema -- see live_engine + README before enabling.
        return self._request("POST", "/portfolio/orders", body).get("order", {})

    def cancel_order(self, order_id: str) -> dict:
        return self._request("DELETE", f"/portfolio/orders/{order_id}")
