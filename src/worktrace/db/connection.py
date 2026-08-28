from __future__ import annotations

import sqlite3
from pathlib import Path


def _configure(connection: sqlite3.Connection) -> sqlite3.Connection:
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def connect(path: Path) -> sqlite3.Connection:
    connection = _configure(sqlite3.connect(path, autocommit=True, timeout=10))
    connection.autocommit = False
    return connection


def connect_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = _configure(sqlite3.connect(uri, uri=True, autocommit=True, timeout=10))
    connection.execute("PRAGMA query_only = ON")
    return connection
