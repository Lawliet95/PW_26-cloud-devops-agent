from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from devops_agent.backend.database import connect, init_db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return init_db(tmp_path / "test.db", seed=42)


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    connection = connect(db_path)
    yield connection
    connection.close()
