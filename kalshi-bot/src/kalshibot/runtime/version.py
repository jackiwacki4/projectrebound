"""Which code is actually running.

`git pull` does NOT change a running collector. Python read the source once at
startup and keeps using it until the process restarts, so a pull leaves the old
model running with no outward sign -- the logs look identical and the data keeps
arriving. That is the failure this module exists to make visible.

Every collector stamps the commit it started from into a small file beside its
database (the same convention as the circuit-breaker marker), so `status.sh` can
compare "the code this process is running" against "the code in the folder" and
say plainly whether a restart is owed.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

UNKNOWN = "unknown"


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "-C", str(root), *args],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        # No git, not a checkout, git hung -- none of which should stop a
        # collector from starting. Version reporting is diagnostics, not runtime.
        return None


def git_revision(root: Optional[Path] = None) -> str:
    """Short commit of the checked-out code, marked if it has local edits.

    The `+local` suffix matters: an operator who edited a file in place is NOT
    running any published commit, and a bare hash would claim otherwise.
    """
    root = root or Path(__file__).resolve().parents[3]
    rev = _git(root, "rev-parse", "--short", "HEAD")
    if not rev:
        return UNKNOWN
    dirty = _git(root, "status", "--porcelain")
    return f"{rev}+local" if dirty else rev


def stamp_path(db_path: str) -> Path:
    return Path(f"{db_path}.version")


def write_version_stamp(db_path: str, family: str,
                        revision: Optional[str] = None) -> dict:
    """Record what this process is running. Never raises: a diagnostics file
    failing to write must not take a collector down with it."""
    info = {
        "revision": revision or git_revision(),
        "family": family,
        "started": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
    }
    try:
        p = stamp_path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(info, indent=2) + "\n")
    except Exception:
        pass
    return info


def read_version_stamp(db_path: str) -> Optional[dict]:
    try:
        return json.loads(stamp_path(db_path).read_text())
    except Exception:
        return None
