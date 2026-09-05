from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from worktrace.candidates.builder import rebuild_candidates
from worktrace.candidates.decisions import append_decision, decision_stream, undo_decision
from worktrace.candidates.projector import CandidateView, project_candidate
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
from worktrace.errors import ScopeViolation
from worktrace.participation import (
    ParticipationCategory,
    canonical_role,
    categories_for_evidence,
    is_implementation_evidence,
)
from worktrace.services import export_app


def _object(external_id: str, title: str) -> NormalizedObject:
    actor = ActorObservation(
        source="git",
        source_instance="fixture-repository",
        external_actor_id="fixture-self",
        display_name="Fixture Engineer",
        is_self=True,
    )
    return NormalizedObject(
        identity=SourceIdentity(
            source="git",
            source_instance="fixture-repository",
            kind="git_commit",
            external_id=external_id,
        ),
        app_id="sample_store",
        title=title,
        body_text="Synthetic evidence",
        source_updated_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
        actors=(actor,),
        participations=(ParticipationObservation("fixture-self", "git_author"),),
        pending_references=(),
        data={"sha": external_id},
        completeness=Completeness.COMPLETE,
    )


def _candidate_state(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, EvidenceRepository, list[str], list[str]]:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    migrate(connection, database_path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) "
        "VALUES ('sample_store', 'Sample Store', '', '')"
    )
    connection.commit()
    repository = EvidenceRepository(connection)
    run_id = repository.start_sync_run(
        "sample_store", "git", "fixture-repository", {"mode": "fixture"}
    )
    repository.store_page(
        run_id,
        [
            _object("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Alpha fix"),
            _object("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "Beta fix"),
            _object("cccccccccccccccccccccccccccccccccccccccc", "Gamma fix"),
        ],
    )
    repository.finish_sync_run(run_id, "complete", "complete_for_scope")
    assert rebuild_candidates("sample_store", repository) == 3
    candidates = [
        str(row[0])
        for row in connection.execute("SELECT id FROM candidate_groups ORDER BY suggested_title")
    ]
    objects = [
        str(row[0])
        for row in connection.execute("SELECT id FROM source_objects ORDER BY external_id")
    ]
    return connection, repository, candidates, objects


def _member_ids(view: CandidateView) -> set[str]:
    return {str(member["source_object_id"]) for member in view.members}


def test_decisions_are_reversible_and_survive_candidate_rebuild(tmp_path: Path) -> None:
    connection, repository, candidates, objects = _candidate_state(tmp_path)
    primary, secondary = candidates[:2]
    alpha, beta = objects[:2]
    try:
        confirm_id = append_decision(connection, "confirm", primary)
        assert project_candidate(connection, primary).status == "confirmed"

        add_id = append_decision(connection, "add_member", primary, {"source_object_id": beta})
        assert _member_ids(project_candidate(connection, primary)) == {alpha, beta}
        undo_decision(connection, add_id)
        assert _member_ids(project_candidate(connection, primary)) == {alpha}

        remove_id = append_decision(
            connection, "remove_member", primary, {"source_object_id": alpha}
        )
        assert _member_ids(project_candidate(connection, primary)) == set()
        undo_decision(connection, remove_id)
        assert _member_ids(project_candidate(connection, primary)) == {alpha}

        merge_id = append_decision(connection, "merge", primary, {"candidate_ids": [secondary]})
        assert _member_ids(project_candidate(connection, primary)) == {alpha, beta}

        split_id = append_decision(
            connection, "split", primary, {"keep_source_object_ids": [alpha]}
        )
        assert _member_ids(project_candidate(connection, primary)) == {alpha}
        undo_decision(connection, split_id)
        assert _member_ids(project_candidate(connection, primary)) == {alpha, beta}
        undo_decision(connection, merge_id)
        assert _member_ids(project_candidate(connection, primary)) == {alpha}

        before = connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0]
        assert rebuild_candidates("sample_store", repository) == 3
        after = connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0]

        rebuilt = project_candidate(connection, primary)
        assert rebuilt.status == "confirmed"
        assert _member_ids(rebuilt) == {alpha}
        assert before == after == 9
        assert any(decision["id"] == confirm_id for decision in rebuilt.decisions)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("action", "payload"),
    (
        ("rename_contribution", {"title": "Reviewed title"}),
        ("confirm", {}),
        ("add_member", {"source_object_id": "__SECOND_OBJECT__"}),
    ),
)
def test_undo_of_undo_is_rejected_without_mutating_the_active_stream(
    tmp_path: Path,
    action: str,
    payload: dict[str, object],
) -> None:
    connection, _, candidates, objects = _candidate_state(tmp_path)
    candidate_id = candidates[0]
    resolved_payload = {
        key: (objects[1] if value == "__SECOND_OBJECT__" else value)
        for key, value in payload.items()
    }
    try:
        original_id = append_decision(connection, action, candidate_id, resolved_payload)
        undo_id = undo_decision(connection, original_id)
        before = connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0]

        with pytest.raises(ScopeViolation, match="undo decisions cannot themselves be undone"):
            undo_decision(connection, undo_id)
        with pytest.raises(ScopeViolation, match="undo decisions cannot themselves be undone"):
            append_decision(
                connection,
                "undo_decision",
                candidate_id,
                {"compensates": undo_id},
                undo_target_id=undo_id,
            )
        with pytest.raises(ScopeViolation, match="decision has already been undone"):
            undo_decision(connection, original_id)

        after = connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0]
        active_ids = {decision.id for decision in decision_stream(connection, active_only=True)}
        assert before == after == 2
        assert original_id not in active_ids
        assert undo_id not in active_ids

        export_path = tmp_path / f"{action}-undo-rejection.json"
        export_app(connection, "sample_store", export_path)
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert {row["id"] for row in exported["human_decisions"]} == {
            original_id,
            undo_id,
        }
    finally:
        connection.close()


