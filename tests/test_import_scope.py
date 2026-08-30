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
from worktrace.cli import _assert_no_scope_contraction, _window, app
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.repository import EvidenceRepository
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


@pytest.mark.parametrize(
    "command",
    [
        ["import", "git", "sample_store", "{repo}", "2025-01-01", "2025-12-31"],
        ["import", "all", "sample_store", "2025-01-01", "2025-12-31"],
    ],
)
def test_narrow_import_window_is_rejected_before_creating_session_or_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
) -> None:
    monkeypatch.delenv("WORKTRACE_DB_PATH", raising=False)
    repository = _repository(tmp_path)
    config = _config(tmp_path, repository)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, _error_text(initialized)

    database_path = tmp_path / "data" / "worktrace.sqlite3"
    connection = connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM import_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
    finally:
        connection.close()

    arguments = [value.format(repo=repository) for value in command] + [
        "--config",
        str(config),
    ]
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0
    assert isinstance(result.exception, WorkTraceError)
    assert "unsafe_scope_replacement" in _error_text(result)
    connection = connect(database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM import_sessions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
    finally:
        connection.close()


def test_configured_scope_cannot_contract_past_authoritative_history(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config = _config(tmp_path, repository_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"date_from": "2024-01-01", "date_to": "2026-08-26"},
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="unsafe_scope_replacement"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2025, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_scope_contraction_uses_import_session_dates_when_run_scope_dates_are_missing(
    tmp_path: Path,
) -> None:
    repository_path = _repository(tmp_path)
    config_path = _config(tmp_path, repository_path)
    configuration = load_config(config_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        session_id = evidence.create_import_session(
            configuration.app("sample_store"), date(2024, 1, 1), date(2026, 8, 26)
        )
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"selection_reasons": ["legacy import"]},
            session_id,
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="would hide"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2025, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_one_missing_scope_boundary_does_not_mix_scope_and_session_ranges(
    tmp_path: Path,
) -> None:
    repository_path = _repository(tmp_path)
    config_path = _config(tmp_path, repository_path)
    configuration = load_config(config_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        session_id = evidence.create_import_session(
            configuration.app("sample_store"), date(2024, 1, 1), date(2026, 8, 26)
        )
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"date_from": "2025-01-01"},
            session_id,
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="only one range boundary"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2025, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_reversed_authoritative_scope_range_fails_closed(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config_path = _config(tmp_path, repository_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"date_from": "2026-08-26", "date_to": "2024-01-01"},
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="scope range is reversed"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2024, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_cross_app_parent_import_session_cannot_supply_scope_dates(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config_path = _config(tmp_path, repository_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        with connection:
            connection.execute("INSERT INTO apps(id, name) VALUES ('other_app', 'Other App')")
            connection.execute(
                """
                INSERT INTO import_sessions(
                    id, app_id, status, started_at, date_from, date_to
                ) VALUES (?, ?, 'complete', ?, ?, ?)
                """,
                (
                    "import:other-app",
                    "other_app",
                    "2026-08-26T00:00:00Z",
                    "2024-01-01",
                    "2026-08-26",
                ),
            )
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"selection_reasons": ["legacy"]},
            "import:other-app",
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="same-application parent session"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2024, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_conflicting_scope_and_parent_session_ranges_fail_closed(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config_path = _config(tmp_path, repository_path)
    configuration = load_config(config_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        session_id = evidence.create_import_session(
            configuration.app("sample_store"), date(2024, 1, 1), date(2026, 8, 26)
        )
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"date_from": "2025-01-01", "date_to": "2026-08-26"},
            session_id,
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="contradictory ranges"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2024, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_complete_scope_and_same_app_parent_session_range_succeeds(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config_path = _config(tmp_path, repository_path)
    configuration = load_config(config_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config_path)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        session_id = evidence.create_import_session(
            configuration.app("sample_store"), date(2024, 1, 1), date(2026, 8, 26)
        )
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"date_from": "2024-01-01", "date_to": "2026-08-26"},
            session_id,
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        _assert_no_scope_contraction(
            evidence,
            "sample_store",
            date(2024, 1, 1),
            date(2026, 8, 26),
        )
    finally:
        connection.close()


def test_unknown_authoritative_non_manual_scope_fails_closed(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config = _config(tmp_path, repository_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        run_id = evidence.start_sync_run(
            "sample_store", "git", "fixture-repository", {"selection_reasons": ["legacy"]}
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="cannot be verified"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2024, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_malformed_authoritative_scope_dates_fail_closed(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config = _config(tmp_path, repository_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        run_id = evidence.start_sync_run(
            "sample_store",
            "git",
            "fixture-repository",
            {"date_from": "not-a-date", "date_to": "2026-08-26"},
        )
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        with pytest.raises(WorkTraceError, match="is malformed"):
            _assert_no_scope_contraction(
                evidence,
                "sample_store",
                date(2024, 1, 1),
                date(2026, 8, 26),
            )
    finally:
        connection.close()


def test_manual_evidence_without_snapshot_dates_does_not_block_import(tmp_path: Path) -> None:
    repository_path = _repository(tmp_path)
    config = _config(tmp_path, repository_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, _error_text(initialized)

    connection = connect(tmp_path / "data" / "worktrace.sqlite3")
    try:
        evidence = EvidenceRepository(connection)
        run_id = evidence.start_sync_run("sample_store", "manual", "local", {})
        evidence.finish_sync_run(run_id, "complete", "complete_for_scope")

        _assert_no_scope_contraction(
            evidence,
            "sample_store",
            date(2024, 1, 1),
            date(2026, 8, 26),
        )
    finally:
        connection.close()


def test_omitted_window_uses_the_complete_configured_employment_range(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    config_path = _config(tmp_path, repository)
    configuration = load_config(config_path)

    assert _window(configuration, None, None) == (
        date(2024, 1, 1),
        date(2026, 8, 26),
    )
