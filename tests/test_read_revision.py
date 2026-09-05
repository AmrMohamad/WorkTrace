from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from worktrace.candidates.builder import rebuild_candidates
from worktrace.candidates.decisions import append_decision, undo_decision
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import (
    ActorObservation,
    NormalizedObject,
    ParticipationObservation,
    SourceIdentity,
)
from worktrace.linking.builder import rebuild_references


def _app(identifier: str) -> AppConfig:
    return AppConfig(
        id=identifier,
        name=identifier,
        market="XX",
        business_type="fixture",
        jira_project_keys=(),
        gitlab_project_ids=(),
        repo_paths=(),
        jira_key_patterns=(),
        production_environments=(),
        release_tag_patterns=(),
        ignored_paths=(),
    )


def _object(
    app_id: str,
    external_id: str,
    actor: str,
    *,
    actor_name: str = "Fixture",
    participates: bool = True,
    is_self: bool = False,
) -> NormalizedObject:
    return NormalizedObject(
        identity=SourceIdentity("git", "fixture", "git_commit", external_id),
        app_id=app_id,
        title=external_id,
        body_text="fixture",
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        actors=(ActorObservation("git", "fixture", actor, actor_name, None, is_self),),
        participations=(ParticipationObservation(actor, "git_author"),) if participates else (),
        pending_references=(),
        data={},
        completeness=Completeness.COMPLETE,
    )


def test_visible_writes_advance_affected_apps_and_not_noop_metadata(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    try:
        migrate(connection, path)
        repository = EvidenceRepository(connection)
        config = WorkTraceConfig(
            schema_version=1,
            data_directory=tmp_path,
            employment_from=datetime(2026, 1, 1).date(),
            employment_to=datetime(2026, 1, 2).date(),
            identity=IdentityConfig("Fixture", (), (), None, None, None),
            apps=(_app("one"), _app("two")),
            config_path=tmp_path / "config.toml",
        )
        repository.ensure_apps(config)
        before = {
            str(row["id"]): int(row["read_revision"])
            for row in connection.execute("SELECT id, read_revision FROM apps")
        }
        repository.ensure_apps(config)
        assert {
            str(row["id"]): int(row["read_revision"])
            for row in connection.execute("SELECT id, read_revision FROM apps")
        } == before

        first = repository.start_sync_run("one", "git", "fixture", {})
        repository.store_page(first, [_object("one", "a", "shared")])
        repository.finish_sync_run(first, "complete", "complete")
        baseline_two = connection.execute(
            "SELECT read_revision FROM apps WHERE id='two'"
        ).fetchone()[0]
        second = repository.start_sync_run("two", "git", "fixture", {})
        repository.store_page(second, [_object("two", "b", "shared")])
        one_revision = connection.execute(
            "SELECT read_revision FROM apps WHERE id='one'"
        ).fetchone()[0]
        two_revision = connection.execute(
            "SELECT read_revision FROM apps WHERE id='two'"
        ).fetchone()[0]
        assert one_revision > before["one"]
        assert two_revision > baseline_two
    finally:
        connection.close()


def test_actor_display_change_invalidates_historically_reachable_apps(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    try:
        migrate(connection, path)
        repository = EvidenceRepository(connection)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES ('one', 'One', '', '')"
        )
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES ('two', 'Two', '', '')"
        )
        connection.commit()
        first = repository.start_sync_run("one", "git", "fixture", {})
        repository.store_page(first, [_object("one", "a", "shared")])
        repository.finish_sync_run(first, "complete", "complete")
        before = connection.execute("SELECT read_revision FROM apps WHERE id='one'").fetchone()[0]

        second = repository.start_sync_run("two", "git", "fixture", {})
        repository.store_page(
            second,
            [
                _object(
                    "two",
                    "b",
                    "shared",
                    actor_name="Renamed Fixture",
                    participates=False,
                )
            ],
        )

        display_name = connection.execute("SELECT display_name FROM actors").fetchone()[0]
        assert display_name == "Renamed Fixture"
        revision = connection.execute("SELECT read_revision FROM apps WHERE id='one'").fetchone()[0]
        assert revision > before
    finally:
        connection.close()


