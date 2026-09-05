from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from worktrace.db.connection import connect, connect_read_only
from worktrace.db.migrations import migrate, user_version
from worktrace.db.readiness import DatabaseReadinessStatus, database_readiness


def test_init_and_migrations_are_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        assert migrate(connection, database_path) == [1, 2, 3, 4]
        assert migrate(connection, database_path) == []
        assert user_version(connection) == 4

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


def test_read_only_connection_accepts_an_exact_short_busy_timeout(tmp_path: Path) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    writer = connect(database_path)
    try:
        migrate(writer, database_path)
    finally:
        writer.close()

    read_only = connect_read_only(database_path, busy_timeout_ms=500)
    try:
        assert read_only.execute("PRAGMA query_only").fetchone()[0] == 1
        assert read_only.execute("PRAGMA busy_timeout").fetchone()[0] == 500
        assert read_only.execute("PRAGMA database_list").fetchone()[2] == str(
            database_path.resolve()
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_only.execute(
                "INSERT INTO apps(id, name, market, business_type) "
                "VALUES ('blocked', 'Blocked', '', '')"
            )
    finally:
        read_only.close()


@pytest.mark.parametrize(
    "filename",
    (
        "ledger ?mode=rw&x=.sqlite3",
        "ledger#fragment.sqlite3",
        "ledger with spaces.sqlite3",
    ),
)
def test_read_only_connection_encodes_uri_metacharacters_and_keeps_os_read_only(
    tmp_path: Path,
    filename: str,
) -> None:
    database_path = tmp_path / filename
    writer = connect(database_path)
    try:
        migrate(writer, database_path)
    finally:
        writer.close()
    entries_before = {path.name for path in tmp_path.iterdir()}

    read_only = connect_read_only(database_path, busy_timeout_ms=500)
    try:
        assert read_only.execute("PRAGMA database_list").fetchone()[2] == str(
            database_path.resolve()
        )
        assert read_only.execute("PRAGMA query_only").fetchone()[0] == 1

        read_only.execute("PRAGMA query_only = OFF")
        assert read_only.execute("PRAGMA query_only").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_only.execute(
                "INSERT INTO apps(id, name, market, business_type) "
                "VALUES ('blocked', 'Blocked', '', '')"
            )
    finally:
        read_only.close()

    assert {path.name for path in tmp_path.iterdir()} == entries_before


def test_read_only_lock_contention_obeys_the_short_timeout(tmp_path: Path) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    writer = connect(database_path)
    migrate(writer, database_path)
    writer.autocommit = True
    writer.execute("BEGIN EXCLUSIVE")
    try:
        read_only = connect_read_only(database_path, busy_timeout_ms=500)
        started = time.monotonic()
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                read_only.execute("SELECT * FROM apps").fetchall()
        finally:
            read_only.close()
        assert 0.4 <= time.monotonic() - started < 1.5
    finally:
        writer.execute("ROLLBACK")
        writer.close()


def test_database_readiness_is_read_only_and_distinguishes_schema_drift(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    writer = connect(database_path)
    try:
        migrate(writer, database_path)
    finally:
        writer.close()

    read_only = connect_read_only(database_path)
    try:
        ready = database_readiness(read_only)
        assert ready.status is DatabaseReadinessStatus.READY
        assert ready.current_version == ready.supported_version == 4
    finally:
        read_only.close()

    writer = connect(database_path)
    try:
        writer.execute("PRAGMA user_version = 2")
        writer.commit()
    finally:
        writer.close()
    read_only = connect_read_only(database_path)
    try:
        older = database_readiness(read_only)
        assert older.status is DatabaseReadinessStatus.UPGRADE_REQUIRED
        assert older.current_version == 2
        assert user_version(read_only) == 2
    finally:
        read_only.close()

    writer = connect(database_path)
    try:
        writer.execute("PRAGMA user_version = 99")
        writer.commit()
    finally:
        writer.close()
    read_only = connect_read_only(database_path)
    try:
        newer = database_readiness(read_only)
        assert newer.status is DatabaseReadinessStatus.UNSUPPORTED_NEWER
        assert newer.current_version == 99
        assert user_version(read_only) == 99
    finally:
        read_only.close()
