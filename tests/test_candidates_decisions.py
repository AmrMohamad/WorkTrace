from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from worktrace.candidates.builder import rebuild_candidates
from worktrace.candidates.decisions import append_decision, undo_decision
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
