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
    source_instance: str = "jira-main",
) -> None:
    connection.execute(
        """
        INSERT INTO sync_runs(
            id, app_id, source, source_instance, status, started_at, completed_at,
            adapter_version, scope_json, completeness
        ) VALUES (?, 'sample_store', 'jira', ?, ?, ?, ?, 'fixture', ?, ?)
        """,
        (
            run_id,
            source_instance,
            status,
            completed_at,
            completed_at,
            json.dumps(scope),
            completeness,
        ),
    )
    connection.execute(
        """
        INSERT INTO source_objects(
            id, app_id, source, source_instance, kind, external_id,
            first_seen_run_id, last_seen_run_id
        ) VALUES (?, 'sample_store', 'jira', ?, 'jira_issue', ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET last_seen_run_id=excluded.last_seen_run_id
        """,
        (object_id, source_instance, object_id, run_id, run_id),
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


def test_legacy_participation_cannot_seed_an_authoritative_current_object(
    tmp_path: Path,
) -> None:
    connection, _ = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:legacy-role",
            object_id="obj:shared-role",
            observation_id="obs:legacy-role",
            status="complete",
            scope={},
            completed_at="2026-08-26T10:00:00+00:00",
            title="Legacy assigned issue",
        )
        _insert_self_participation(connection, "obj:shared-role")
        _insert_remote_observation(
            connection,
            run_id="run:current-role",
            object_id="obj:shared-role",
            observation_id="obs:current-role",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Current issue without self participation",
        )
        connection.commit()

        repository = EvidenceRepository(connection)
        current = repository.current_observations("sample_store")
        assert [str(row["id"]) for row in current] == ["obs:current-role"]
        assert rebuild_candidates("sample_store", repository) == 0
    finally:
        connection.close()


