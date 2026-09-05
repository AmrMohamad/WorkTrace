from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tests.test_mapped_references import _app, _object, _worktrace_config
from tests.test_mcp_security import _mcp_state
from worktrace.candidates.builder import GENERATOR_VERSION
from worktrace.candidates.decisions import append_decision, undo_decision
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository, source_instance_id
from worktrace.domain.enums import Completeness
from worktrace.domain.models import NormalizedObject, SourceIdentity
from worktrace.errors import ScopeViolation
from worktrace.mcp_server.tools import WorkTraceTools
from worktrace.packets.builder import PacketBuilder
from worktrace.read_models.evidence_context import (
    _decision_mentions_object,
    context_readiness,
    describe_object,
    scan_memberships,
    scan_relations,
)


def _jira_context_tools(
    tmp_path: Path,
) -> tuple[WorkTraceTools, dict[str, str], Path]:
    database = tmp_path / "jira-context.sqlite3"
    app = AppConfig(
        id="sample_jira",
        name="Sample Jira",
        market="",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(),
        repo_paths=(),
        jira_key_patterns=(),
        production_environments=(),
        release_tag_patterns=(),
        ignored_paths=(),
    )
    config = WorkTraceConfig(
        schema_version=1,
        data_directory=tmp_path,
        employment_from=date(2024, 1, 1),
        employment_to=date(2026, 12, 31),
        identity=IdentityConfig("Fixture", (), (), None, None, None),
        apps=(app,),
        config_path=tmp_path / "jira.toml",
    )
    connection = connect(database)
    try:
        migrate(connection, database)
        repository = EvidenceRepository(connection)
        repository.ensure_apps(config)
        instance = "jira-fixture"
        run = repository.start_sync_run(
            app.id,
            "jira",
            instance,
            {"mode": "fixture", "selection_policy_version": 2},
        )

        def record(kind: str, external_id: str, data: dict[str, object]) -> NormalizedObject:
            return NormalizedObject(
                identity=SourceIdentity("jira", instance, kind, external_id),
                app_id=app.id,
                title=external_id,
                body_text=None,
                source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
                actors=(),
                participations=(),
                pending_references=(),
                data=data,  # type: ignore[arg-type]
                completeness=Completeness.COMPLETE,
            )

        repository.store_page(
            run,
            [
                record("jira_issue", "100", {"key": "DEMO-1"}),
                record(
                    "jira_issue",
                    "200",
                    {"key": "OTHER-1", "parent_key": "DEMO-1"},
                ),
                record("jira_issue", "DEMO-9", {}),
                record(
                    "issue_comment",
                    "100:out",
                    {"issue_id": "100", "issue_key": "DEMO-1"},
                ),
                record(
                    "issue_changelog",
                    "100:in",
                    {"issue_id": "100", "issue_key": "DEMO-1"},
                ),
                record(
                    "issue_comment",
                    "100:conflict",
                    {
                        "issue_id": "100",
                        "issue_key": "OTHER-1",
                        "parent_key": "DEMO-1",
                    },
                ),
                record(
                    "issue_changelog",
                    "100:fallback",
                    {"issue_id": "100", "parent_key": "DEMO-1"},
                ),
                record(
                    "issue_comment",
                    "999:missing",
                    {"issue_id": "999", "parent_key": "DEMO-1"},
                ),
            ],
        )
        repository.finish_sync_run(run, "complete", "complete_for_scope")
        objects = {
            str(row["external_id"]): str(row["id"])
            for row in connection.execute("SELECT id, external_id FROM source_objects")
        }
        observations = {
            str(row["source_object_id"]): str(row["id"])
            for row in connection.execute("SELECT id, source_object_id FROM observations")
        }
        references = (
            ("ref:jira-out", objects["100"], objects["100:out"]),
            ("ref:jira-in", objects["100:in"], objects["100"]),
            ("ref:jira-conflict-out", objects["100"], objects["100:conflict"]),
            ("ref:jira-conflict-in", objects["100:conflict"], objects["100"]),
            ("ref:jira-fallback", objects["100"], objects["100:fallback"]),
            ("ref:jira-missing", objects["100"], objects["999:missing"]),
        )
        connection.executemany(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id, derived
            ) VALUES (?, 'sample_jira', ?, ?, 'jira_comment_issue', 'fixture', ?, 1)
            """,
            [
                (reference_id, from_id, to_id, observations[from_id])
                for reference_id, from_id, to_id in references
            ],
        )
        connection.commit()
    finally:
        connection.close()
    return WorkTraceTools(config=config, database_path=database), objects, database


def test_context_requires_a_scoped_source_object_and_keeps_endpoint_availability_separate(
    tmp_path: Path,
) -> None:
    database, _, tools = _mcp_state(tmp_path)
    writer = connect(database)
    try:
        writer.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, exact_value, supporting_observation_id, derived
            ) VALUES (
                'ref:manual-context', 'sample_store', 'obj:manual_1', 'obj:manual_2',
                'mentions_jira_key', 'fixture', 'DEMO-1', 'obs:manual_1', 1
            )
            """
        )
        writer.commit()
    finally:
        writer.close()

    with tools._builder() as builder:
        described = describe_object(builder, "sample_store", "obj:manual_1")
        assert described["current_observation_id"] == "obs:manual_1"
        assert described["availability"]["state"] == "unknown"
        with pytest.raises(ScopeViolation, match="source-object"):
            describe_object(builder, "sample_store", "obs:manual_1")

        rows = list(scan_relations(builder, "sample_store", "obj:manual_1"))
        assert len(rows) == 1
        _, relation, has_more = rows[0]
        assert has_more is False
        assert relation is not None
        assert relation["direction"] == "outgoing"
        assert relation["relationship_interpretation"] == "textual_mention"
        assert relation["to_endpoint"] == {
            "object_id": "obj:manual_2",
            "current_observation_id": "obs:manual_2",
            "availability": {
                "state": "unknown",
                "current": False,
                "evidence_id": None,
                "reason": None,
                "observed_at": None,
            },
        }


