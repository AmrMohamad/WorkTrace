from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner, Result

from worktrace.adapters.git_local import LocalGitAdapter, LocalGitConfig
from worktrace.adapters.gitlab import GitLabAdapter, GitLabConfig
from worktrace.adapters.jira import JiraAdapter, JiraConfig
from worktrace.cli import app
from worktrace.errors import ConfigurationError, WorkTraceError


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "fixture-repository"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repository)],
        check=True,
        capture_output=True,
        text=True,
    )
    return repository


def _config(tmp_path: Path, repository: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
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
jira_project_keys = ["DEMO"]
gitlab_project_ids = [101]
repo_paths = [{str(repository)!r}]
""",
        encoding="utf-8",
    )
    return path


def _error_text(result: Result) -> str:
    exception = str(result.exception) if result.exception is not None else ""
    return "\n".join((result.stdout, result.stderr, exception))


def test_each_adapter_rejects_a_reversed_window(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    start = date(2026, 2, 1)
    end = date(2026, 1, 1)

    with pytest.raises(ConfigurationError, match="Git date_from must not be after date_to"):
        LocalGitAdapter(
            LocalGitConfig(
                repository_path=repository,
                source_instance=str(repository),
                app_id="sample_store",
                email_key=b"fixture-only-key",
                date_from=start,
                date_to=end,
            )
        )

    with (
        httpx.Client(base_url="https://jira.fixture.example") as client,
        pytest.raises(ConfigurationError, match="Jira date_from must not be after date_to"),
    ):
        JiraAdapter(
            JiraConfig(
                base_url="https://jira.fixture.example",
                source_instance="https://jira.fixture.example",
                app_id="sample_store",
                project_keys=("DEMO",),
                email_key=b"fixture-only-key",
                date_from=start,
                date_to=end,
            ),
            client,
        )

    with (
        httpx.Client(base_url="https://gitlab.fixture.example") as client,
        pytest.raises(ConfigurationError, match="GitLab date_from must not be after date_to"),
    ):
        GitLabAdapter(
            GitLabConfig(
                base_url="https://gitlab.fixture.example",
                source_instance="https://gitlab.fixture.example/projects/101",
                app_id="sample_store",
                project_id=101,
                email_key=b"fixture-only-key",
                date_from=start,
                date_to=end,
            ),
            client,
        )


@pytest.mark.parametrize(
    "command",
    [
        ["import", "git", "sample_store", "{repo}", "{start}", "{end}"],
        ["import", "jira", "sample_store", "{start}", "{end}"],
        ["import", "gitlab", "sample_store", "101", "{start}", "{end}"],
        ["import", "all", "sample_store", "{start}", "{end}"],
    ],
)
@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-02-01", "2026-01-01", "date_from must not be after date_to"),
        ("2023-12-31", "2024-01-02", "outside the configured employment scope"),
        ("2026-08-01", "2026-08-27", "outside the configured employment scope"),
    ],
)
def test_cli_import_commands_reject_reversed_and_out_of_employment_windows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    start: str,
    end: str,
    message: str,
) -> None:
    monkeypatch.delenv("WORKTRACE_DB_PATH", raising=False)
    repository = _repository(tmp_path)
    config = _config(tmp_path, repository)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, _error_text(initialized)

    arguments = [value.format(repo=repository, start=start, end=end) for value in command] + [
        "--config",
        str(config),
    ]
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0
    assert isinstance(result.exception, WorkTraceError)
    assert message in _error_text(result)
