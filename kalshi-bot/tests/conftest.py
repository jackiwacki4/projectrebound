"""Shared test fixtures."""
import sqlite3

import pytest

from kalshibot.storage.dao import Dao
from kalshibot.storage.db import connect


@pytest.fixture
def dao(tmp_path) -> Dao:
    conn: sqlite3.Connection = connect(str(tmp_path / "test.db"))
    return Dao(conn)
