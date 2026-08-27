from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from worktrace.candidates.builder import rebuild_candidates
from worktrace.candidates.decisions import append_decision, undo_decision
from worktrace.candidates.projector import list_candidates, project_candidate
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.queries import evidence_excerpt, search_evidence, source_status
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import NotFound
from worktrace.mcp_server.tools import WorkTraceTools
from worktrace.packets.builder import PacketBuilder
from worktrace.services import add_manual_evidence, export_app


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
    source: str = "jira",
    kind: str = "jira_issue",
    data: dict[str, object] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO sync_runs(
            id, app_id, source, source_instance, status, started_at, completed_at,
            adapter_version, scope_json, completeness
        ) VALUES (?, 'sample_store', ?, ?, ?, ?, ?, 'fixture', ?, ?)
        """,
        (
            run_id,
            source,
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
        ) VALUES (?, 'sample_store', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET last_seen_run_id=excluded.last_seen_run_id
        """,
        (object_id, source, source_instance, kind, object_id, run_id, run_id),
    )
    connection.execute(
        """
        INSERT INTO observations(
            id, source_object_id, sync_run_id, fetched_at, payload_hash, title,
            body_text, data_json, completeness, adapter_version,
            normalization_version, redaction_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'fixture', '2', '1')
        """,
        (
            observation_id,
            object_id,
            run_id,
            completed_at,
            f"hash:{observation_id}",
            title,
            f"{title} body",
            json.dumps(data or {}),
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

        with pytest.raises(NotFound):
            PacketBuilder(connection, config).contribution_summary("candidate:legacy")

        tools = WorkTraceTools(config=config, database_path=database_path)
        assert tools.search_evidence(query="Legacy", app_id="sample_store")["results"] == []
        assert tools.list_contribution_candidates(app_id="sample_store")["candidates"] == []
        with pytest.raises(NotFound):
            tools.get_evidence_excerpt(evidence_id="obs:legacy")

        export_path = tmp_path / "export.json"
        assert export_app(connection, "sample_store", export_path) == 0
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert "Legacy whole-project evidence" not in export_path.read_text(encoding="utf-8")
        assert exported["candidate_groups"] == []
        assert exported["candidate_members"] == []

        append_decision(
            connection,
            "confirm_candidate",
            "candidate:legacy",
            {
                "contribution_id": "contribution:legacy-history",
                "app_id": "sample_store",
                "title": "Human-confirmed historical contribution",
                "members": ["obj:legacy"],
            },
        )
        confirmed = tools.get_contribution_summary(contribution_id="contribution:legacy-history")
        assert confirmed["members"] == []
        assert confirmed["unsupported_member_ids"] == ["obj:legacy"]
        assert any(
            "no authoritative current observation" in item for item in confirmed["limitations"]
        )

        status = source_status(connection, "sample_store")[0]
        assert status["selection_policy_version"] is None
        assert status["authoritative_current"] is False
        assert status["limitations"]
        packet_status = PacketBuilder(connection, config).source_status("sample_store")["jira"][
            "instances"
        ][0]
        assert packet_status["complete"] is False
        assert packet_status["authoritative_current"] is False
        assert packet_status["limitations"]
    finally:
        connection.close()


def test_complete_but_partial_v2_run_is_quarantined_across_surfaces(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    config = _config(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:v2-control",
            object_id="obj:v2-control",
            observation_id="obs:v2-control",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-26T10:00:00+00:00",
            title="Eligible v2 evidence",
        )
        _insert_remote_observation(
            connection,
            run_id="run:v2-partial",
            object_id="obj:v2-partial",
            observation_id="obs:v2-partial",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Partial v2 evidence",
            completeness="partial",
        )
        _insert_self_participation(connection, "obj:v2-partial")
        connection.commit()

        manual_observation_id = add_manual_evidence(
            EvidenceRepository(connection),
            config.app("sample_store"),
            title="Eligible manual evidence",
            body="Human-supplied control",
            evidence_type="fixture_control",
        )
        repository = EvidenceRepository(connection)
        current_ids = {str(row["id"]) for row in repository.current_observations("sample_store")}
        assert current_ids == {"obs:v2-control", manual_observation_id}
        assert search_evidence(connection, "sample_store", "Partial v2") == []
        assert [
            item["evidence_id"] for item in search_evidence(connection, "sample_store", "Eligible")
        ] == [manual_observation_id, "obs:v2-control"]
        with pytest.raises(NotFound):
            evidence_excerpt(connection, "obs:v2-partial", chars=1_200)
        assert (
            evidence_excerpt(connection, "obs:v2-control", chars=1_200)["authoritative_current"]
            is True
        )

        assert rebuild_candidates("sample_store", repository) == 1
        assert (
            connection.execute(
                "SELECT 1 FROM candidate_groups WHERE seed_object_id='obj:v2-partial'"
            ).fetchone()
            is None
        )

        tools = WorkTraceTools(config=config, database_path=database_path)
        with pytest.raises(NotFound):
            tools.get_evidence_excerpt(evidence_id="obs:v2-partial")
        assert (
            tools.get_evidence_excerpt(evidence_id=manual_observation_id)["authoritative_current"]
            is True
        )

        export_path = tmp_path / "partial-export.json"
        export_app(connection, "sample_store", export_path)
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert {row["id"] for row in exported["observations"]} == {
            "obs:v2-control",
            manual_observation_id,
        }
        assert exported["participations"] == []

        latest = source_status(connection, "sample_store")[0]
        assert latest["completeness"] == "partial"
        assert latest["authoritative_current"] is False
        assert latest["complete"] is False
        assert latest["limitations"]
    finally:
        connection.close()


def test_ineligible_availability_event_cannot_poison_projection_or_packet(
    tmp_path: Path,
) -> None:
    connection, _ = _ledger(tmp_path)
    config = _config(tmp_path)
    repository = EvidenceRepository(connection)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:availability-v2",
            object_id="obj:availability",
            observation_id="obs:availability-v2",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-26T10:00:00+00:00",
            title="Available v2 evidence",
        )
        connection.execute(
            """
            INSERT INTO source_object_availability_events(
                id, source_object_id, sync_run_id, state, reason, observed_at
            ) VALUES ('availability:v2', 'obj:availability', 'run:availability-v2',
                      'visible', 'observed', '2026-08-26T10:00:00+00:00')
            """
        )
        connection.execute(
            """
            UPDATE source_objects SET availability='visible', availability_reason='observed',
                availability_observed_at='2026-08-26T10:00:00+00:00'
            WHERE id='obj:availability'
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:availability', 'sample_store', 'obj:availability',
                      'fixture', 'Availability candidate', 'feature',
                      '2026-08-26T10:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_members(
                candidate_id, source_object_id, membership_reason, context_only
            ) VALUES ('candidate:availability', 'obj:availability', 'seed', 0)
            """
        )
        connection.commit()

        legacy_run = repository.start_sync_run("sample_store", "jira", "jira-main", {})
        repository.record_object_unavailable(
            legacy_run,
            source="jira",
            source_instance="jira-main",
            kind="jira_issue",
            external_id="obj:availability",
        )
        repository.finish_sync_run(legacy_run, "complete", "complete_for_scope")

        projected = connection.execute(
            "SELECT availability, availability_reason FROM source_objects "
            "WHERE id='obj:availability'"
        ).fetchone()
        assert tuple(projected) == ("visible", "observed")
        summary = PacketBuilder(connection, config).contribution_summary("candidate:availability")
        assert summary["contradictions"] == []
        assert [member["evidence_id"] for member in summary["members"]] == ["obs:availability-v2"]

        eligible_run = repository.start_sync_run(
            "sample_store",
            "jira",
            "jira-main",
            {"selection_policy_version": 2},
        )
        repository.record_object_unavailable(
            eligible_run,
            source="jira",
            source_instance="jira-main",
            kind="jira_issue",
            external_id="obj:availability",
        )
        repository.finish_sync_run(eligible_run, "complete", "complete_for_scope")
        projected = connection.execute(
            "SELECT availability, availability_reason FROM source_objects "
            "WHERE id='obj:availability'"
        ).fetchone()
        assert tuple(projected) == ("unavailable", "not_found")
    finally:
        connection.close()


def test_superseded_same_run_observation_and_participation_are_not_citable(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    config = _config(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:same-run",
            object_id="obj:same-run",
            observation_id="obs:same-run-old",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Superseded same-run evidence",
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, fetched_at, payload_hash, title,
                body_text, data_json, completeness, adapter_version,
                normalization_version, redaction_version
            ) VALUES ('obs:same-run-new', 'obj:same-run', 'run:same-run',
                      '2026-08-27T10:01:00+00:00', 'hash:same-run-new',
                      'Current same-run evidence', 'Current same-run body', '{}',
                      'complete_for_scope', 'fixture', '2', '1')
            """
        )
        connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, is_self
            ) VALUES ('actor:same-run', 'jira', 'jira-main', 'fixture-self',
                      'Fixture Engineer', 1)
            """
        )
        for participation_id, observation_id in (
            ("part:same-run-old", "obs:same-run-old"),
            ("part:same-run-new", "obs:same-run-new"),
        ):
            connection.execute(
                """
                INSERT INTO participations(
                    id, source_object_id, observation_id, actor_id, role, details_json
                ) VALUES (?, 'obj:same-run', ?, 'actor:same-run', 'jira_assignee', '{}')
                """,
                (participation_id, observation_id),
            )
        connection.commit()

        repository = EvidenceRepository(connection)
        assert [str(row["id"]) for row in repository.current_observations("sample_store")] == [
            "obs:same-run-new"
        ]
        with pytest.raises(NotFound):
            evidence_excerpt(connection, "obs:same-run-old", chars=1_200)
        assert (
            evidence_excerpt(connection, "obs:same-run-new", chars=1_200)["evidence_id"]
            == "obs:same-run-new"
        )

        tools = WorkTraceTools(config=config, database_path=database_path)
        for stale_id in ("obs:same-run-old", "part:same-run-old"):
            with pytest.raises(NotFound):
                tools.get_evidence_excerpt(evidence_id=stale_id)
        assert tools.get_evidence_excerpt(evidence_id="obs:same-run-new")["evidence_id"] == (
            "obs:same-run-new"
        )
        assert tools.get_evidence_excerpt(evidence_id="part:same-run-new")["evidence_id"] == (
            "part:same-run-new"
        )
        assert tools.get_evidence_excerpt(evidence_id="obj:same-run")["evidence_id"] == (
            "obs:same-run-new"
        )

        export_path = tmp_path / "same-run-export.json"
        export_app(connection, "sample_store", export_path)
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert [row["id"] for row in exported["observations"]] == ["obs:same-run-new"]
        assert [row["id"] for row in exported["participations"]] == ["part:same-run-new"]
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


@pytest.mark.parametrize(
    ("run_source", "run_instance", "object_source", "object_instance"),
    [
        ("git", "fixture-repo", "jira", "jira-main"),
        ("manual", "local-user", "gitlab", "gitlab-main"),
        ("git", "fixture-repo", "git", "other-repo"),
    ],
)
def test_corrupt_source_run_tuple_cannot_borrow_authority(
    tmp_path: Path,
    run_source: str,
    run_instance: str,
    object_source: str,
    object_instance: str,
) -> None:
    connection, database_path = _ledger(tmp_path)
    try:
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES ('run:borrowed', 'sample_store', ?, ?, 'complete',
                      '2026-08-27T10:00:00+00:00', '2026-08-27T10:00:00+00:00',
                      'fixture', '{}', 'complete_for_scope')
            """,
            (run_source, run_instance),
        )
        connection.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id, availability,
                availability_reason, availability_observed_at
            ) VALUES ('obj:borrowed', 'sample_store', ?, ?, 'jira_issue',
                      'DEMO-999', 'run:borrowed', 'run:borrowed', 'visible',
                      'observed', '2026-08-27T10:00:00+00:00')
            """,
            (object_source, object_instance),
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, fetched_at, payload_hash, title,
                body_text, data_json, completeness, adapter_version,
                normalization_version, redaction_version
            ) VALUES ('obs:borrowed', 'obj:borrowed', 'run:borrowed',
                      '2026-08-27T10:00:00+00:00', 'hash:borrowed',
                      'BORROWED AUTHORITY TITLE', 'BORROWED AUTHORITY BODY', '{}',
                      'complete_for_scope', 'fixture', '2', '1')
            """
        )
        connection.execute(
            """
            INSERT INTO source_object_availability_events(
                id, source_object_id, sync_run_id, state, reason, observed_at
            ) VALUES ('availability:borrowed', 'obj:borrowed', 'run:borrowed',
                      'visible', 'observed', '2026-08-27T10:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:borrowed', 'sample_store', 'obj:borrowed',
                      'fixture', 'BORROWED CANDIDATE TITLE', 'feature',
                      '2026-08-27T10:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO candidate_members VALUES ('candidate:borrowed', 'obj:borrowed', 'seed', 0)"
        )
        connection.commit()

        repository = EvidenceRepository(connection)
        assert repository.current_observations("sample_store") == []
        assert search_evidence(connection, "sample_store", "BORROWED") == []
        for evidence_id in ("obs:borrowed", "availability:borrowed"):
            with pytest.raises(NotFound):
                evidence_excerpt(connection, evidence_id, chars=1_200)

        tools = WorkTraceTools(config=_config(tmp_path), database_path=database_path)
        assert tools.search_evidence(query="BORROWED", app_id="sample_store")["results"] == []
        assert tools.list_contribution_candidates(app_id="sample_store")["candidates"] == []
        for evidence_id in ("obs:borrowed", "availability:borrowed"):
            with pytest.raises(NotFound):
                tools.get_evidence_excerpt(evidence_id=evidence_id)

        export_path = tmp_path / "borrowed-export.json"
        assert export_app(connection, "sample_store", export_path) == 0
        assert "BORROWED" not in export_path.read_text(encoding="utf-8")
        assert rebuild_candidates("sample_store", repository) == 0
    finally:
        connection.close()


def test_cross_object_participation_and_reference_support_are_never_authoritative(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:origin-a",
            object_id="obj:origin-a",
            observation_id="obs:origin-a",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Origin A unique evidence",
            source_instance="jira-a",
        )
        _insert_remote_observation(
            connection,
            run_id="run:origin-b",
            object_id="obj:origin-b",
            observation_id="obs:origin-b",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:01:00+00:00",
            title="Origin B unique evidence",
            source_instance="jira-b",
        )
        connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, is_self
            ) VALUES ('actor:wrong-source', 'git', 'fixture-repo', 'fixture-self',
                      'Wrong-source actor', 1),
                     ('actor:wrong-object', 'jira', 'jira-b', 'fixture-self',
                      'Wrong-object actor', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, details_json
            ) VALUES ('part:wrong-source', 'obj:origin-a', 'obs:origin-a',
                      'actor:wrong-source', 'jira_assignee', '{}'),
                     ('part:wrong-object', 'obj:origin-b', 'obs:origin-a',
                      'actor:wrong-object', 'jira_assignee', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:wrong-origin', 'sample_store', 'obj:origin-b',
                      'obj:origin-a', 'jira_subtask_of', 'fixture', 'obs:origin-a')
            """
        )
        connection.commit()

        repository = EvidenceRepository(connection)
        assert rebuild_candidates("sample_store", repository) == 0

        tools = WorkTraceTools(config=_config(tmp_path), database_path=database_path)
        assert (
            tools.search_evidence(
                query="Origin A unique", app_id="sample_store", actor_id="actor:wrong-object"
            )["results"]
            == []
        )
        for evidence_id in ("part:wrong-source", "part:wrong-object", "ref:wrong-origin"):
            with pytest.raises(NotFound):
                tools.get_evidence_excerpt(evidence_id=evidence_id)

        export_path = tmp_path / "origin-integrity-export.json"
        export_app(connection, "sample_store", export_path)
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert exported["participations"] == []
        assert exported["actors"] == []
        assert exported["references"] == []
    finally:
        connection.close()


def test_mixed_authority_candidate_reselects_canonical_seed_across_surfaces(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:legacy-seed",
            object_id="obj:legacy-seed",
            observation_id="obs:legacy-seed",
            status="complete",
            scope={},
            completed_at="2026-08-26T10:00:00+00:00",
            title="QUARANTINED LEGACY TITLE",
        )
        _insert_remote_observation(
            connection,
            run_id="run:current-member",
            object_id="obj:current-member",
            observation_id="obs:current-member",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Current authoritative member",
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:mixed-authority', 'sample_store', 'obj:legacy-seed',
                      'fixture', 'QUARANTINED LEGACY TITLE', 'hotfix',
                      '2026-08-27T11:00:00+00:00')
            """
        )
        for object_id, reason in (
            ("obj:legacy-seed", "seed"),
            ("obj:current-member", "structural_reference"),
        ):
            connection.execute(
                "INSERT INTO candidate_members VALUES ('candidate:mixed-authority', ?, ?, 0)",
                (object_id, reason),
            )
        connection.commit()

        projected = project_candidate(connection, "candidate:mixed-authority")
        assert projected.seed_object_id == "obj:current-member"
        assert projected.title == "Current authoritative member"
        assert projected.contribution_type == "feature"
        assert [member["source_object_id"] for member in projected.members] == [
            "obj:current-member"
        ]
        assert projected.unsupported_member_ids == ()
        assert list_candidates(connection, "sample_store")[0] == projected

        tools = WorkTraceTools(config=_config(tmp_path), database_path=database_path)
        listed = tools.list_contribution_candidates(app_id="sample_store")["candidates"]
        assert [item["title"] for item in listed] == ["Current authoritative member"]
        summary = tools.get_contribution_summary(contribution_id="candidate:mixed-authority")
        assert summary["contribution"]["title"] == "Current authoritative member"
        assert [member["object_id"] for member in summary["members"]] == ["obj:current-member"]

        export_path = tmp_path / "mixed-authority-export.json"
        export_app(connection, "sample_store", export_path)
        exported_text = export_path.read_text(encoding="utf-8")
        exported = json.loads(exported_text)
        assert "QUARANTINED LEGACY TITLE" not in exported_text
        assert exported["candidate_groups"][0]["seed_object_id"] == "obj:current-member"
        assert exported["candidate_groups"][0]["suggested_title"] == (
            "Current authoritative member"
        )
        assert [row["source_object_id"] for row in exported["candidate_members"]] == [
            "obj:current-member"
        ]
    finally:
        connection.close()