def test_candidate_overflow_does_not_consume_discarded_tail_seeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, repository, _, objects = _candidate_state(tmp_path)
    try:
        fourth_run = repository.start_sync_run(
            "sample_store", "git", "fixture-repository", {"mode": "fixture"}
        )
        repository.store_page(
            fourth_run,
            [
                _object("a" * 40, "Alpha fix"),
                _object("b" * 40, "Beta fix"),
                _object("c" * 40, "Gamma fix"),
                _object("d" * 40, "Delta fix"),
            ],
        )
        repository.finish_sync_run(fourth_run, "complete", "complete_for_scope")
        objects = [
            str(row[0])
            for row in connection.execute("SELECT id FROM source_objects ORDER BY external_id")
        ]
        seed = objects[0]
        supporting_observation_id = next(
            str(row["id"])
            for row in repository.current_observations("sample_store")
            if str(row["source_object_id"]) == seed
        )
        for index, target in enumerate(objects[1:], start=1):
            connection.execute(
                """
                INSERT INTO "references"(
                    id, app_id, from_object_id, to_object_id, relationship_type,
                    extraction_method, supporting_observation_id, derived
                ) VALUES (?, 'sample_store', ?, ?, 'mr_contains_commit', 'fixture', ?, 1)
                """,
                (f"ref:overflow:{index}", seed, target, supporting_observation_id),
            )
        connection.commit()
        monkeypatch.setattr("worktrace.candidates.builder.MAX_MEMBERS", 2)

        rebuild_candidates("sample_store", repository)

        represented = {
            str(row[0])
            for row in connection.execute("SELECT source_object_id FROM candidate_members")
        }
        assert represented == set(objects)
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM candidate_groups WHERE status='needs_manual_narrowing'"
            ).fetchone()[0]
            >= 1
        )
    finally:
        connection.close()


