from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from worktrace.cli import app
from worktrace.mcp_server.tools import WorkTraceTools


def _git(repository: Path, *arguments: str, environment: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return result.stdout.strip()


def _config(tmp_path: Path, repository: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""schema_version = 1

[data]
directory = {str(tmp_path / "data")!r}

[employment]
from = "2026-01-01"
to = "2026-12-31"

[identity]
display_name = "Fixture Engineer"
git_author_emails = ["fixture@example.test"]
git_author_names = ["Fixture Engineer"]

[[apps]]
id = "sample"
name = "Sample"
repo_paths = [{str(repository)!r}]
gitlab_project_ids = []
jira_project_keys = []
""",
        encoding="utf-8",
    )
    return path


def test_public_search_context_summary_then_cli_correction_workflow(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Fixture Engineer",
            "GIT_AUTHOR_EMAIL": "fixture@example.test",
            "GIT_COMMITTER_NAME": "Fixture Engineer",
            "GIT_COMMITTER_EMAIL": "fixture@example.test",
            "GIT_AUTHOR_DATE": "2026-04-01T12:00:00Z",
            "GIT_COMMITTER_DATE": "2026-04-01T12:00:00Z",
        }
    )
    (repository / "first.txt").write_text("first\n", encoding="utf-8")
    _git(repository, "add", "first.txt", environment=environment)
    _git(repository, "commit", "-m", "first implementation", environment=environment)
    first_sha = _git(repository, "rev-parse", "HEAD", environment=environment)
    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    _git(repository, "add", "second.txt", environment=environment)
    _git(
        repository,
        "commit",
        "-m",
        f"second discovery references {first_sha}",
        environment=environment,
    )

    config_path = _config(tmp_path, repository)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, initialized.output
    imported = runner.invoke(app, ["import", "all", "sample", "--config", str(config_path)])
    assert imported.exit_code == 0, imported.output
    rebuilt = runner.invoke(app, ["rebuild", "all", "sample", "--config", str(config_path)])
    assert rebuilt.exit_code == 0, rebuilt.output

    tools = WorkTraceTools(config_path=config_path)
    found = tools.search_evidence(app_id="sample", query="second discovery", limit=10)
    assert "error" not in found
    result = next(item for item in found["results"] if item["external_id"] != first_sha)
    object_id = str(result["object_id"])
    initial_token = str(found["view_token"])

    context = tools.get_evidence_context(
        app_id="sample", object_id=object_id, expected_view_token=initial_token
    )
    relation_items = context["relations"]["items"]
    assert any(item["relationship_type"] == "mentions_commit_sha" for item in relation_items)
    membership_items = context["memberships"]["items"]
    assert membership_items and membership_items[0]["basis"] == "suggestion"
    candidate_id = str(membership_items[0]["candidate_id"])
    summary = tools.get_contribution_summary(
        contribution_id=candidate_id, expected_view_token=str(context["view_token"])
    )
    assert summary["contribution"]["candidate_id"] == candidate_id

    confirmed = runner.invoke(app, ["confirm", candidate_id, "--config", str(config_path)])
    assert confirmed.exit_code == 0, confirmed.output
    confirmed_context = tools.get_evidence_context(app_id="sample", object_id=object_id)
    assert confirmed_context["view_token"] != context["view_token"]
    assert any(
        item["basis"] == "confirmed" and item["candidate_id"] == candidate_id
        for item in confirmed_context["memberships"]["items"]
    )

    removed = runner.invoke(
        app, ["remove-member", candidate_id, object_id, "--config", str(config_path)]
    )
    assert removed.exit_code == 0, removed.output
    removed_context = tools.get_evidence_context(app_id="sample", object_id=object_id)
    assert removed_context["view_token"] != confirmed_context["view_token"]
    assert not any(
        item["basis"] == "confirmed" and item["candidate_id"] == candidate_id
        for item in removed_context["memberships"]["items"]
    )

    remove_decision_id = str(json.loads(removed.output)["decision_id"])
    undone = runner.invoke(app, ["undo", remove_decision_id, "--config", str(config_path)])
    assert undone.exit_code == 0, undone.output
    restored = tools.get_evidence_context(app_id="sample", object_id=object_id)
    assert restored["view_token"] != removed_context["view_token"]
    assert any(
        item["basis"] == "confirmed" and item["candidate_id"] == candidate_id
        for item in restored["memberships"]["items"]
    )