def test_export_preserves_app_scoped_decision_closure_and_unsupported_history(
    tmp_path: Path,
) -> None:
    connection, _ = _ledger(tmp_path)
    try:
        for suffix, instance in (("one", "jira-one"), ("two", "jira-two")):
            _insert_remote_observation(
                connection,
                run_id=f"run:eligible-{suffix}",
                object_id=f"obj:eligible-{suffix}",
                observation_id=f"obs:eligible-{suffix}",
                status="complete",
                scope={"selection_policy_version": 2},
                completed_at=f"2026-08-27T10:0{1 if suffix == 'one' else 2}:00+00:00",
                title=f"Eligible {suffix}",
                source_instance=instance,
            )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:eligible-export', 'sample_store', 'obj:eligible-one',
                      'fixture', 'Eligible one', 'feature',
                      '2026-08-27T11:00:00+00:00')
            """
        )
        for object_id in ("obj:eligible-one", "obj:eligible-two"):
            connection.execute(
                "INSERT INTO candidate_members VALUES "
                "('candidate:eligible-export', ?, 'fixture', 0)",
                (object_id,),
            )

        _insert_remote_observation(
            connection,
            run_id="run:legacy-history",
            object_id="obj:legacy-history",
            observation_id="obs:legacy-history",
            status="complete",
            scope={},
            completed_at="2026-08-26T09:00:00+00:00",
            title="QUARANTINED PROVIDER HISTORY",
            source_instance="jira-legacy-history",
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:legacy-history', 'sample_store', 'obj:legacy-history',
                      'fixture', 'QUARANTINED PROVIDER HISTORY', 'feature',
                      '2026-08-26T10:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO candidate_members VALUES "
            "('candidate:legacy-history', 'obj:legacy-history', 'seed', 0)"
        )

        connection.execute("INSERT INTO apps VALUES ('other_app', 'Other App', 'YY', 'fixture')")
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES ('run:other', 'other_app', 'manual', 'local-user', 'complete',
                      '2026-08-27T10:00:00+00:00', '2026-08-27T10:00:00+00:00',
                      'fixture', '{}', 'complete_for_scope')
            """
        )
        connection.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES ('obj:other', 'other_app', 'manual', 'local-user',
                      'manual_evidence', 'other', 'run:other', 'run:other')
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:other', 'other_app', 'obj:other', 'fixture',
                      'PRIVATE OTHER APP TITLE', 'unknown',
                      '2026-08-27T10:00:00+00:00')
            """
        )
        connection.commit()

        decision_ids: list[str] = []
        decision_ids.append(
            append_decision(
                connection,
                "confirm_candidate",
                "candidate:eligible-export",
                {
                    "contribution_id": "contribution:eligible-export",
                    "app_id": "sample_store",
                    "title": "Human eligible title",
                    "members": ["obj:eligible-one", "obj:eligible-two"],
                },
            )
        )
        decision_ids.append(
            append_decision(
                connection,
                "attest_claim",
                "contribution:eligible-export",
                {"claim": "result", "statement": "Human verified result"},
            )
        )
        decision_ids.append(
            append_decision(
                connection,
                "rename_contribution",
                "contribution:eligible-export",
                {"title": "Renamed eligible contribution"},
            )
        )
        removed = append_decision(
            connection,
            "remove_member",
            "contribution:eligible-export",
            {"source_object_id": "obj:eligible-two"},
        )
        decision_ids.append(removed)
        decision_ids.append(undo_decision(connection, removed))

        legacy_confirm = append_decision(
            connection,
            "confirm_candidate",
            "candidate:legacy-history",
            {
                "contribution_id": "contribution:legacy-history",
                "app_id": "sample_store",
                "title": "Human-confirmed historical contribution",
                "members": ["obj:legacy-history"],
            },
        )
        legacy_attest = append_decision(
            connection,
            "attest_claim",
            "contribution:legacy-history",
            {"claim": "context", "statement": "Human historical context"},
        )
        decision_ids.extend((legacy_confirm, legacy_attest))

        other_confirm = append_decision(
            connection,
            "confirm_candidate",
            "candidate:other",
            {
                "contribution_id": "contribution:other",
                "app_id": "other_app",
                "title": "PRIVATE OTHER APP TITLE",
                "members": ["obj:other"],
            },
        )
        other_attest = append_decision(
            connection,
            "attest_claim",
            "contribution:other",
            {"claim": "private", "statement": "PRIVATE OTHER APP ATTESTATION"},
        )

        # Simulate a deterministic rebuild changing the raw candidate after confirmation.
        # The immutable human confirmation snapshot must remain the projected history.
        connection.execute(
            "DELETE FROM candidate_members "
            "WHERE candidate_id='candidate:eligible-export' "
            "AND source_object_id='obj:eligible-two'"
        )
        connection.commit()

        export_path = tmp_path / "decision-closure-export.json"
        export_app(connection, "sample_store", export_path)
        exported_text = export_path.read_text(encoding="utf-8")
        exported = json.loads(exported_text)

        assert {row["id"] for row in exported["human_decisions"]} == set(decision_ids)
        assert other_confirm not in exported_text
        assert other_attest not in exported_text
        assert "PRIVATE OTHER APP" not in exported_text
        assert [row["id"] for row in exported["candidate_groups"]] == ["candidate:eligible-export"]
        assert {row["source_object_id"] for row in exported["candidate_members"]} == {
            "obj:eligible-one",
            "obj:eligible-two",
        }
        assert "QUARANTINED PROVIDER HISTORY" not in exported_text
        assert exported["unsupported_contribution_history"] == [
            {
                "app_id": "sample_store",
                "candidate_id": "candidate:legacy-history",
                "contribution_id": "contribution:legacy-history",
                "current_evidence_available": False,
                "decision_ids": [legacy_confirm, legacy_attest],
                "status": "confirmed_history_unsupported",
                "title": "Human-confirmed historical contribution",
                "unsupported_member_ids": ["obj:legacy-history"],
            }
        ]
    finally:
        connection.close()


def test_contradiction_citations_are_semantic_eligible_and_excerpt_resolvable(
    tmp_path: Path,
) -> None:
    connection, database_path = _ledger(tmp_path)
    config = _config(tmp_path)
    repository = EvidenceRepository(connection)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:closed-mr",
            object_id="obj:closed-mr",
            observation_id="obs:closed-mr",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T10:00:00+00:00",
            title="Closed without merge",
            source="gitlab",
            source_instance="gitlab-closed",
            kind="gitlab_mr",
            data={"state": "closed", "merged_at": None},
        )
        connection.execute(
            """
            INSERT INTO source_object_availability_events(
                id, source_object_id, sync_run_id, state, reason, observed_at
            ) VALUES ('availability:closed-visible', 'obj:closed-mr', 'run:closed-mr',
                      'visible', 'observed', '2026-08-27T10:00:00+00:00')
            """
        )
        connection.execute(
            """
            UPDATE source_objects SET availability='visible', availability_reason='observed',
                availability_observed_at='2026-08-27T10:00:00+00:00'
            WHERE id='obj:closed-mr'
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:closed-mr', 'sample_store', 'obj:closed-mr',
                      'fixture', 'Closed without merge', 'feature',
                      '2026-08-27T10:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO candidate_members VALUES "
            "('candidate:closed-mr', 'obj:closed-mr', 'seed', 0)"
        )

        _insert_remote_observation(
            connection,
            run_id="run:unavailable-base",
            object_id="obj:unavailable-mr",
            observation_id="obs:unavailable-base",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T09:00:00+00:00",
            title="Unavailable merge request",
            source="gitlab",
            source_instance="gitlab-unavailable",
            kind="gitlab_mr",
            data={"state": "opened"},
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:unavailable-mr', 'sample_store', 'obj:unavailable-mr',
                      'fixture', 'Unavailable merge request', 'feature',
                      '2026-08-27T09:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO candidate_members VALUES "
            "('candidate:unavailable-mr', 'obj:unavailable-mr', 'seed', 0)"
        )
        connection.commit()

        append_decision(
            connection,
            "confirm_candidate",
            "candidate:unavailable-mr",
            {
                "contribution_id": "contribution:unavailable-mr",
                "app_id": "sample_store",
                "title": "Human unavailable MR history",
                "members": ["obj:unavailable-mr"],
            },
        )
        unavailable_run = repository.start_sync_run(
            "sample_store",
            "gitlab",
            "gitlab-unavailable",
            {"selection_policy_version": 2},
        )
        unavailable_id = repository.record_object_unavailable(
            unavailable_run,
            source="gitlab",
            source_instance="gitlab-unavailable",
            kind="gitlab_mr",
            external_id="obj:unavailable-mr",
        )
        repository.finish_sync_run(unavailable_run, "complete", "complete_for_scope")

        builder = PacketBuilder(connection, config)
        closed = builder.contribution_summary("candidate:closed-mr")["contradictions"]
        assert closed == [
            {
                "kind": "closed_without_merge",
                "statement": "GitLab recorded a closed merge request, not a merged one.",
                "evidence_ids": ["obs:closed-mr"],
            }
        ]
        unavailable = builder.contribution_summary("contribution:unavailable-mr")["contradictions"]
        assert unavailable[0]["evidence_ids"] == [unavailable_id]

        tools = WorkTraceTools(config=config, database_path=database_path)
        closed_excerpt = tools.get_evidence_excerpt(evidence_id="obs:closed-mr")
        assert closed_excerpt["evidence_id"] == "obs:closed-mr"
        unavailable_excerpt = tools.get_evidence_excerpt(evidence_id=unavailable_id)
        assert unavailable_excerpt["content_type"] == "availability_evidence"
        assert unavailable_excerpt["state"] == "unavailable"
        assert unavailable_excerpt["reason"] == "not_found"
        assert unavailable_excerpt["authoritative_current"] is True
    finally:
        connection.close()


