"""Daily-loss circuit breaker.

If realized live losses for the current local day exceed the configured
threshold, live trading halts. The tripped state is persisted as a marker file
so it SURVIVES A RESTART and can only be cleared by explicit human action
(`kalshibot reset-breaker`). It never resets automatically, and never at
midnight rollover -- a human decides the day is over.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from ..storage.dao import Dao


def _local_day_bounds_ms(now: Optional[datetime] = None) -> tuple[int, int]:
    now = now or datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


class CircuitBreaker:
    def __init__(self, marker_path: str, dao: Dao, daily_loss_limit_cents: int) -> None:
        self._marker = Path(marker_path)
        self._dao = dao
        self._limit = int(daily_loss_limit_cents)

    def is_tripped(self) -> bool:
        return self._marker.exists()

    def realized_loss_today_cents(self) -> int:
        """Net realized live P&L for markets settled today (negative = loss)."""
        start_ms, end_ms = _local_day_bounds_ms()
        rows = self._dao.conn.execute(
            """
            SELECT o.side, o.fill_price_cents, o.fee_cents, s.result
            FROM live_orders o JOIN settlements s ON s.ticker = o.ticker
            WHERE o.status = 'filled' AND o.fill_price_cents IS NOT NULL
              AND s.recorded_ts BETWEEN ? AND ?
            """,
            (start_ms, end_ms),
        ).fetchall()
        total = 0
        for r in rows:
            won = (r["side"] == "yes" and r["result"] == "yes") or \
                  (r["side"] == "no" and r["result"] == "no")
            gross = (100 - r["fill_price_cents"]) if won else (-r["fill_price_cents"])
            total += gross - (r["fee_cents"] or 0)
        return total

    def evaluate_and_maybe_trip(self) -> Optional[str]:
        """Trip (persisting the marker) if today's realized loss exceeds the
        limit. Returns a reason string if tripped (now or already), else None."""
        if self.is_tripped():
            return "circuit breaker already tripped (manual reset required)"
        pnl = self.realized_loss_today_cents()
        if pnl <= -self._limit:
            self._marker.parent.mkdir(parents=True, exist_ok=True)
            self._marker.write_text(
                f"tripped: realized loss {pnl}c <= -{self._limit}c on "
                f"{datetime.now().isoformat()}\n"
            )
            return f"daily loss limit hit ({pnl}c <= -{self._limit}c)"
        return None

    def reset(self) -> bool:
        """Human-only reset. Returns True if a tripped marker was cleared."""
        if self._marker.exists():
            self._marker.unlink()
            return True
        return False
