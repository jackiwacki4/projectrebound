"""Health-check reporting."""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict, dataclass
from typing import Optional

from ..risk.circuit_breaker import CircuitBreaker
from ..risk.kill_switch import KillSwitch
from ..storage.dao import Dao
from ..util import now_ms

_START_MONO = time.monotonic()


@dataclass
class Health:
    uptime_seconds: int
    last_book_snapshot_ms: Optional[int]
    last_forecast_ms: Optional[int]        # weather family input
    last_sports_rating_ms: Optional[int]   # sports family input
    last_game_state_ms: Optional[int]      # sports family observation
    open_markets: int
    settled_markets: int
    kill_switch_engaged: bool
    circuit_breaker_tripped: bool
    disk_free_mb: int
    state_stale: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


def collect_health(dao: Dao, kill: KillSwitch, breaker: CircuitBreaker,
                   db_path: str, state_stale: bool) -> Health:
    conn = dao.conn
    last_book = conn.execute("SELECT MAX(captured_ts) AS t FROM book_snapshots").fetchone()["t"]
    last_fc = conn.execute("SELECT MAX(fetched_ts) AS t FROM forecasts").fetchone()["t"]
    # Both families' input timestamps are reported regardless of which one is
    # running: a null here tells you at a glance that a family has never
    # collected, which is exactly what you want to see after a config switch.
    last_rating = conn.execute(
        "SELECT MAX(fetched_ts) AS t FROM sports_ratings").fetchone()["t"]
    last_state = conn.execute(
        "SELECT MAX(captured_ts) AS t FROM sports_game_states").fetchone()["t"]
    open_markets = conn.execute("SELECT COUNT(*) AS c FROM markets WHERE status='open'").fetchone()["c"]
    settled = conn.execute("SELECT COUNT(*) AS c FROM settlements").fetchone()["c"]
    free_mb = shutil.disk_usage(".").free // (1024 * 1024)
    return Health(
        uptime_seconds=int(time.monotonic() - _START_MONO),
        last_book_snapshot_ms=last_book,
        last_forecast_ms=last_fc,
        last_sports_rating_ms=last_rating,
        last_game_state_ms=last_state,
        open_markets=open_markets,
        settled_markets=settled,
        kill_switch_engaged=kill.engaged(),
        circuit_breaker_tripped=breaker.is_tripped(),
        disk_free_mb=int(free_mb),
        state_stale=state_stale,
    )
