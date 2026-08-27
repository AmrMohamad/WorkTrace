from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from worktrace.candidates.builder import rebuild_candidates
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.queries import evidence_excerpt, search_evidence, source_status
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import NotFound
from worktrace.mcp_server.tools import WorkTraceTools
from worktrace.packets.builder import PacketBuilder
from worktrace.services import export_app


def _config(tmp_path: Path) -> WorkTraceConfig:
    return WorkTraceConfig(
        schema_version=1,
        data_directory=tmp_path,
        employment_from=date(2020, 1, 1),
        employment_to=date(2026, 12, 31),
        identity=IdentityConfig(
            display_name="Fixture Engineer",
            git_author_emails=(),
            git_author_names=(),
            jira_account_id="fixture-self",
            gitlab_user_id=None,
            gitlab_username=None,
        ),
        apps=(
            AppConfig(
                id="sample_store",
                name="Sample Store",
                market="XX",
                business_type="fixture",
                jira_project_keys=("DEMO",),
                gitlab_project_ids=(),
                repo_paths=(),
                jira_key_patterns=(r"DEMO-[0-9]+",),
                production_environments=(),
                release_tag_patterns=(),
                ignored_paths=(),
            ),
        ),
        config_path=tmp_path / "config.toml",
    )


def _ledger(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    database_path = tmp_path / "ledger.sqlite3"
    connection = connect(database_path)
    migrate(connection, database_path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) "
        "VALUES ('sample_store', 'Sample Store', 'XX', 'fixture')"
    )
    connection.commit()
    return connection, database_path


def _insert_remote_observation(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    object_id: str,
    observation_id: str,
    status: str,
    scope: dict[str, object],
    completed_at: str,
    title: str,
    completeness: str = "complete_for_scope",
) -> None:
    connection.execute(
        """
        INSERT INTO sync_runs(
            id, app_id, source, source_instance, status, started_at, completed_at,
            adapter_version, scope_json, completeness
        ) VALUES (?, 'sample_store', 'jira', 'jira-main', ?, ?, ?, 'fixture', ?, ?)
        """,
        (run_id, status, completed_at, completed_at, json.dumps(scope), completeness),
    )
    connection.execute(
        """
        INSERT INTO source_objects(
            id, app_id, source, source_instance, kind, external_id,
            first_seen_run_id, last_seen_run_id
        ) VALUES (?, 'sample_store', 'jira', 'jira-main', 'jira_issue', ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET last_seen_run_id=excluded.last_seen_run_id
        """,
        (object_id, object_id, run_id, run_id),
    )
    connection.execute(
        """
        INSERT INTO observations(
            id, source_object_id, sync_run_id, fetched_at, payload_hash, title,
            body_text, data_json, completeness, adapter_version,
            normalization_version, redaction_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, 'fixture', '2', '1')
        """,
        (
            observation_id,
            object_id,
            run_id,
            completed_at,
            f"hash:{observation_id}",
            title,
            f"{title} body",
            completeness,
        ),
    )


def _insert_self_participation(connection: sqlite3.Connection, object_id: str) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO actors(
            id, source, source_instance, external_actor_id, display_name, is_self
        ) VALUES ('actor:self', 'jira', 'jira-main', 'fixture-self', 'Fixture Engineer', 1)
        """
    )
    observation_id = str(
        connection.execute(
            "SELECT id FROM observations WHERE source_object_id=? ORDER BY fetched_at DESC LIMIT 1",
            (object_id,),
        ).fetchone()[0]
    )
    connection.execute(
        """
        INSERT INTO participations(
            id, source_object_id, observation_id, actor_id, role, details_json
        ) VALUES ('part:self', ?, ?, 'actor:self', 'jira_assignee', '{}')
        """,
        (object_id, observation_id),
    )


def test_unversioned_remote_run_is_quarantined_across_all_consumer_surfaces(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    config = _config(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:unversioned",
            object_id="obj:legacy",
            observation_id="obs:legacy",
            status="complete",
            scope={},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Legacy whole-project evidence",
        )
        _insert_self_participation(connection, "obj:legacy")
        connection.commit()

        repository = EvidenceRepository(connection)
        assert repository.current_observations("sample_store") == []
        assert search_evidence(connection, "sample_store", "Legacy") == []
        with pytest.raises(NotFound):
            evidence_excerpt(connection, "obs:legacy", chars=1_200)
        assert rebuild_candidates("sample_store", repository) == 0

        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES (
                'candidate:legacy', 'sample_store', 'obj:legacy', 'fixture',
                'Legacy candidate', 'feature', '2026-08-27T10:00:00+00:00'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_members(
                candidate_id, source_object_id, membership_reason, context_only
            ) VALUES ('candidate:legacy', 'obj:legacy', 'confirmed_history', 0)
            """
        )
        connection.commit()

        summary = PacketBuilder(connection, config).contribution_summary("candidate:legacy")
        assert summary["members"] == []
        assert summary["unsupported_member_ids"] == ["obj:legacy"]

        tools = WorkTraceTools(config=config, database_path=database_path)
        assert tools.search_evidence(query="Legacy", app_id="sample_store")["results"] == []
        with pytest.raises(NotFound):
            tools.get_evidence_excerpt(evidence_id="obs:legacy")

        export_path = tmp_path / "export.json"
        assert export_app(connection, "sample_store", export_path) == 0
        assert "Legacy whole-project evidence" not in export_path.read_text(encoding="utf-8")

        status = source_status(connection, "sample_store")[0]
        assert status["selection_policy_version"] is None
        assert status["authoritative_current"] is False
        assert status["limitations"]
        packet_status = summary["source_status"]["jira"]["instances"][0]
        assert packet_status["complete"] is False
        assert packet_status["authoritative_current"] is False
        assert packet_status["limitations"]
    finally:
        connection.close()


@pytest.mark.parametrize("malformed_version", ["2x", 2.5, True])
def test_malformed_remote_policy_versions_are_not_authoritative(
    tmp_path: Path,
    malformed_version: object,
) -> None:
    connection, _ = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:malformed",
            object_id="obj:malformed",
            observation_id="obs:malformed",
            status="complete",
            scope={"selection_policy_version": malformed_version},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Malformed policy evidence",
        )
        connection.commit()

        assert EvidenceRepository(connection).current_observations("sample_store") == []
        status = source_status(connection, "sample_store")[0]
        assert status["selection_policy_version"] is None
        assert status["authoritative_current"] is False
    finally:
        connection.close()


def test_excerpt_never_promotes_failed_run_and_object_fallback_uses_current_authority(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    config = _config(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:complete",
            object_id="obj:shared",
            observation_id="obs:complete",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-26T10:00:00+00:00",
            title="Authoritative evidence",
        )
        _insert_remote_observation(
            connection,
            run_id="run:failed",
            object_id="obj:shared",
            observation_id="obs:failed",
            status="failed",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Failed-run evidence",
            completeness="partial",
        )
        connection.commit()

        tools = WorkTraceTools(config=config, database_path=database_path)
        with pytest.raises(NotFound):
            tools.get_evidence_excerpt(evidence_id="obs:failed")
        fallback = tools.get_evidence_excerpt(evidence_id="obj:shared")
        assert fallback["evidence_id"] == "obs:complete"
        assert fallback["text"] == "Authoritative evidence body"
        assert fallback["run_status"] == "complete"
        assert fallback["authoritative_current"] is True
    finally:
        connection.close()