def test_mr_author_with_complete_paths_and_other_authored_commit_is_not_implementation(
    tmp_path: Path,
) -> None:
    connection, repository, _, _ = _candidate_state(tmp_path)
    run_id = "run:gitlab:biased-paths"
    mr_id = "obj:gitlab:mr:7"
    paths_id = "obj:gitlab:mr:7:paths"
    commit_id = "obj:gitlab:mr:7:commit:other"
    try:
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES (?, 'sample_store', 'gitlab', '101', 'complete', ?, ?, 'fixture',
                      '{"selection_policy_version":2}', 'selection_biased')
            """,
            (run_id, "2026-08-26T12:00:00+00:00", "2026-08-26T12:00:00+00:00"),
        )
        for object_id, kind, external_id in (
            (mr_id, "gitlab_mr", "101:7"),
            (paths_id, "gitlab_merge_request_changed_paths", "101:7:changed_paths"),
            (commit_id, "gitlab_merge_request_commit", "a" * 40),
        ):
            connection.execute(
                """
                INSERT INTO source_objects(
                    id, app_id, source, source_instance, kind, external_id,
                    first_seen_run_id, last_seen_run_id
                ) VALUES (?, 'sample_store', 'gitlab', '101', ?, ?, ?, ?)
                """,
                (object_id, kind, external_id, run_id, run_id),
            )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, data_json, completeness, adapter_version,
                normalization_version, redaction_version
            ) VALUES ('obs:gitlab:mr:7', ?, ?, ?, ?, 'hash-mr', 'Fixture MR', '{}',
                      'complete', 'fixture', '1', '1')
            """,
            (mr_id, run_id, "2026-08-26T12:00:00+00:00", "2026-08-26T12:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, data_json, completeness, adapter_version,
                normalization_version, redaction_version
            ) VALUES ('obs:gitlab:mr:7:paths', ?, ?, ?, ?, 'hash-paths', 'Fixture paths', ?,
                      'selection_biased', 'fixture', '1', '1')
            """,
            (
                paths_id,
                run_id,
                "2026-08-26T12:00:00+00:00",
                "2026-08-26T12:00:00+00:00",
                '{"changed_paths":[{"new_path":"src/checkout/payment.py"}],'
                '"overflow":false,"scope_complete":true}',
            ),
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, data_json, completeness, adapter_version,
                normalization_version, redaction_version
            ) VALUES ('obs:gitlab:mr:7:commit:other', ?, ?, ?, ?, 'hash-commit',
                      'Collaborator commit', '{"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                      'complete', 'fixture', '1', '1')
            """,
            (commit_id, run_id, "2026-08-26T12:00:00+00:00", "2026-08-26T12:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, is_self,
                identity_policy_version
            ) VALUES ('actor:gitlab:self', 'gitlab', '101', '7', 'Fixture Engineer', 1, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, is_self,
                identity_policy_version
            ) VALUES ('actor:gitlab:other', 'gitlab', '101', '8', 'Collaborator', 0, 1)
            """
        )
        connection.execute(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, effective_from
            ) VALUES ('participation:gitlab:mr:7:author', ?, 'obs:gitlab:mr:7',
                      'actor:gitlab:self', 'mr_author', ?)
            """,
            (mr_id, "2026-08-26T12:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, effective_from
            ) VALUES ('participation:gitlab:mr:7:commit:other', ?,
                      'obs:gitlab:mr:7:commit:other', 'actor:gitlab:other',
                      'gitlab_commit_author', ?)
            """,
            (commit_id, "2026-08-26T12:00:00+00:00"),
        )
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:gitlab:mr:7:paths', 'sample_store', ?, ?,
                      'gitlab_mr_changed_paths', 'fixture', 'obs:gitlab:mr:7:paths')
            """,
            (paths_id, mr_id),
        )
        connection.execute(
            """
            INSERT INTO "references"(
                id, app_id, from_object_id, to_object_id, relationship_type,
                extraction_method, supporting_observation_id
            ) VALUES ('ref:gitlab:mr:7:commit:other', 'sample_store', ?, ?,
                      'gitlab_mr_commit', 'fixture', 'obs:gitlab:mr:7:commit:other')
            """,
            (commit_id, mr_id),
        )
        connection.commit()

        rebuild_candidates("sample_store", repository)

        candidate = connection.execute(
            "SELECT id FROM candidate_groups WHERE seed_object_id=?", (mr_id,)
        ).fetchone()
        assert candidate is not None
        member_ids = {
            str(row[0])
            for row in connection.execute(
                "SELECT source_object_id FROM candidate_members WHERE candidate_id=?",
                (str(candidate["id"]),),
            )
        }
        assert {mr_id, paths_id, commit_id} <= member_ids
        assert categories_for_evidence(
            "gitlab",
            "gitlab_mr",
            "mr_author",
            {"changed_paths": [{"new_path": "Checkout/Payment.swift"}]},
        ) == frozenset({ParticipationCategory.CONTEXT})
        assert not is_implementation_evidence(
            "gitlab",
            "gitlab_mr",
            "mr_author",
            {"changed_paths": [{"new_path": "Checkout/Payment.swift"}]},
        )
    finally:
        connection.close()


def test_annotated_tag_author_is_release_context_not_implementation() -> None:
    assert canonical_role("git", "git_tag", "git_author") == "git_tag_author"
    assert categories_for_evidence("git", "git_tag", "author", {"tag_name": "v1.2.3"}) == frozenset(
        {ParticipationCategory.RELEASE_ASSOCIATED}
    )
    assert not is_implementation_evidence("git", "git_tag", "author", {"tag_name": "v1.2.3"})
