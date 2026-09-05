from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from worktrace.db.connection import connect
from worktrace.db.migrations import migrate, migrations, user_version
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import NormalizedObject, SourceIdentity


def _object() -> NormalizedObject:
    return NormalizedObject(
        identity=SourceIdentity("jira", "jira-main", "jira_issue", "10001"),
        app_id="sample",
        title="DEMO-1",
        body_text="bounded fixture",
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        actors=(),
        participations=(),
        pending_references=(),
        data={"key": "DEMO-1"},
        completeness=Completeness.COMPLETE,
    )


def test_populated_v2_upgrade_preserves_ids_and_seeds_baseline(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.sqlite3"
    connection = connect(database_path)
    try:
        for migration in migrations()[:2]:
            connection.executescript(migration.sql)
            connection.execute(f"PRAGMA user_version={migration.version}")
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES ('sample','Sample','','')"
        )
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES ('sync:v2','sample','jira','jira-main','complete',
                      '2026-01-01T00:00:00+00:00','2026-01-01T00:01:00+00:00',
                      '1','{}','complete_for_scope')
            """
        )
        connection.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES ('obj:stable','sample','jira','jira-main','jira_issue','10001',
                      'sync:v2','sync:v2')
            """
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, fetched_at, payload_hash, data_json,
                completeness, adapter_version, normalization_version, redaction_version
            ) VALUES ('obs:stable','obj:stable','sync:v2','2026-01-01T00:01:00+00:00',
                      'hash','{}','complete_for_scope','1','1','1')
            """
        )
        connection.execute(
            """
            INSERT INTO human_decisions(id, action, target_id, payload_json, created_at)
            VALUES ('decision:stable','attest_claim','obj:stable','{}',
                    '2026-01-01T00:02:00+00:00')
            """
        )
        connection.commit()

        assert migrate(connection, database_path) == [3, 4, 5]
        assert user_version(connection) == 5
        assert connection.execute("SELECT id FROM source_objects").fetchone()[0] == "obj:stable"
        assert connection.execute("SELECT id FROM observations").fetchone()[0] == "obs:stable"
        assert (
            connection.execute("SELECT id FROM human_decisions").fetchone()[0] == "decision:stable"
        )
        event = connection.execute(
            "SELECT state, reason, sync_run_id FROM source_object_availability_events"
        ).fetchone()
        assert tuple(event) == ("visible", "migration_baseline", "sync:v2")
    finally:
        connection.close()


def test_only_complete_run_projects_unavailable_and_reappearance(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES ('sample','Sample','','')"
        )
        repository = EvidenceRepository(connection)
        first = repository.start_sync_run(
            "sample", "jira", "jira-main", {"selection_policy_version": 2}
        )
        repository.store_page(first, [_object()])
        repository.finish_sync_run(first, "complete", "complete_for_scope")
        object_id = str(connection.execute("SELECT id FROM source_objects").fetchone()[0])

        failed = repository.start_sync_run(
            "sample", "jira", "jira-main", {"selection_policy_version": 2}
        )
        repository.record_object_unavailable(
            failed,
            source="jira",
            source_instance="jira-main",
            kind="jira_issue",
            external_id="10001",
        )
        repository.finish_sync_run(failed, "failed", "partial", "fixture failure")
        assert (
            connection.execute(
                "SELECT availability FROM source_objects WHERE id=?", (object_id,)
            ).fetchone()[0]
            == "visible"
        )

        unavailable = repository.start_sync_run(
            "sample", "jira", "jira-main", {"selection_policy_version": 2}
        )
        repository.record_object_unavailable(
            unavailable,
            source="jira",
            source_instance="jira-main",
            kind="jira_issue",
            external_id="10001",
        )
        repository.finish_sync_run(unavailable, "complete", "complete_for_scope")
        state = connection.execute(
            "SELECT availability, availability_reason FROM source_objects WHERE id=?",
            (object_id,),
        ).fetchone()
        assert tuple(state) == ("unavailable", "not_found")

        reappeared = repository.start_sync_run(
            "sample", "jira", "jira-main", {"selection_policy_version": 2}
        )
        repository.store_page(reappeared, [_object()])
        repository.finish_sync_run(reappeared, "complete", "complete_for_scope")
        state = connection.execute(
            "SELECT availability, availability_reason FROM source_objects WHERE id=?",
            (object_id,),
        ).fetchone()
        assert tuple(state) == ("visible", "reappeared")
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_object_availability_events WHERE source_object_id=?",
                (object_id,),
            ).fetchone()[0]
            == 4
        )
    finally:
        connection.close()
