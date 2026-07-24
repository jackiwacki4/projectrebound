"""Small shared utilities."""
from __future__ import annotations

import time
from datetime import datetime, timezone


def now_ms() -> int:
    """Current UTC time in Unix milliseconds."""
    return int(time.time() * 1000)


def iso_to_ms(iso: str) -> int:
    """Parse an ISO-8601 timestamp (with or without trailing Z) to Unix ms."""
    s = iso.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