def test_public_context_uses_jira_owning_identity_before_parent_metadata(
    tmp_path: Path,
) -> None:
    tools, objects, _ = _jira_context_tools(tmp_path)

    # A root issue must not borrow an in-scope parent key when its own key is
    # foreign, and the legacy textual external-key fallback remains bounded.
    with pytest.raises(ScopeViolation, match="outside current configured source scope"):
        tools.get_evidence_context(app_id="sample_jira", object_id=objects["200"])
    legacy = tools.get_evidence_context(app_id="sample_jira", object_id=objects["DEMO-9"])
    assert legacy["object"]["object_id"] == objects["DEMO-9"]

    context = tools.get_evidence_context(app_id="sample_jira", object_id=objects["100"])
    relation_ids = {item["reference_id"] for item in context["relations"]["items"]}
    # Explicit owning issue_key supports both directions.  A subresource can
    # fall back only through the exact same-source parent ID binding.
    assert {"ref:jira-out", "ref:jira-in", "ref:jira-fallback"} <= relation_ids
    # A foreign explicit issue_key and a missing parent binding both fail
    # closed even when source-controlled parent_key says DEMO-1.
    assert not {
        "ref:jira-conflict-out",
        "ref:jira-conflict-in",
        "ref:jira-missing",
    } & relation_ids


def test_confirmed_rowless_context_membership_survives_legacy_generated_state(
    tmp_path: Path,
) -> None:
    database, _, tools = _mcp_state(tmp_path)
    writer = connect(database)
    try:
        append_decision(
            writer,
            "confirm_candidate",
            "candidate:manual_1",
            {
                "app_id": "sample_store",
                "contribution_id": "contribution:manual-context",
                "members": ["obj:manual_2"],
                "context_members": ["obj:manual_1"],
                "title": "Human-approved context",
            },
        )
        writer.execute("DELETE FROM candidate_groups WHERE id='candidate:manual_1'")
        writer.commit()
    finally:
        writer.close()

    with tools._builder() as builder:
        generation, rows = scan_memberships(builder, "sample_store", "obj:manual_1")
        assert generation is not None
        scanned = list(rows)

    assert len(scanned) == 1
    _, membership, has_more = scanned[0]
    assert has_more is False
    assert membership is not None
    assert membership["contribution_id"] == "contribution:manual-context"
    assert membership["role"] == "context"
    assert membership["basis"] == "confirmed"
    assert membership["evidence_state"] == "authoritative_current"


def test_membership_locator_uses_accepted_fields_not_unrelated_decision_prose() -> None:
    object_id = "obj:manual_1"
    assert _decision_mentions_object(
        "confirm_candidate",
        {"members": [object_id], "context_members": []},
        object_id,
    )
    assert _decision_mentions_object("add_member", {"source_object_id": object_id}, object_id)
    assert not _decision_mentions_object(
        "attest_claim",
        {"statement": f"Please inspect {object_id}", "members": [object_id]},
        object_id,
    )


