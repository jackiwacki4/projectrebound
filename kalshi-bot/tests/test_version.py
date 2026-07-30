"""Knowing which code a running collector is actually using.

`git pull` does not restart anything: Python read the source once at startup and
keeps using it. A pull therefore leaves the old model running with no outward
sign -- same log lines, same data arriving, silently stale predictions. These
tests pin the stamp that makes that visible.
"""
import json
import subprocess
from pathlib import Path

from kalshibot.runtime import version


def test_revision_is_the_checked_out_commit():
    rev = version.git_revision()
    assert rev != version.UNKNOWN
    real = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    assert rev.startswith(real)


def test_local_edits_are_flagged_rather_than_reported_as_a_clean_commit(tmp_path):
    """An operator who edited a file in place is running NO published commit.
    A bare hash would claim otherwise, which is the sort of small lie that makes
    a later "but it worked on that version" impossible to resolve."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                    capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "t@example.com")
    run("config", "user.name", "t")
    (repo / "f.txt").write_text("one\n")
    run("add", "-A")
    run("commit", "-qm", "first")

    clean = version.git_revision(repo)
    assert clean != version.UNKNOWN
    assert "+local" not in clean

    (repo / "f.txt").write_text("edited\n")
    assert version.git_revision(repo) == f"{clean}+local"


def test_unknown_outside_a_checkout(tmp_path):
    """Version reporting is diagnostics. Not being in a git checkout must yield
    'unknown', never an exception that stops a collector from starting."""
    assert version.git_revision(tmp_path) == version.UNKNOWN


def test_stamp_round_trip(tmp_path):
    db = str(tmp_path / "sub" / "x.db")          # parent does not exist yet
    written = version.write_version_stamp(db, "sports", revision="abc1234")
    assert written["revision"] == "abc1234"
    assert written["family"] == "sports"

    read = version.read_version_stamp(db)
    assert read["revision"] == "abc1234"
    assert read["pid"] == written["pid"]
    # Beside the database, matching the circuit-breaker marker convention.
    assert version.stamp_path(db) == Path(f"{db}.version")


def test_missing_stamp_reads_as_none_not_an_error(tmp_path):
    assert version.read_version_stamp(str(tmp_path / "never-written.db")) is None


def test_corrupt_stamp_reads_as_none(tmp_path):
    """A truncated write (killed mid-flush) must not crash the status output."""
    db = tmp_path / "x.db"
    Path(f"{db}.version").write_text("{not json")
    assert version.read_version_stamp(str(db)) is None


def test_writing_a_stamp_never_raises(tmp_path):
    """A diagnostics file failing to write must not take a collector down."""
    unwritable = tmp_path / "file-not-a-dir"
    unwritable.write_text("x")
    info = version.write_version_stamp(str(unwritable / "nope.db"), "sports")
    assert info["family"] == "sports"           # returns the info regardless
    assert version.read_version_stamp(str(unwritable / "nope.db")) is None


def test_stamp_records_a_restart(tmp_path):
    """Restarting must overwrite, so 'running' always means the current process."""
    db = str(tmp_path / "x.db")
    version.write_version_stamp(db, "sports", revision="old111")
    version.write_version_stamp(db, "sports", revision="new222")
    assert version.read_version_stamp(db)["revision"] == "new222"


def test_stamp_is_readable_json_for_a_human(tmp_path):
    db = str(tmp_path / "x.db")
    version.write_version_stamp(db, "weather", revision="abc1234")
    raw = Path(f"{db}.version").read_text()
    assert "\n" in raw                           # indented, not one long line
    assert set(json.loads(raw)) == {"revision", "family", "started", "pid"}
