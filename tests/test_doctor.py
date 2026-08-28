from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from worktrace.cli import app


def _config(tmp_path: Path, repository: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
schema_version = 1
[data]
directory = {str(tmp_path / "data")!r}
[employment]
from = "2024-01-01"
to = "2026-12-31"
[identity]
display_name = "Fixture Engineer"
git_author_emails = ["fixture@example.test"]
git_author_names = ["Fixture Engineer"]
[[apps]]
id = "sample"
name = "Sample"
repo_paths = [{str(repository)!r}]
""",
        encoding="utf-8",
    )
    return path


def test_default_doctor_is_offline_and_returns_structured_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = _config(tmp_path, repository)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0

    def unexpected_client(*args: object, **kwargs: object) -> httpx.Client:
        raise AssertionError("default doctor attempted a provider request")

    monkeypatch.setattr("worktrace.doctor.httpx.Client", unexpected_client)
    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert {tuple(sorted(check)) for check in payload["checks"]} == {
        ("message", "name", "ok", "remediation", "scope", "status")
    }
    live = next(check for check in payload["checks"] if check["name"] == "live_providers")
    assert live["status"] == "skipped"


def test_doctor_fails_when_configured_provider_credentials_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = _config(tmp_path, repository)
    text = config.read_text(encoding="utf-8")
    config.write_text(text + '\njira_project_keys = ["DEMO"]\n', encoding="utf-8")
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0
    for name in (
        "WORKTRACE_JIRA_BASE_URL",
        "WORKTRACE_JIRA_EMAIL",
        "WORKTRACE_JIRA_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    result = runner.invoke(app, ["doctor", "--config", str(config)])
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    check = next(item for item in payload["checks"] if item["name"] == "jira_credentials")
    assert check["status"] == "fail"
    assert "token" not in check["message"].casefold()


def test_doctor_reports_invalid_configuration_as_structured_failure(tmp_path: Path) -> None:
    config = tmp_path / "broken.toml"
    config.write_text("schema_version = [not valid TOML", encoding="utf-8")

    result = CliRunner().invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["checks"] == [
        {
            "name": "configuration",
            "scope": "local",
            "status": "fail",
            "message": payload["checks"][0]["message"],
            "remediation": "Correct the WorkTrace TOML configuration and scope mappings.",
            "ok": False,
        }
    ]


def test_doctor_fails_for_nonprivate_data_and_database_modes(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = _config(tmp_path, repository)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0
    data = tmp_path / "data"
    database = data / "worktrace.sqlite3"
    data.chmod(0o755)
    database.chmod(0o644)

    result = runner.invoke(app, ["doctor", "--config", str(config)])

    assert result.exit_code == 2
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["storage_permissions"]["status"] == "fail"
    assert checks["database_permissions"]["status"] == "fail"


def test_doctor_live_rejects_unsafe_origin_before_constructing_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = _config(tmp_path, repository)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'git_author_names = ["Fixture Engineer"]',
            'git_author_names = ["Fixture Engineer"]\njira_account_id = "account-7"',
        )
        + '\njira_project_keys = ["DEMO"]\n',
        encoding="utf-8",
    )
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0
    monkeypatch.setenv("WORKTRACE_JIRA_BASE_URL", "http://attacker.example/user")
    monkeypatch.setenv("WORKTRACE_JIRA_EMAIL", "fixture@example.test")
    monkeypatch.setenv("WORKTRACE_JIRA_API_TOKEN", "must-not-be-sent")
    constructed = False

    def forbidden_client(*_: object, **__: object) -> httpx.Client:
        nonlocal constructed
        constructed = True
        raise AssertionError("unsafe provider client was constructed")

    monkeypatch.setattr("worktrace.doctor.httpx.Client", forbidden_client)
    result = runner.invoke(app, ["doctor", "--live", "--config", str(config)])

    assert result.exit_code == 2
    assert constructed is False
    checks = json.loads(result.stdout)["checks"]
    assert any(check["name"] == "jira_live" and check["status"] == "fail" for check in checks)


@pytest.mark.parametrize(
    ("environment_name", "check_name"),
    (
        ("WORKTRACE_JIRA_BASE_URL", "jira_live"),
        ("WORKTRACE_GITLAB_BASE_URL", "gitlab_live"),
    ),
)
def test_doctor_live_reports_partial_credentials_as_structured_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    check_name: str,
) -> None:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    config = _config(tmp_path, repository)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0
    monkeypatch.setenv(environment_name, "https://provider.example")

    result = runner.invoke(app, ["doctor", "--live", "--config", str(config)])

    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    check = next(item for item in payload["checks"] if item["name"] == check_name)
    assert check["status"] == "fail"
    assert "ConfigurationError" in check["message"]
