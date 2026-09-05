from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from worktrace.config import (
    AppConfig,
    IdentityConfig,
    ModuleRule,
    WorkTraceConfig,
)
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.queries import source_status as query_source_status
from worktrace.db.repository import EvidenceRepository
from worktrace.mcp_server.tools import WorkTraceTools
from worktrace.packets.builder import PacketBuilder

EXPECTED_PHASE4_IDS = (
    "identity.what",
    "identity.app_flow",
    "identity.when",
    "identity.origin",
    "identity.ownership",
    "problem.what",
    "problem.before",
    "problem.severity",
    "problem.affected",
    "problem.blocked",
    "problem.constraints",
    "problem.ambiguity",
    "action.implemented",
    "action.decisions",
    "action.technology",
    "action.reuse",
    "action.architecture",
    "action.coordination",
    "action.quality",
    "action.review",
    "result.change",
    "result.measurement",
    "result.scope",
    "result.errors_time",
    "result.business",
    "result.release",
    "result.current_use",
    "result.reuse",
    "result.feedback",
    "result.defensibility",
)


def _config(tmp_path: Path) -> WorkTraceConfig:
    app = AppConfig(
        id="sample_store",
        name="Sample Store",
        market="XX",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(101,),
        repo_paths=(tmp_path / "repo",),
        jira_key_patterns=(r"DEMO-[0-9]+",),
        production_environments=("production",),
        release_tag_patterns=(r"v[0-9]+.*",),
        ignored_paths=("vendor/**",),
        module_rules=(
            ModuleRule(pattern="src/checkout/**", module="Checkout"),
            ModuleRule(pattern="src/generated/**", module="Generated"),
        ),
    )
    return WorkTraceConfig(
        schema_version=1,
        data_directory=tmp_path,
        employment_from=date(2020, 1, 1),
        employment_to=date(2026, 12, 31),
        identity=IdentityConfig(
            display_name="Fixture Engineer",
            git_author_emails=(),
            git_author_names=("Fixture Engineer",),
            jira_account_id="fixture-self",
            gitlab_user_id=7,
            gitlab_username="fixture-engineer",
        ),
        apps=(app,),
        config_path=tmp_path / "config.toml",
    )


def _insert_run(
    connection: sqlite3.Connection,
    run_id: str,
    source: str,
    source_instance: str,
    *,
    status: str = "complete",
    completeness: str = "complete_for_scope",
    completed_at: str = "2026-08-26T12:00:00+00:00",
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
            json.dumps({"selection_policy_version": 2} if source in {"jira", "gitlab"} else {}),
            completeness,
        ),
    )


def _insert_object(
    connection: sqlite3.Connection,
    *,
    object_id: str,
    observation_id: str,
    run_id: str,
    source: str,
    source_instance: str,
    kind: str,
    external_id: str,
    title: str,
    data: dict[str, object],
    observed_at: str = "2026-08-25T23:59:59+00:00",
) -> None:
    connection.execute(
        """
        INSERT INTO source_objects(
            id, app_id, source, source_instance, kind, external_id,
            first_seen_run_id, last_seen_run_id
        ) VALUES (?, 'sample_store', ?, ?, ?, ?, ?, ?)
        """,
        (object_id, source, source_instance, kind, external_id, run_id, run_id),
    )
    connection.execute(
        """
        INSERT INTO observations(
            id, source_object_id, sync_run_id, source_updated_at, fetched_at,
            payload_hash, title, body_text, data_json, completeness,
            adapter_version, normalization_version, redaction_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'complete', 'fixture', '1', '1')
        """,
        (
            observation_id,
            object_id,
            run_id,
            observed_at,
            observed_at,
            f"hash-{observation_id}",
            title,
            "Synthetic and sanitized evidence.",
            json.dumps(data, sort_keys=True),
        ),
    )


