from __future__ import annotations

import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

import pytest

import worktrace.db.migrations as migration_module
from worktrace.db.connection import connect
from worktrace.db.migrations import Migration, backup_database, migrate, user_version
from worktrace.errors import DatabaseError


def _populate(path: Path, *, wal: bool = False) -> sqlite3.Connection:
    connection = sqlite3.connect(path, autocommit=True)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE sentinel(value TEXT)")
    connection.execute("INSERT INTO sentinel VALUES ('original')")
    return connection


def _one_migration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migration = Migration(1, tmp_path / "001_fixture.sql", "CREATE TABLE added(value TEXT);\n")
    monkeypatch.setattr(migration_module, "migrations", lambda: (migration,))


def test_backup_contains_latest_committed_wal_pages(tmp_path: Path) -> None:
    path = tmp_path / "ledger ?# .sqlite3"
    destination = tmp_path / "private" / "snapshot.backup"
    with closing(_populate(path, wal=True)) as writer:
        writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        main_before = path.read_bytes()
        writer.execute("INSERT INTO sentinel VALUES ('committed-in-wal')")
        assert path.read_bytes() == main_before
        assert Path(f"{path}-wal").stat().st_size > 0

        assert backup_database(path, destination) == destination
        with closing(sqlite3.connect(destination)) as restored:
            assert restored.execute("SELECT value FROM sentinel ORDER BY rowid").fetchall() == [
                ("original",),
                ("committed-in-wal",),
            ]
            assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600
        assert stat.S_IMODE(destination.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("symlink", [False, True])
def test_backup_never_replaces_existing_destination(tmp_path: Path, symlink: bool) -> None:
    path = tmp_path / "ledger.sqlite3"
    destination = tmp_path / "snapshot.backup"
    protected = tmp_path / "protected"
    protected.write_bytes(b"keep this")
    if symlink:
        destination.symlink_to(protected)
    else:
        destination.write_bytes(b"keep this")
    with closing(_populate(path)), pytest.raises(FileExistsError):
        backup_database(path, destination)
    assert destination.read_bytes() == protected.read_bytes() == b"keep this"
    assert destination.is_symlink() is symlink


def test_backup_contention_is_bounded_and_retry_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(migration_module, "BACKUP_TIMEOUT_SECONDS", 0.05)
    path = tmp_path / "ledger.sqlite3"
    destination = tmp_path / "snapshot.backup"
    with closing(_populate(path)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("INSERT INTO sentinel VALUES ('uncommitted')")
        started = time.monotonic()
        with pytest.raises(DatabaseError, match="backup timed out"):
            backup_database(path, destination)
        assert time.monotonic() - started < 1
        assert not destination.exists()
        writer.execute("ROLLBACK")
        assert backup_database(path, destination) == destination
        with closing(sqlite3.connect(destination)) as restored:
            assert restored.execute("SELECT value FROM sentinel").fetchall() == [("original",)]


def test_backup_failure_cleans_only_new_destination(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite3"
    original = b"not a SQLite database"
    path.write_bytes(original)
    destination = tmp_path / "snapshot.backup"
    with pytest.raises(DatabaseError, match="backup failed"):
        backup_database(path, destination)
    assert not destination.exists()
    assert path.read_bytes() == original


@pytest.mark.parametrize("wal", [False, True])
def test_migration_backs_up_before_changes_with_autocommit_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wal: bool
) -> None:
    _one_migration(tmp_path, monkeypatch)
    path = tmp_path / "ledger.sqlite3"
    with closing(_populate(path, wal=wal)) as writer, closing(connect(path)) as connection:
        writer.execute("INSERT INTO sentinel VALUES ('latest')")
        assert connection.autocommit is False
        assert migrate(connection, path) == [1]
        assert connection.autocommit is False
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 10_000
        assert user_version(connection) == 1
        backups = list(tmp_path.glob("*.backup"))
        assert len(backups) == 1
        with closing(sqlite3.connect(backups[0])) as restored:
            assert user_version(restored) == 0
            assert restored.execute("SELECT value FROM sentinel ORDER BY rowid").fetchall() == [
                ("original",),
                ("latest",),
            ]
            assert (
                restored.execute("SELECT name FROM sqlite_master WHERE name='added'").fetchone()
                is None
            )


def test_migration_contention_aborts_before_backup_or_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_migration(tmp_path, monkeypatch)
    monkeypatch.setattr(migration_module, "MIGRATION_BUSY_TIMEOUT_MS", 50)
    path = tmp_path / "ledger.sqlite3"
    with closing(_populate(path)) as writer, closing(connect(path)) as connection:
        writer.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        with pytest.raises(DatabaseError, match="lock"):
            migrate(connection, path)
        assert time.monotonic() - started < 1
        assert not list(tmp_path.glob("*.backup"))
        assert user_version(connection) == 0
        assert connection.autocommit is False
        writer.execute("ROLLBACK")
        assert migrate(connection, path) == [1]


def test_migration_keeps_writer_reservation_during_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_migration(tmp_path, monkeypatch)
    path = tmp_path / "ledger.sqlite3"
    original_backup = backup_database
    blocked_writes: list[bool] = []

    def inspect_backup(path: Path, destination: Path | None = None) -> Path | None:
        with closing(sqlite3.connect(path, autocommit=True, timeout=0)) as other:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("INSERT INTO sentinel VALUES ('interloper')")
            blocked_writes.append(True)
        return original_backup(path, destination)

    monkeypatch.setattr(migration_module, "backup_database", inspect_backup)
    with closing(_populate(path)), closing(connect(path)) as connection:
        assert migrate(connection, path) == [1]
    assert blocked_writes == [True]


def test_reader_blocking_migration_commit_rolls_back_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_migration(tmp_path, monkeypatch)
    monkeypatch.setattr(migration_module, "MIGRATION_BUSY_TIMEOUT_MS", 50)
    path = tmp_path / "ledger.sqlite3"
    with closing(_populate(path)) as reader, closing(connect(path)) as connection:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM sentinel").fetchall()
        started = time.monotonic()
        with pytest.raises(DatabaseError, match="lock"):
            migrate(connection, path)
        assert time.monotonic() - started < 1
        assert user_version(connection) == 0
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='added'").fetchone()
            is None
        )
        assert len(list(tmp_path.glob("*.backup"))) == 1
        reader.execute("ROLLBACK")
        assert migrate(connection, path) == [1]


def test_later_migration_failure_rolls_back_the_whole_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "ledger.sqlite3"
    migrations = (
        Migration(1, tmp_path / "001_first.sql", "CREATE TABLE added(value TEXT);\n"),
        Migration(2, tmp_path / "002_broken.sql", "INSERT INTO missing VALUES (1);\n"),
    )
    monkeypatch.setattr(migration_module, "migrations", lambda: migrations)
    with closing(_populate(path)), closing(connect(path)) as connection:
        with pytest.raises(DatabaseError, match="002_broken"):
            migrate(connection, path)
        assert user_version(connection) == 0
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name='added'").fetchone()
            is None
        )
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "original"


def test_newer_schema_is_rejected_without_backup_or_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_migration(tmp_path, monkeypatch)
    path = tmp_path / "ledger.sqlite3"
    with closing(_populate(path)) as writer:
        writer.execute("PRAGMA user_version=99")
    with closing(connect(path)) as connection:
        with pytest.raises(DatabaseError, match="newer"):
            migrate(connection, path)
        assert user_version(connection) == 99
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "original"
        assert connection.autocommit is False
        assert not list(tmp_path.glob("*.backup"))


def test_backup_failure_prevents_migration_and_releases_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _one_migration(tmp_path, monkeypatch)
    path = tmp_path / "ledger.sqlite3"

    def failed_backup(path: Path, destination: Path | None = None) -> Path | None:
        raise DatabaseError("synthetic backup failure")

    monkeypatch.setattr(migration_module, "backup_database", failed_backup)
    with closing(_populate(path)), closing(connect(path)) as connection:
        with pytest.raises(DatabaseError, match="synthetic backup failure"):
            migrate(connection, path)
        assert user_version(connection) == 0
        connection.rollback()
        with closing(sqlite3.connect(path, autocommit=True, timeout=0)) as other:
            other.execute("INSERT INTO sentinel VALUES ('writer released')")
