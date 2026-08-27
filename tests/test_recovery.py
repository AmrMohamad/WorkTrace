from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import worktrace.db.migrations as migration_module
from worktrace.db.connection import connect
from worktrace.db.migrations import Migration, migrate, user_version
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import NormalizedObject, SourceIdentity
from worktrace.errors import DatabaseError


def _object(*, title: str) -> NormalizedObject:
    return NormalizedObject(
        identity=SourceIdentity(
            source="git",
            source_instance="fixture-repository",
            kind="git_commit",
            external_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        ),
        app_id="sample_store",
        title=title,
        body_text="DEMO-101 synthetic evidence",
        source_updated_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
        actors=(),
        participations=(),
        pending_references=(),
        data={"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
        completeness=Completeness.COMPLETE,
    )


def test_failed_migration_rolls_back_and_preserves_existing_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    connection.execute("INSERT INTO sentinel VALUES ('preserved')")
    connection.commit()
    broken = Migration(
        version=1,
        path=tmp_path / "001_broken.sql",
        sql="CREATE TABLE transient(value TEXT); INSERT INTO missing_table VALUES (1);",
    )
    monkeypatch.setattr(migration_module, "migrations", lambda: (broken,))

    try:
        with pytest.raises(DatabaseError, match=r"migration 001_broken\.sql failed"):
            migrate(connection, database_path)

        assert user_version(connection) == 0
        assert connection.execute("SELECT value FROM sentinel").fetchone()[0] == "preserved"
        transient = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transient'"
        ).fetchone()
        assert transient is None
        assert len(list(tmp_path.glob("worktrace.sqlite3.*.backup"))) == 1
    finally:
        connection.close()


@pytest.mark.recovery
def test_previous_complete_run_remains_current_after_failed_later_run(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, '', '')",
            ("sample_store", "Sample Store"),
        )
        connection.commit()
        repository = EvidenceRepository(connection)

        complete_run = repository.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"mode": "fixture"},
        )
        repository.store_page(complete_run, [_object(title="Complete observation")])
        repository.finish_sync_run(complete_run, "complete", "complete_for_scope")

        failed_run = repository.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"mode": "fixture"},
        )
        repository.store_page(failed_run, [_object(title="Uncommitted failed observation")])
        repository.finish_sync_run(
            failed_run,
            "failed",
            "partial",
            "synthetic interruption",
        )

        current = repository.current_observations("sample_store")
        assert len(current) == 1
        assert current[0]["title"] == "Complete observation"
        assert current[0]["sync_run_id"] == complete_run
        assert (
            connection.execute("SELECT status FROM sync_runs WHERE id=?", (failed_run,)).fetchone()[
                0
            ]
            == "failed"
        )
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    finally:
        connection.close()


def test_failed_page_transaction_does_not_leave_partial_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, '', '')",
            ("sample_store", "Sample Store"),
        )
        connection.commit()
        repository = EvidenceRepository(connection)
        run_id = repository.start_sync_run(
            "sample_store", "git", "fixture-repository", {"mode": "fixture"}
        )

        invalid = _object(title="Invalid")
        invalid = NormalizedObject(
            identity=invalid.identity,
            app_id="unconfigured_app",
            title=invalid.title,
            body_text=invalid.body_text,
            source_updated_at=invalid.source_updated_at,
            actors=invalid.actors,
            participations=invalid.participations,
            pending_references=invalid.pending_references,
            data=invalid.data,
            completeness=invalid.completeness,
        )
        with pytest.raises(DatabaseError, match="configured application scope"):
            repository.store_page(run_id, [_object(title="Valid before failure"), invalid])

        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM source_objects").fetchone()[0] == 0
    finally:
        connection.close()