def test_cli_candidate_limit_is_applied_after_orphan_suppression(tmp_path: Path) -> None:
    connection, _ = _ledger(tmp_path)
    try:
        _insert_remote_observation(
            connection,
            run_id="run:valid-tail",
            object_id="obj:valid-tail",
            observation_id="obs:valid-tail",
            status="complete",
            scope={"selection_policy_version": 2},
            completed_at="2026-08-27T08:00:00+00:00",
            title="Valid tail candidate",
            source_instance="jira-valid-tail",
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:valid-tail', 'sample_store', 'obj:valid-tail',
                      'fixture', 'Valid tail candidate', 'feature',
                      '2026-08-27T08:00:00+00:00')
            """
        )
        connection.execute(
            "INSERT INTO candidate_members VALUES "
            "('candidate:valid-tail', 'obj:valid-tail', 'seed', 0)"
        )
        for index in range(25):
            _insert_remote_observation(
                connection,
                run_id=f"run:orphan-{index:02d}",
                object_id=f"obj:orphan-{index:02d}",
                observation_id=f"obs:orphan-{index:02d}",
                status="complete",
                scope={},
                completed_at=f"2026-08-27T09:{index:02d}:00+00:00",
                title=f"Orphan {index:02d}",
                source_instance=f"jira-orphan-{index:02d}",
            )
            connection.execute(
                """
                INSERT INTO candidate_groups(
                    id, app_id, seed_object_id, generator_version, suggested_title,
                    suggested_type, generated_at
                ) VALUES (?, 'sample_store', ?, 'fixture', ?, 'feature', ?)
                """,
                (
                    f"candidate:orphan-{index:02d}",
                    f"obj:orphan-{index:02d}",
                    f"Orphan {index:02d}",
                    f"2026-08-27T09:{index:02d}:00+00:00",
                ),
            )
            connection.execute(
                "INSERT INTO candidate_members VALUES (?, ?, 'seed', 0)",
                (f"candidate:orphan-{index:02d}", f"obj:orphan-{index:02d}"),
            )
        connection.commit()

        assert [candidate.id for candidate in list_candidates(connection, "sample_store")] == [
            "candidate:valid-tail"
        ]
    finally:
        connection.close()