def test_candidate_references_require_current_support_and_accept_v2_control(
    tmp_path: Path,
) -> None:
    connection, _ = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:legacy-reference",
            object_id="obj:reference-child",
            observation_id="obs:legacy-reference",
            status="complete",
            scope={},
            completed_at="2026-08-25T10:00:00+00:00",
            title="Legacy child",
        )
        _insert_remote_observation(
            connection,
            run_id="run:current-reference-child",
            object_id="obj:reference-child",
            observation_id="obs:current-reference-child",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Current child",
        )
        _insert_remote_observation(
            connection,
            run_id="run:current-reference-parent",
            object_id="obj:reference-parent",
            observation_id="obs:current-reference-parent",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Current parent",
            source_instance="jira-secondary",
        )
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:legacy-grouping', 'sample_store', 'obj:reference-child',
                      'obj:reference-parent', 'jira_subtask_of', 'fixture',
                      'obs:legacy-reference')
            """
        )
        connection.commit()

        repository = EvidenceRepository(connection)
        assert rebuild_candidates("sample_store", repository) == 0
        legacy_export = tmp_path / "legacy-reference-export.json"
        export_app(connection, "sample_store", legacy_export)
        assert json.loads(legacy_export.read_text(encoding="utf-8"))["references"] == []

        connection.execute("DELETE FROM \"references\" WHERE id='ref:legacy-grouping'")
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:current-grouping', 'sample_store', 'obj:reference-child',
                      'obj:reference-parent', 'jira_subtask_of', 'fixture',
                      'obs:current-reference-child')
            """
        )
        connection.commit()

        assert rebuild_candidates("sample_store", repository) == 1
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT source_object_id FROM candidate_members WHERE context_only=0"
            )
        } == {"obj:reference-child", "obj:reference-parent"}
        current_export = tmp_path / "current-reference-export.json"
        export_app(connection, "sample_store", current_export)
        assert [
            row["id"]
            for row in json.loads(current_export.read_text(encoding="utf-8"))["references"]
        ] == ["ref:current-grouping"]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("status", "scope", "completeness"),
    [
        ("failed", {"selection_policy_version": 2}, "partial"),
        ("complete", {"selection_policy_version": 1}, "complete_for_scope"),
        ("complete", {}, "complete_for_scope"),
    ],
)
def test_non_authoritative_typed_reference_is_not_citable_through_mcp(
    tmp_path: Path,
    status: str,
    scope: dict[str, object],
    completeness: str,
) -> None:
    connection, database_path = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:reference-support",
            object_id="obj:reference-support",
            observation_id="obs:reference-support",
            status=status,
            scope=scope,
            completed_at="2026-08-27T10:00:00+00:00",
            title="Non-authoritative reference support",
            completeness=completeness,
        )
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:non-authoritative', 'sample_store', 'obj:reference-support',
                      'obj:reference-support', 'related_to', 'fixture',
                      'obs:reference-support')
            """
        )
        connection.commit()

        tools = WorkTraceTools(config=_config(tmp_path), database_path=database_path)
        with pytest.raises(NotFound):
            tools.get_evidence_excerpt(evidence_id="ref:non-authoritative")
    finally:
        connection.close()


def test_failed_reference_cannot_contradict_supported_claim_and_v2_control_can(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    config = _config(tmp_path)
    try:
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES ('run:git-current', 'sample_store', 'git', 'fixture-repo',
                      'complete', '2026-08-27T09:00:00+00:00',
                      '2026-08-27T09:00:00+00:00', 'fixture', '{}',
                      'complete_for_scope')
            """
        )
        connection.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES ('obj:implementation', 'sample_store', 'git', 'fixture-repo',
                      'git_commit', 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                      'run:git-current', 'run:git-current')
            """
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, fetched_at, payload_hash, title,
                body_text, data_json, completeness, adapter_version,
                normalization_version, redaction_version
            ) VALUES ('obs:implementation', 'obj:implementation', 'run:git-current',
                      '2026-08-27T09:00:00+00:00', 'hash:implementation',
                      'Implementation evidence', 'Synthetic implementation body',
                      '{"changed_paths":[{"path":"src/worktrace/example.py"}]}',
                      'complete', 'fixture', '2', '1')
            """
        )
        connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, is_self
            ) VALUES ('actor:git-self', 'git', 'fixture-repo', 'fixture-self',
                      'Fixture Engineer', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, details_json
            ) VALUES ('part:implementation', 'obj:implementation', 'obs:implementation',
                      'actor:git-self', 'git_author', '{}')
            """
        )
        _insert_remote_observation(
            connection,
            run_id="run:failed-revert",
            object_id="obj:failed-revert",
            observation_id="obs:failed-revert",
            status="failed",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Failed revert evidence",
            completeness="partial",
        )
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:failed-revert', 'sample_store', 'obj:failed-revert',
                      'obj:implementation', 'reverts', 'fixture', 'obs:failed-revert')
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:authority', 'sample_store', 'obj:implementation',
                      'fixture', 'Authority candidate', 'feature',
                      '2026-08-27T10:00:00+00:00')
            """
        )
        for object_id in ("obj:implementation", "obj:failed-revert"):
            connection.execute(
                """
                INSERT INTO candidate_members(
                    candidate_id, source_object_id, membership_reason, context_only
                ) VALUES ('candidate:authority', ?, 'fixture', 0)
                """,
                (object_id,),
            )
        connection.commit()

        builder = PacketBuilder(connection, config)
        packet = builder.build_packet("candidate:authority")
        implemented = next(
            question
            for question in packet["sections"]["action"]
            if question["question_id"] == "action.implemented"
        )
        assert packet["contradictions"] == []
        assert implemented["status"] == "supported"
        assert "action.implemented" in packet["defensibility"]["well_supported_question_ids"]

        tools = WorkTraceTools(config=config, database_path=database_path)
        with pytest.raises(NotFound):
            tools.get_evidence_excerpt(evidence_id="ref:failed-revert")

        connection.execute(
            """
            UPDATE sync_runs SET status='complete', completeness='complete_for_scope'
            WHERE id='run:failed-revert'
            """
        )
        connection.execute(
            """
            UPDATE observations SET completeness='complete_for_scope'
            WHERE id='obs:failed-revert'
            """
        )
        connection.commit()

        authoritative_packet = builder.build_packet("candidate:authority")
        authoritative_implemented = next(
            question
            for question in authoritative_packet["sections"]["action"]
            if question["question_id"] == "action.implemented"
        )
        assert authoritative_packet["contradictions"][0]["kind"] == "recorded_revert"
        assert authoritative_implemented["status"] == "contradicted"
        assert (
            "action.implemented"
            not in authoritative_packet["defensibility"]["well_supported_question_ids"]
        )
        excerpt = tools.get_evidence_excerpt(evidence_id="ref:failed-revert")
        assert excerpt["content_type"] == "typed_reference_evidence"
        assert excerpt["supporting_observation_id"] == "obs:failed-revert"
        assert excerpt["authoritative_current"] is True
    finally:
        connection.close()
