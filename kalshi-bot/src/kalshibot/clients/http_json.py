"""Shared JSON-over-HTTPS fetch for the free public data APIs.

Used by every non-Kalshi data source (weather forecasts, sports feeds). The one
behaviour worth naming: on an HTTP error we re-raise WITH the response body
attached, because these APIs put the actual reason in a JSON field and a bare
"HTTP Error 400: Bad Request" hides it -- which cost real debugging time on the
Open-Meteo model identifiers.

Kalshi requests do NOT come through here: they are signed and must pass the
funding denylist guard, so they live in `kalshi_client.py`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

# Free public APIs ask for a descriptive User-Agent (api.weather.gov requires one).
USER_AGENT = "(projectrebound-phase1, kalshibot@example.com)"


def get_json(url: str, timeout: int = 20, accept: str = "application/json") -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise urllib.error.HTTPError(
            e.url, e.code, f"{e.reason} -- {body}" if body else str(e.reason),
            e.headers, None) from None


def _ssl():
    from .ssl_support import ssl_context
    return ssl_context()