def _packet_state(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, Path, WorkTraceConfig, str]:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    migrate(connection, database_path)
    config = _config(tmp_path)
    EvidenceRepository(connection).ensure_apps(config)

    _insert_run(
        connection,
        "run:git_old",
        "git",
        "fixture-repo",
        completed_at="2026-06-01T00:00:00+00:00",
    )
    _insert_run(connection, "run:gitlab", "gitlab", "101")
    _insert_run(connection, "run:jira_complete", "jira", "jira-cloud")
    _insert_run(
        connection,
        "run:jira_partial",
        "jira",
        "jira-cloud",
        status="failed",
        completeness="partial",
        completed_at="2026-08-27T00:00:00+00:00",
    )
    _insert_run(connection, "run:manual", "manual", "local")

    _insert_object(
        connection,
        object_id="obj:commit",
        observation_id="obs:commit",
        run_id="run:git_old",
        source="git",
        source_instance="fixture-repo",
        kind="git_commit",
        external_id="a" * 40,
        title="Checkout validation",
        data={
            "authored_at": "2026-08-25T23:59:59Z",
            "committed_at": "2026-08-25T23:59:59Z",
            "changed_paths": [
                "src/checkout/Validation.py",
                "src/generated/Bindings.py",
                "vendor/library.py",
                "unmapped/notes.py",
            ],
            "generated_paths": ["src/generated/Bindings.py"],
        },
    )
    _insert_object(
        connection,
        object_id="obj:mr",
        observation_id="obs:mr",
        run_id="run:gitlab",
        source="gitlab",
        source_instance="101",
        kind="merge_request",
        external_id="7",
        title="Merge checkout validation",
        data={"state": "merged", "merged_at": "2026-08-25T23:59:59Z"},
    )
    _insert_object(
        connection,
        object_id="obj:tag",
        observation_id="obs:tag",
        run_id="run:git_old",
        source="git",
        source_instance="fixture-repo",
        kind="git_tag",
        external_id="v1.0.0",
        title="Release v1.0.0",
        data={"tag": "v1.0.0"},
    )
    _insert_object(
        connection,
        object_id="obj:deployment",
        observation_id="obs:deployment",
        run_id="run:gitlab",
        source="gitlab",
        source_instance="101",
        kind="deployment",
        external_id="88",
        title="Production deployment",
        data={"status": "success", "environment": "production"},
    )
    _insert_object(
        connection,
        object_id="obj:jira",
        observation_id="obs:jira",
        run_id="run:jira_complete",
        source="jira",
        source_instance="jira-cloud",
        kind="jira_issue",
        external_id="DEMO-7",
        title="Validate checkout full name",
        data={"priority": "High", "created_at": "2026-08-25T23:59:59Z"},
    )
    _insert_object(
        connection,
        object_id="obj:metric",
        observation_id="obs:metric",
        run_id="run:manual",
        source="manual",
        source_instance="local",
        kind="production_metric",
        external_id="metric-1",
        title="Sanitized checkout metric",
        data={"kind": "measured_outcome", "comparison": "synthetic"},
    )

    connection.execute(
        """
        INSERT INTO actors(
            id, source, source_instance, external_actor_id, display_name, is_self,
            identity_policy_version
        ) VALUES ('actor:self', 'git', 'fixture-repo', 'fixture-self',
                  'Fixture Engineer', 1, 1)
        """
    )
    connection.execute(
        """
        INSERT INTO participations(
            id, source_object_id, observation_id, actor_id, role, effective_from
        ) VALUES ('participation:author', 'obj:commit', 'obs:commit', 'actor:self',
                  'author', '2026-08-25T23:59:59+00:00')
        """
    )
    connection.execute(
        """
        INSERT INTO "references"(
            id, app_id, from_object_id, to_object_id, relationship_type,
            extraction_method, supporting_observation_id
        ) VALUES ('reference:revert', 'sample_store', 'obj:mr', 'obj:commit',
                  'reverts', 'fixture', 'obs:mr')
        """
    )

    candidate_id = "candidate:phase4"
    connection.execute(
        """
        INSERT INTO candidate_groups(
            id, app_id, seed_object_id, generator_version, suggested_title,
            suggested_type, generated_at
        ) VALUES (?, 'sample_store', 'obj:commit', 'fixture',
                  'Checkout full-name validation', 'feature',
                  '2026-08-26T12:00:00+00:00')
        """,
        (candidate_id,),
    )
    for object_id in (
        "obj:commit",
        "obj:mr",
        "obj:tag",
        "obj:deployment",
        "obj:jira",
        "obj:metric",
    ):
        connection.execute(
            "INSERT INTO candidate_members(candidate_id, source_object_id, "
            "membership_reason) VALUES (?, ?, 'fixture')",
            (candidate_id, object_id),
        )
    for decision_id, claim, statement in (
        (
            "decision:released",
            "released_to_users",
            "A local attestation says the synthetic release reached users.",
        ),
        (
            "decision:enabled",
            "currently_enabled",
            "A local attestation says the synthetic feature remains enabled.",
        ),
    ):
        connection.execute(
            """
            INSERT INTO human_decisions(id, action, target_id, payload_json, created_at)
            VALUES (?, 'attest_claim', ?, ?, '2026-08-26T12:00:00+00:00')
            """,
            (
                decision_id,
                candidate_id,
                json.dumps({"claim": claim, "statement": statement}, sort_keys=True),
            ),
        )
    connection.commit()
    return connection, database_path, config, candidate_id