def test_mapped_reference_excerpt_requires_and_accepts_live_explicit_mapping(
    tmp_path: Path,
) -> None:
    repo_a, repo_b = (tmp_path / "repo-a").resolve(), (tmp_path / "repo-b").resolve()
    app = _app(repo_a, repo_b, mapped=True)
    config = _worktrace_config(app, tmp_path)
    sha = "a" * 40
    connection = connect(tmp_path / "ledger.sqlite3")
    try:
        migrate(connection, tmp_path / "ledger.sqlite3")
        repository = EvidenceRepository(connection)
        repository.ensure_apps(config)
        git_instance = source_instance_id(app.id, "git", repo_a)
        gitlab_instance = source_instance_id(app.id, "gitlab", 101)
        git_run = repository.start_sync_run(app.id, "git", git_instance, {"mode": "fixture"})
        repository.store_page(
            git_run,
            [_object("git", git_instance, "git_commit", sha)],
        )
        repository.finish_sync_run(git_run, "complete", "complete_for_scope")
        gitlab_run = repository.start_sync_run(
            app.id,
            "gitlab",
            gitlab_instance,
            {"mode": "fixture", "selection_policy_version": 2},
        )
        repository.store_page(
            gitlab_run,
            [
                _object(
                    "gitlab",
                    gitlab_instance,
                    "gitlab_mr",
                    "101:1",
                    data={"project_id": "101"},
                )
            ],
        )
        repository.finish_sync_run(gitlab_run, "complete", "complete_for_scope")
        source = connection.execute(
            "SELECT id FROM source_objects WHERE source='gitlab'"
        ).fetchone()
        target = connection.execute("SELECT id FROM source_objects WHERE source='git'").fetchone()
        observation = connection.execute(
            "SELECT id FROM observations WHERE source_object_id=?", (str(source["id"]),)
        ).fetchone()
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, exact_value, supporting_observation_id, derived
            ) VALUES (?, ?, ?, ?, 'mapped_commit_sha',
                      'explicit_repo_project_full_sha:commit_record', ?, ?, 1)
            """,
            ("ref:mapped", app.id, source["id"], target["id"], sha, observation["id"]),
        )
        connection.commit()

        builder = PacketBuilder(connection, config)
        excerpt = builder.evidence_excerpt("ref:mapped", 100)
        assert excerpt["content_type"] == "typed_reference_evidence"
        assert excerpt["relationship_type"] == "mapped_commit_sha"
        relation_rows = list(scan_relations(builder, app.id, str(source["id"])))
        assert relation_rows[0][1] is not None
        assert relation_rows[0][1]["relationship_interpretation"] == (
            "explicitly_mapped_sha_reference"
        )
    finally:
        connection.close()


def test_removed_rowless_member_returns_after_undo_without_replaying_decisions(
    tmp_path: Path,
) -> None:
    database, _, tools = _mcp_state(tmp_path)
    writer = connect(database)
    try:
        append_decision(
            writer,
            "confirm_candidate",
            "candidate:manual_1",
            {
                "app_id": "sample_store",
                "contribution_id": "contribution:rowless",
                "members": ["obj:manual_1", "obj:manual_2"],
            },
        )
        removed = append_decision(
            writer,
            "remove_member",
            "candidate:manual_1",
            {"source_object_id": "obj:manual_1"},
        )
        writer.execute("DELETE FROM candidate_groups WHERE id='candidate:manual_1'")
        writer.commit()

        with tools._builder() as builder:
            assert list(scan_memberships(builder, "sample_store", "obj:manual_1")[1]) == [
                ({"phase": "after", "key": "contribution:rowless"}, None, False)
            ]

        undo_decision(writer, removed)
        with tools._builder() as builder:
            restored = list(scan_memberships(builder, "sample_store", "obj:manual_1")[1])
        assert restored[0][1] is not None
        assert restored[0][1]["role"] == "material"
    finally:
        writer.close()


def test_merge_split_aliases_deduplicate_to_latest_confirmed_contribution(
    tmp_path: Path,
) -> None:
    database, _, tools = _mcp_state(tmp_path)
    writer = connect(database)
    try:
        # Two generated aliases both locate the queried object.  The canonical
        # decision lineage must collapse them before keyset continuation.
        writer.execute(
            "INSERT INTO candidate_members VALUES "
            "('candidate:manual_2', 'obj:manual_1', 'fixture', 0)"
        )
        append_decision(
            writer,
            "merge_contributions",
            "candidate:manual_1",
            {
                "app_id": "sample_store",
                "candidate_ids": ["candidate:manual_1", "candidate:manual_2"],
                "contribution_id": "contribution:merged",
                "members": ["obj:manual_1", "obj:manual_2"],
            },
        )
        append_decision(
            writer,
            "split_contribution",
            "candidate:manual_1",
            {
                "app_id": "sample_store",
                "candidate_ids": ["candidate:manual_1", "candidate:manual_2"],
                "contribution_id": "contribution:split",
                "members": ["obj:manual_1"],
                "context_members": [],
            },
        )
        writer.commit()
    finally:
        writer.close()

    with tools._builder() as builder:
        _, rows = scan_memberships(builder, "sample_store", "obj:manual_1")
        memberships = [item for _, item, _ in rows if item is not None]

    assert len(memberships) == 1
    assert memberships[0]["contribution_id"] == "contribution:split"
    assert memberships[0]["candidate_id"] == "candidate:manual_1"


def test_legacy_generated_memberships_are_suppressed_but_readiness_requires_rebuild(
    tmp_path: Path,
) -> None:
    _, _, tools = _mcp_state(tmp_path)
    with tools._builder() as builder:
        readiness = context_readiness(builder, "sample_store")
        _, rows = scan_memberships(builder, "sample_store", "obj:manual_1")
        scanned = list(rows)

    assert readiness["requires_rebuild"] is True
    assert readiness["limitations"]
    assert scanned == [({"phase": "after", "key": "candidate:manual_1"}, None, False)]


def test_relation_keeps_unavailable_target_but_rejects_noncurrent_declaring_authority(
    tmp_path: Path,
) -> None:
    database, _, tools = _mcp_state(tmp_path)
    writer = connect(database)
    try:
        writer.execute(
            "UPDATE source_objects SET availability='unavailable', availability_reason='fixture', "
            "availability_observed_at='2026-08-26T12:00:00+00:00' WHERE id='obj:manual_2'"
        )
        writer.execute(
            """
            INSERT INTO source_object_availability_events(
                id, source_object_id, sync_run_id, state, reason, observed_at
            ) VALUES ('availability:manual-2', 'obj:manual_2', 'run:manual_new',
                      'unavailable', 'fixture', '2026-08-26T12:00:00+00:00')
            """
        )
        writer.execute("DELETE FROM observations WHERE source_object_id='obj:manual_2'")
        writer.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id, derived
            ) VALUES ('ref:available-declarer', 'sample_store', 'obj:manual_1', 'obj:manual_2',
                      'mentions_jira_key', 'fixture', 'obs:manual_1', 1)
            """
        )
        writer.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES ('run:failed-declarer', 'sample_store', 'manual', 'local', 'failed',
                      '2026-08-27T12:00:00+00:00', '2026-08-27T12:00:00+00:00',
                      'fixture', '{}', 'unknown')
            """
        )
        writer.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, body_text, data_json, completeness,
                adapter_version, normalization_version, redaction_version
            ) VALUES ('obs:failed-declarer', 'obj:manual_0', 'run:failed-declarer',
                      '2026-08-27T12:00:00+00:00', '2026-08-27T12:00:00+00:00',
                      'failed-hash', 'Failed', NULL, '{}', 'unknown', 'fixture', '1', '1')
            """
        )
        writer.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id, derived
            ) VALUES ('ref:old-declarer', 'sample_store', 'obj:manual_0', 'obj:manual_1',
                      'mentions_jira_key', 'fixture', 'obs:failed-declarer', 1)
            """
        )
        writer.commit()
    finally:
        writer.close()

    with tools._builder() as builder:
        relations = list(scan_relations(builder, "sample_store", "obj:manual_1"))
    assert len(relations) == 1
    item = relations[0][1]
    assert item is not None
    assert item["reference_id"] == "ref:available-declarer"
    assert item["to_endpoint"]["current_observation_id"] is None
    assert item["to_endpoint"]["availability"]["state"] == "unavailable"
    assert item["to_endpoint"]["availability"]["evidence_id"] == "availability:manual-2"


def test_membership_locator_scan_is_lossless_across_more_than_two_hundred_groups(
    tmp_path: Path,
) -> None:
    database, _, tools = _mcp_state(tmp_path)
    writer = connect(database)
    try:
        generated_at = "2026-08-26T12:00:00+00:00"
        writer.execute(
            "UPDATE candidate_groups SET generator_version=?, generated_at=?",
            (GENERATOR_VERSION, generated_at),
        )
        writer.executemany(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES (?, 'sample_store', 'manual', 'local', 'manual_evidence', ?,
                      'run:manual_new', 'run:manual_new')
            """,
            [(f"obj:fanout-{index:03d}", f"fanout-{index:03d}") for index in range(205)],
        )
        writer.executemany(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES (?, 'sample_store', ?, ?, 'Fanout', 'unknown', ?)
            """,
            [
                (
                    f"candidate:fanout-{index:03d}",
                    f"obj:fanout-{index:03d}",
                    GENERATOR_VERSION,
                    generated_at,
                )
                for index in range(205)
            ],
        )
        writer.executemany(
            "INSERT INTO candidate_members VALUES (?, 'obj:manual_1', 'fixture', 0)",
            [(f"candidate:fanout-{index:03d}",) for index in range(205)],
        )
        writer.commit()
    finally:
        writer.close()

    seen: list[str] = []
    after: str | None = None
    while True:
        with tools._builder() as builder:
            _, rows = scan_memberships(builder, "sample_store", "obj:manual_1", after=after)
            page = list(rows)
        seen.extend(position["key"] for position, item, _ in page if item is not None)
        if not page or not page[-1][2]:
            break
        after = page[-1][0]["key"]

    assert len(seen) == 206
    assert len(seen) == len(set(seen))