def test_visible_writer_joins_outer_transaction_and_rolls_back_its_revision(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    try:
        migrate(connection, path)
        repository = EvidenceRepository(connection)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES ('one', 'One', '', '')"
        )
        connection.commit()
        run_id = repository.start_sync_run("one", "git", "fixture", {})
        before = connection.execute("SELECT read_revision FROM apps WHERE id='one'").fetchone()[0]
        connection.autocommit = True
        connection.execute("BEGIN")
        repository.update_run_progress(run_id, {"pages": 1})
        repository.finish_sync_run(run_id, "complete", "complete")
        assert connection.in_transaction
        connection.execute("ROLLBACK")
        row = connection.execute(
            "SELECT status, progress_json FROM sync_runs WHERE id=?", (run_id,)
        ).fetchone()
        assert tuple(row) == ("running", "{}")
        revision = connection.execute("SELECT read_revision FROM apps WHERE id='one'").fetchone()[0]
        assert revision == before
    finally:
        connection.close()


def test_production_writer_matrix_advances_revision_for_visible_state(tmp_path: Path) -> None:
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    try:
        migrate(connection, path)
        repository = EvidenceRepository(connection)
        app = _app("one")
        config = WorkTraceConfig(
            schema_version=1,
            data_directory=tmp_path,
            employment_from=datetime(2026, 1, 1).date(),
            employment_to=datetime(2026, 1, 2).date(),
            identity=IdentityConfig("Fixture", (), (), None, None, None),
            apps=(app,),
            config_path=tmp_path / "config.toml",
        )
        repository.ensure_apps(config)

        def revision() -> int:
            return int(
                connection.execute("SELECT read_revision FROM apps WHERE id='one'").fetchone()[0]
            )

        previous = revision()
        session_id = repository.create_import_session(
            app, datetime(2026, 1, 1).date(), datetime(2026, 1, 2).date()
        )
        assert revision() > previous
        previous = revision()
        repository.update_import_session_progress(session_id, {"stage": "stdio"})
        assert revision() > previous
        previous = revision()
        repository.finish_import_session(session_id, "complete", {"stage": "complete"})
        assert revision() > previous

        previous = revision()
        run_id = repository.start_sync_run("one", "git", "fixture", {})
        assert revision() > previous
        previous = revision()
        repository.update_run_progress(run_id, {"pages": 1})
        assert revision() > previous
        connection.commit()
        previous = revision()
        repository.store_page(run_id, [_object("one", "a", "self", is_self=True)])
        assert revision() > previous
        previous = revision()
        repository.record_object_unavailable(
            run_id,
            source="git",
            source_instance="fixture",
            kind="git_commit",
            external_id="a",
        )
        assert revision() > previous
        previous = revision()
        repository.finish_sync_run(run_id, "complete", "complete")
        assert revision() > previous

        previous = revision()
        assert rebuild_references(app, repository) == 0
        assert revision() > previous
        previous = revision()
        assert rebuild_candidates("one", repository) == 1
        assert revision() > previous
        candidate_id = connection.execute("SELECT id FROM candidate_groups").fetchone()[0]
        previous = revision()
        decision_id = append_decision(connection, "confirm", candidate_id)
        assert revision() > previous
        previous = revision()
        undo_decision(connection, decision_id)
        assert revision() > previous

        stale = repository.start_sync_run("one", "git", "fixture", {})
        connection.execute(
            "UPDATE sync_runs SET started_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (stale,),
        )
        connection.commit()
        previous = revision()
        assert repository.mark_stale_runs_failed() == 1
        assert revision() > previous
    finally:
        connection.close()
