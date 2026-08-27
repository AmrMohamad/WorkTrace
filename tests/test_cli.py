from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.utils import strip_ansi
from typer.testing import CliRunner

from worktrace.cli import _commit_seed_scope, _CommitSeedSelection, app


def _write_config(tmp_path: Path, repository: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f"""
schema_version = 1

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
id = "sample_store"
name = "Sample Store"
market = "XX"
business_type = "fixture"
jira_project_keys = ["DEMO"]
gitlab_project_ids = [101]
repo_paths = [{str(repository)!r}]
""",
        encoding="utf-8",
    )
    return config


def test_cli_help_init_status_search_export_and_confirmed_purge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKTRACE_DB_PATH", raising=False)
    repository = tmp_path / "fixture-repository"
    subprocess.run(
        ["git", "init", "-q", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    config = _write_config(tmp_path, repository)
    runner = CliRunner()

    help_result = runner.invoke(app, ["--help"])
    assert help_result.exit_code == 0
    assert "Local evidence-oriented contribution reconstruction" in help_result.stdout

    first_init = runner.invoke(app, ["init", "--config", str(config)])
    assert first_init.exit_code == 0, first_init.stdout
    assert json.loads(first_init.stdout)["migrations_applied"] == [1, 2, 3]

    second_init = runner.invoke(app, ["init", "--config", str(config)])
    assert second_init.exit_code == 0, second_init.stdout
    assert json.loads(second_init.stdout)["migrations_applied"] == []

    database = tmp_path / "data" / "worktrace.sqlite3"
    key = tmp_path / "data" / "email-hmac.key"
    assert database.exists()
    assert key.exists()

    status = runner.invoke(app, ["status", "sample_store", "--config", str(config)])
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout) == {"app_id": "sample_store", "sources": []}

    search = runner.invoke(
        app,
        ["search", "sample_store", "checkout", "--config", str(config)],
    )
    assert search.exit_code == 0, search.stdout
    assert json.loads(search.stdout) == []

    destination = tmp_path / "private-export.json"
    exported = runner.invoke(
        app,
        ["export", "sample_store", str(destination), "--config", str(config)],
    )
    assert exported.exit_code == 0, exported.stdout
    assert json.loads(exported.stdout)["objects"] == 0
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["schema"] == "worktrace-export-v2"
    assert payload["app_id"] == "sample_store"

    refused = runner.invoke(app, ["purge", "--config", str(config)])
    assert refused.exit_code != 0
    assert "purge requires --yes" in strip_ansi(refused.stderr)
    assert database.exists()
    assert key.exists()

    purged = runner.invoke(app, ["purge", "--yes", "--config", str(config)])
    assert purged.exit_code == 0, purged.stdout
    purge_result = json.loads(purged.stdout)
    assert purge_result["recoverable"] is True
    assert "Exports and custom backups" in purge_result["retained"]
    assert set(purge_result["removed"]) == {str(database), str(key)}
    assert not database.exists()
    assert not key.exists()
    assert destination.exists()


def test_local_commit_seed_cap_records_drops_and_selection_policy() -> None:
    scope = _commit_seed_scope(
        _CommitSeedSelection(
            values=("c" * 40, "b" * 40),
            total_count=3,
            limit=2,
        )
    )

    assert scope["selection_biased"] is True
    assert scope["relevant_local_commit_sha_total"] == 3
    assert scope["selection_events"] == [
        {
            "kind": "local_git_commit_seed_cap",
            "input_count": 3,
            "selected_count": 2,
            "dropped_count": 1,
            "limit": 2,
            "selection_policy": "source_updated_at_desc_then_sha_desc",
        }
    ]
