from __future__ import annotations

import sqlite3
from pathlib import Path


def _configure(
    connection: sqlite3.Connection,
    *,
    busy_timeout_ms: int = 10_000,
) -> sqlite3.Connection:
    if busy_timeout_ms < 0:
        raise ValueError("busy_timeout_ms must be non-negative")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA trusted_schema = OFF")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return connection


def connect(path: Path) -> sqlite3.Connection:
    connection = _configure(sqlite3.connect(path, autocommit=True, timeout=10))
    connection.autocommit = False
    return connection


def connect_read_only(
    path: Path,
    *,
    busy_timeout_ms: int = 10_000,
) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = _configure(
        sqlite3.connect(
            uri,
            uri=True,
            autocommit=True,
            timeout=busy_timeout_ms / 1_000,
        ),
        busy_timeout_ms=busy_timeout_ms,
    )
    connection.execute("PRAGMA query_only = ON")
    return connection
