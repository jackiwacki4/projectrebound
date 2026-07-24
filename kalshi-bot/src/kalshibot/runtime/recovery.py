"""Sleep/wake + network-loss recovery.

After a laptop sleep, a network drop, or any collection failure, cached market
state is stale and must NOT drive orders until a fresh resync completes. This
tracks that stale flag. The trading path checks it (via the staleness gate) and
refuses live orders while stale; data collection continues regardless.
"""
from __future__ import annotations

import time


class StaleState:
    def __init__(self) -> None:
        # Start stale: nothing is trusted until the first successful resync.
        self._stale = True
        self._reason = "startup"
        self._last_mono = time.monotonic()

    @property
    def is_stale(self) -> bool:
        return self._stale

    @property
    def reason(self) -> str:
        return self._reason

    def mark_stale(self, reason: str) -> None:
        self._stale = True
        self._reason = reason

    def mark_fresh(self) -> None:
        self._stale = False
        self._reason = ""

    def check_wake_gap(self, expected_period_s: float) -> bool:
        """Detect a sleep/pause by comparing elapsed monotonic time to the
        expected loop period. Returns True (and marks stale) if a gap is found."""
        now = time.monotonic()
        elapsed = now - self._last_mono
        self._last_mono = now
        # Generous tolerance: only a real sleep/pause trips this.
        if elapsed > max(expected_period_s * 3, expected_period_s + 60):
            self.mark_stale(f"time gap of {elapsed:.0f}s detected (sleep/pause)")
            return True
        return False
