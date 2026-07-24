"""File-based kill switch.

If the configured file exists, ALL live trading halts. Checked before every
single order (not just at startup), so the operator can stop live trading
instantly by creating the file -- e.g. `touch ./HALT` -- without restarting
the process. Data collection continues regardless.
"""
from __future__ import annotations

from pathlib import Path


class KillSwitch:
    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def engaged(self) -> bool:
        return self._path.exists()

    @property
    def path(self) -> str:
        return str(self._path)
