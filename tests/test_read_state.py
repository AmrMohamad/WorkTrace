from __future__ import annotations

import threading
from pathlib import Path

import pytest

from worktrace.db.connection import connect, connect_read_only
from worktrace.db.migrations import migrate
from worktrace.db.read_state import READ_MODEL_VERSION, READ_PROTOCOL_VERSION, read_snapshot
from worktrace.errors import DatabaseError


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    try:
        migrate(connection, path)
    finally:
        connection.close()
    return path


def test_read_snapshot_requires_query_only_connection(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = connect(path)
    try:
        with pytest.raises(DatabaseError, match="query-only"), read_snapshot(connection):
            pass
    finally:
        connection.close()


def test_read_snapshot_owns_and_rolls_back_on_success_and_failure(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = connect_read_only(path)
    try:
        assert (READ_PROTOCOL_VERSION, READ_MODEL_VERSION) == (1, 1)
        with read_snapshot(connection):
            assert connection.in_transaction
        assert not connection.in_transaction

        with pytest.raises(RuntimeError, match="fixture"), read_snapshot(connection):
            assert connection.in_transaction
            raise RuntimeError("fixture")
        assert not connection.in_transaction

        with pytest.raises(KeyboardInterrupt), read_snapshot(connection):
            assert connection.in_transaction
            raise KeyboardInterrupt
        assert not connection.in_transaction
    finally:
        connection.close()


def test_read_snapshot_preserves_a_caller_owned_transaction(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = connect_read_only(path)
    try:
        connection.execute("BEGIN")
        with pytest.raises(KeyboardInterrupt), read_snapshot(connection):
            assert connection.in_transaction
            raise KeyboardInterrupt
        assert connection.in_transaction
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize("journal_mode", ("WAL", "DELETE"))
def test_read_snapshot_has_journal_correct_writer_concurrency(
    tmp_path: Path, journal_mode: str
) -> None:
    path = _database(tmp_path)
    setup = connect(path)
    try:
        setup.autocommit = True
        configured_mode = str(setup.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0])
        assert configured_mode.upper() == journal_mode
        setup.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES ('sample', 'Sample', '', '')"
        )
    finally:
        setup.close()

    writer_started = threading.Event()
    writer_updated = threading.Event()
    commit_started = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []

    def write_revision() -> None:
        writer = connect(path)
        try:
            writer.autocommit = True
            if journal_mode == "DELETE":
                writer.execute("BEGIN IMMEDIATE")
            writer_started.set()
            writer.execute("UPDATE apps SET read_revision=read_revision+1 WHERE id='sample'")
            writer_updated.set()
            commit_started.set()
            if journal_mode == "DELETE":
                writer.execute("COMMIT")
            writer_finished.set()
        except BaseException as exc:  # pragma: no cover - surfaced in the parent thread
            failures.append(exc)
            writer_finished.set()
        finally:
            writer.close()

    reader = connect_read_only(path)
    thread = threading.Thread(target=write_revision)
    try:
        with read_snapshot(reader):
            revision = reader.execute(
                "SELECT read_revision FROM apps WHERE id='sample'"
            ).fetchone()[0]
            assert revision == 0
            thread.start()
            assert writer_started.wait(2)
            assert writer_updated.wait(2)
            assert commit_started.wait(2)
            if journal_mode == "WAL":
                assert writer_finished.wait(2)
            else:
                assert not writer_finished.wait(0.05)
            revision = reader.execute(
                "SELECT read_revision FROM apps WHERE id='sample'"
            ).fetchone()[0]
            assert revision == 0
        assert writer_finished.wait(2)
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert not failures
        assert reader.execute("SELECT read_revision FROM apps WHERE id='sample'").fetchone()[0] == 1
    finally:
        reader.close()
        if thread.is_alive():
            thread.join(timeout=2)
