from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from worktrace.db.connection import connect, connect_read_only
from worktrace.db.migrations import migrate, user_version


def test_init_and_migrations_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        assert migrate(connection, database_path) == [1, 2, 3]
        assert migrate(connection, database_path) == []
        assert user_version(connection) == 3

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "apps",
            "sync_runs",
            "source_objects",
            "observations",
            "actors",
            "participations",
            "references",
            "candidate_groups",
            "candidate_members",
            "human_decisions",
            "source_object_availability_events",
        } <= tables
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA trusted_schema").fetchone()[0] == 0
    finally:
        connection.close()


def test_read_only_connection_rejects_mutation(tmp_path: Path) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
    finally:
        connection.close()

    read_only = connect_read_only(database_path)
    try:
        assert read_only.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            read_only.execute(
                "INSERT INTO apps(id, name, market, business_type) VALUES ('x', 'X', '', '')"
            )
    finally:
        read_only.close()
