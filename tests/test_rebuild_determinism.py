from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from worktrace.candidates.builder import rebuild_candidates
from worktrace.config import AppConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import (
    ActorObservation,
    JsonValue,
    NormalizedObject,
    ParticipationObservation,
    SourceIdentity,
)
from worktrace.linking.builder import rebuild_references


def _app() -> AppConfig:
    return AppConfig(
        id="sample_store",
        name="Sample Store",
        market="XX",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(101,),
        repo_paths=(),
        jira_key_patterns=(r"DEMO-[0-9]+",),
        production_environments=("production",),
        release_tag_patterns=("v*",),
        ignored_paths=(),
    )


def _object(
    *,
    source: str,
    source_instance: str,
    kind: str,
    external_id: str,
    title: str,
    body: str,
    data: dict[str, JsonValue],
    self_role: str | None = None,
) -> NormalizedObject:
    actors = ()
    participations = ()
    if self_role:
        actors = (
            ActorObservation(
                source=source,
                source_instance=source_instance,
                external_actor_id=f"fixture-self-{source}",
                display_name="Fixture Engineer",
                is_self=True,
            ),
        )
        participations = (ParticipationObservation(f"fixture-self-{source}", self_role),)
    return NormalizedObject(
        identity=SourceIdentity(source, source_instance, kind, external_id),
        app_id="sample_store",
        title=title,
        body_text=body,
        source_updated_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
        actors=actors,
        participations=participations,
        pending_references=(),
        data=data,
        completeness=Completeness.COMPLETE,
    )


def _inventory(connection: sqlite3.Connection) -> tuple[bytes, bytes]:
    references = [
        tuple(row)
        for row in connection.execute(
            "SELECT id, from_object_id, to_object_id, relationship_type, extraction_method, "
            'exact_value, supporting_observation_id, derived FROM "references" ORDER BY id'
        )
    ]
    candidates = [
        tuple(row)
        for row in connection.execute(
            """
            SELECT cg.id, cg.seed_object_id, cg.generator_version, cg.suggested_title,
                   cg.suggested_type, cg.status, cm.source_object_id,
                   cm.membership_reason, cm.context_only
            FROM candidate_groups cg
            JOIN candidate_members cm ON cm.candidate_id=cg.id
            ORDER BY cg.id, cm.source_object_id
            """
        )
    ]
    return (
        json.dumps(references, separators=(",", ":")).encode(),
        json.dumps(candidates, separators=(",", ":")).encode(),
    )


def test_repeated_rebuilds_produce_byte_stable_semantic_inventories(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) "
            "VALUES ('sample_store', 'Sample Store', '', '')"
        )
        connection.commit()
        repository = EvidenceRepository(connection)

        git_run = repository.start_sync_run(
            "sample_store", "git", "fixture-repository", {"mode": "fixture"}
        )
        repository.store_page(
            git_run,
            [
                _object(
                    source="git",
                    source_instance="fixture-repository",
                    kind="git_commit",
                    external_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    title="DEMO-101 checkout fix",
                    body="DEMO-101 synthetic commit",
                    data={"sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
                    self_role="git_author",
                )
            ],
        )
        repository.finish_sync_run(git_run, "complete", "complete_for_scope")

        jira_run = repository.start_sync_run(
            "sample_store", "jira", "https://fixture.example", {"mode": "fixture"}
        )
        repository.store_page(
            jira_run,
            [
                _object(
                    source="jira",
                    source_instance="https://fixture.example",
                    kind="jira_issue",
                    external_id="DEMO-101",
                    title="Checkout validation defect",
                    body="Synthetic issue",
                    data={"key": "DEMO-101"},
                )
            ],
        )
        repository.finish_sync_run(jira_run, "complete", "complete_for_scope")

        app = _app()
        assert rebuild_references(app, repository) == 1
        assert rebuild_candidates(app.id, repository) == 2
        first = _inventory(connection)

        assert rebuild_references(app, repository) == 1
        assert rebuild_candidates(app.id, repository) == 2
        second = _inventory(connection)

        assert second == first
    finally:
        connection.close()