def _all_questions(packet: dict[str, object]) -> list[dict[str, object]]:
    sections = packet["sections"]
    assert isinstance(sections, dict)
    return [
        question
        for questions in sections.values()
        if isinstance(questions, list)
        for question in questions
        if isinstance(question, dict)
    ]


def test_phase4_packet_preserves_claim_authority_and_independent_release_rungs(
    tmp_path: Path,
) -> None:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    try:
        packet = PacketBuilder(connection, config).build_packet(candidate_id)
    finally:
        connection.close()

    questions = _all_questions(packet)
    assert packet["schema_version"] == 2
    assert tuple(packet) == (
        "schema_version",
        "contribution",
        "as_of",
        "source_status",
        "evidence_summary",
        "sections",
        "participation",
        "identity_policy",
        "release_ladder",
        "contradictions",
        "defensibility",
        "limitations",
    )
    identity_policy = packet["identity_policy"]
    assert isinstance(identity_policy, dict)
    assert identity_policy["valid"] is True
    assert identity_policy["version"] == 1
    assert identity_policy["warnings"] == []
    assert questions
    assert tuple(question["question_id"] for question in questions) == EXPECTED_PHASE4_IDS
    assert len({question["question_id"] for question in questions}) == 30
    assert "legacy_question_id_aliases" not in packet
    assert all(
        question["supporting_evidence_ids"]
        for question in questions
        if question["answer_draft"] is not None
    )
    assert all(
        question["answer_draft"] is None
        for question in questions
        if question["status"] in {"unknown", "unresolved"}
    )

    participation = packet["participation"]
    assert isinstance(participation, dict)
    assert participation["ownership_statement"] == {
        "status": "requires_human_confirmation",
        "statement": None,
        "supporting_evidence_ids": [],
    }

    ladder = packet["release_ladder"]
    assert isinstance(ladder, dict)
    assert tuple(ladder) == (
        "implemented",
        "merged",
        "release_associated",
        "deployed",
        "released_to_users",
        "currently_enabled",
        "measurably_successful",
    )
    assert ladder["implemented"]["status"] == "supported"
    assert ladder["merged"]["status"] == "supported"
    assert ladder["release_associated"]["status"] == "supported"
    assert ladder["deployed"]["status"] == "supported"
    assert ladder["released_to_users"]["status"] == "human_attested"
    assert ladder["currently_enabled"]["status"] == "human_attested"
    assert ladder["measurably_successful"]["status"] == "supported"
    assert len({id(ladder[name]) for name in ladder}) == 7


def test_packet_reports_partial_stale_contradictory_and_module_rule_state(
    tmp_path: Path,
) -> None:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    try:
        builder = PacketBuilder(connection, config)
        summary = builder.contribution_summary(candidate_id)
        packet = builder.build_packet(candidate_id)
    finally:
        connection.close()

    assert summary["modules"] == ["Checkout"]
    assert summary["module_evidence_ids"] == ["obs:commit"]
    source_status = summary["source_status"]
    assert source_status["git"]["stale"] is True
    assert source_status["jira"]["complete"] is False
    assert source_status["jira"]["instances"][0]["status"] == "failed"
    assert any(item["kind"] == "recorded_revert" for item in summary["contradictions"])
    result_change = next(
        question
        for question in packet["sections"]["result"]
        if question["question_id"] == "result.change"
    )
    assert result_change["status"] == "contradicted"
    assert result_change["contradicting_evidence_ids"]
    assert "result.change" not in packet["defensibility"]["well_supported_question_ids"]


