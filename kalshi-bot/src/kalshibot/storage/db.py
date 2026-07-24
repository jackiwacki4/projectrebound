"""Database connection management: WAL mode, single file, survives restarts."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .schema import DDL, SCHEMA_VERSION


def connect(db_path: str) -> sqlite3.Connection:
    """Open (creating if needed) the SQLite database in WAL mode.

    WAL lets a reader (e.g. the reporting CLI) run while the collector writes,
    and survives a hard process kill without corrupting the file.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    row = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
