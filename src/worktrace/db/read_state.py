"""Small primitives shared by read models and visible-state writers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from worktrace.errors import DatabaseError

READ_PROTOCOL_VERSION = 1
READ_MODEL_VERSION = 2


def mark_read_state_changed(connection: sqlite3.Connection, app_id: str) -> None:
    """Advance one app's visible-state revision without taking ownership of a commit."""
    connection.execute("UPDATE apps SET read_revision=read_revision+1 WHERE id=?", (app_id,))


def mark_read_states_changed(connection: sqlite3.Connection, app_ids: Iterable[str]) -> None:
    """Advance each distinct affected app in the caller's transaction."""
    for app_id in sorted(set(app_ids)):
        mark_read_state_changed(connection, app_id)


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Use one query-only SQLite snapshot, joining a caller-owned transaction."""
    query_only = connection.execute("PRAGMA query_only").fetchone()
    if query_only is None or int(query_only[0]) != 1:
        raise DatabaseError("read snapshot requires a query-only SQLite connection")
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        yield connection
    finally:
        if owns_transaction and connection.in_transaction:
            connection.execute("ROLLBACK")
