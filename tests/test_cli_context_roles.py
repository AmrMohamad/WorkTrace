from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from worktrace.candidates.builder import rebuild_candidates
from worktrace.cli import app
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import (
    ActorObservation,
    NormalizedObject,
    ParticipationObservation,
    SourceIdentity,
)
from worktrace.packets.builder import PacketBuilder


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""schema_version = 1

[data]
directory = {str(tmp_path / "data")!r}

[employment]
from = "2024-01-01"
to = "2026-08-26"

[identity]
display_name = "Fixture Engineer"
git_author_emails = ["fixture@example.test"]
git_author_names = ["Fixture Engineer"]

[[apps]]
id = "sample"
name = "Sample"
repo_paths = [{str(tmp_path / "repo")!r}]
gitlab_project_ids = []
jira_project_keys = []
""",
        encoding="utf-8",
    )
    return path


def _object(sha: str) -> NormalizedObject:
    actor = ActorObservation("git", "fixture-repository", "self", "Fixture", is_self=True)
    return NormalizedObject(
        identity=SourceIdentity("git", "fixture-repository", "git_commit", sha),
        app_id="sample",
        title=sha,
        body_text="fixture",
        source_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        actors=(actor,),
        participations=(ParticipationObservation("self", "git_author"),),
        pending_references=(),
        data={"sha": sha},
        completeness=Completeness.COMPLETE,
    )


def test_cli_merge_split_undo_and_rebuild_preserve_context_roles(tmp_path: Path) -> None:
    config_path = _config(tmp_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, initialized.output
    configuration = load_config(config_path)
    connection = connect(configuration.database_path)
    try:
        repository = EvidenceRepository(connection)
        run = repository.start_sync_run("sample", "git", "fixture-repository", {"mode": "fixture"})
        repository.store_page(run, [_object("a" * 40), _object("b" * 40), _object("c" * 40)])
        repository.finish_sync_run(run, "complete", "complete_for_scope")
        rebuild_candidates("sample", repository)
        candidates = {
            str(row["external_id"]): str(row["id"])
            for row in connection.execute(
                "SELECT candidate.id, object.external_id FROM candidate_groups candidate "
                "JOIN source_objects object ON object.id=candidate.seed_object_id"
            )
        }
        objects = {
            str(row["external_id"]): str(row["id"])
            for row in connection.execute("SELECT id, external_id FROM source_objects")
        }
        first, second, context_object = (
            candidates["a" * 40],
            candidates["b" * 40],
            objects["c" * 40],
        )
        material_first, material_second = objects["a" * 40], objects["b" * 40]
        # Add fixture context to the first generated suggestion without making it material.
        connection.execute(
            "INSERT OR REPLACE INTO candidate_members("
            "candidate_id, source_object_id, membership_reason, context_only) "
            "VALUES (?, ?, 'fixture_context', 1)",
            (first, context_object),
        )
        connection.commit()
        first_view = PacketBuilder(connection, configuration).resolve_contribution(first)
        assert context_object in first_view.context_ids
    finally:
        connection.close()

    merged = runner.invoke(app, ["merge", first, second, "--config", str(config_path)])
    assert merged.exit_code == 0, merged.output
    merge_result = json.loads(merged.output)
    contribution_id = str(merge_result["contribution_id"])

    connection = connect(configuration.database_path)
    try:
        payload = json.loads(
            str(
                connection.execute(
                    "SELECT payload_json FROM human_decisions WHERE id=?",
                    (merge_result["decision_id"],),
                ).fetchone()[0]
            )
        )
        assert payload["context_members"] == [context_object]
        assert set(payload["members"]) == {material_first, material_second, context_object}
    finally:
        connection.close()

    split = runner.invoke(
        app,
        ["split", contribution_id, material_second, context_object, "--config", str(config_path)],
    )
    assert split.exit_code == 0, split.output
    split_result = json.loads(split.output)
    connection = connect(configuration.database_path)
    try:
        split_payload = json.loads(
            str(
                connection.execute(
                    "SELECT payload_json FROM human_decisions WHERE id=?",
                    (split_result["decision_id"],),
                ).fetchone()[0]
            )
        )
        assert set(split_payload["members"]) == {material_second, context_object}
        assert split_payload["context_members"] == [context_object]
    finally:
        connection.close()

    undone = runner.invoke(
        app, ["undo", str(split_result["decision_id"]), "--config", str(config_path)]
    )
    assert undone.exit_code == 0, undone.output
    rebuilt = runner.invoke(app, ["rebuild", "candidates", "sample", "--config", str(config_path)])
    assert rebuilt.exit_code == 0, rebuilt.output
    connection = connect(configuration.database_path)
    try:
        view = PacketBuilder(connection, configuration).resolve_contribution(contribution_id)
        assert view.member_ids == {material_first, material_second}
        assert view.context_ids == {context_object}
    finally:
        connection.close()