def test_review_participation_supports_review_but_not_coordination(tmp_path: Path) -> None:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    connection.execute("DELETE FROM participations WHERE id='participation:author'")
    connection.execute(
        """
        INSERT INTO participations(
            id, source_object_id, observation_id, actor_id, role, effective_from
        ) VALUES ('participation:reviewer', 'obj:commit', 'obs:commit', 'actor:self',
                  'reviewer', '2026-08-25T23:59:59+00:00')
        """
    )
    connection.commit()
    try:
        packet = PacketBuilder(connection, config).build_packet(candidate_id)
    finally:
        connection.close()

    questions = {item["question_id"]: item for item in _all_questions(packet)}
    assert questions["action.review"]["status"] == "supported"
    assert questions["action.review"]["supporting_evidence_ids"] == ["participation:reviewer"]
    assert questions["action.coordination"]["status"] == "unknown"
    assert questions["action.coordination"]["answer_draft"] is None
    assert questions["action.coordination"]["supporting_evidence_ids"] == []


def test_selection_biased_changed_paths_do_not_support_scope_claims(tmp_path: Path) -> None:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    connection.execute(
        """
        UPDATE observations
        SET completeness='selection_biased',
            data_json=?
        WHERE id='obs:commit'
        """,
        (
            json.dumps(
                {
                    "changed_paths": ["src/checkout/Validation.py"],
                    "overflow": True,
                    "scope_complete": False,
                    "limitations": ["Changed paths were truncated by the provider."],
                },
                sort_keys=True,
            ),
        ),
    )
    connection.commit()
    try:
        builder = PacketBuilder(connection, config)
        summary = builder.contribution_summary(candidate_id)
        packet = builder.build_packet(candidate_id)
    finally:
        connection.close()

    assert summary["modules"] == []
    assert any("changed-path" in item.casefold() for item in summary["limitations"])
    questions = {item["question_id"]: item for item in _all_questions(packet)}
    assert questions["problem.affected"]["status"] == "unknown"
    assert questions["action.implemented"]["status"] in {"unknown", "unresolved"}
    assert questions["action.implemented"]["answer_draft"] is None
    assert questions["action.implemented"]["supporting_evidence_ids"] == []
    assert questions["result.scope"]["status"] == "unknown"


def test_source_status_surfaces_bounded_selection_as_incomplete(tmp_path: Path) -> None:
    connection, _, config, _ = _packet_state(tmp_path)
    limitation = "GitLab hydration exceeded its configured bound."
    connection.execute(
        """
        UPDATE sync_runs
        SET completeness='selection_biased', progress_json=?
        WHERE id='run:gitlab'
        """,
        (
            json.dumps(
                {
                    "selection_biased": True,
                    "limitations": [limitation],
                    "selection_events": [
                        {
                            "kind": "gitlab_merge_request_hydration_cap",
                            "input_count": 501,
                            "selected_count": 500,
                            "dropped_count": 1,
                            "limit": 500,
                            "selection_policy": "updated_at_desc_then_iid_desc",
                        }
                    ],
                },
                sort_keys=True,
            ),
        ),
    )
    connection.commit()
    try:
        status = PacketBuilder(connection, config).source_status("sample_store")["gitlab"]
        query_status = next(
            item
            for item in query_source_status(connection, "sample_store")
            if item["source"] == "gitlab"
        )
    finally:
        connection.close()

    assert status["complete"] is False
    instance = status["instances"][0]
    assert instance["status"] == "complete"
    assert instance["completeness"] == "selection_biased"
    assert instance["complete"] is False
    assert instance["limitations"] == [limitation]
    assert instance["selection_events"][0]["dropped_count"] == 1
    assert query_status["complete"] is False
    assert query_status["limitations"] == [limitation]
    assert query_status["selection_events"][0]["dropped_count"] == 1


def test_date_to_is_inclusive_for_candidate_and_evidence_search(tmp_path: Path) -> None:
    connection, database_path, config, candidate_id = _packet_state(tmp_path)
    connection.close()
    tools = WorkTraceTools(config=config, database_path=database_path)

    candidates = tools.list_contribution_candidates(
        app_id="sample_store",
        date_from="2026-08-25",
        date_to="2026-08-25",
    )
    assert [item["candidate_id"] for item in candidates["candidates"]] == [candidate_id]

    evidence = tools.search_evidence(
        query="checkout",
        app_id="sample_store",
        date_from="2026-08-25",
        date_to="2026-08-25",
    )
    assert {item["evidence_id"] for item in evidence["results"]} >= {
        "obs:commit",
        "obs:jira",
    }
