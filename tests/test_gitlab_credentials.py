from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Literal

import httpx
import pytest
from typer.testing import CliRunner

from worktrace.cli import app
from worktrace.config import gitlab_credentials
from worktrace.errors import ConfigurationError


def _config(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
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
gitlab_user_id = 7
gitlab_username = "fixture"
[[apps]]
id = "sample"
name = "Sample"
gitlab_project_ids = [101]
repo_paths = [{str(repository)!r}]
""",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    ("variable", "scheme", "expected"),
    (
        (
            "WORKTRACE_GITLAB_TOKEN",
            "pat",
            {"Accept": "application/json", "PRIVATE-TOKEN": "pat-secret"},
        ),
        (
            "WORKTRACE_GITLAB_OAUTH_TOKEN",
            "oauth",
            {"Accept": "application/json", "Authorization": "Bearer oauth-secret"},
        ),
    ),
)
def test_gitlab_credentials_select_exact_auth_header(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    scheme: Literal["pat", "oauth"],
    expected: dict[str, str],
) -> None:
    monkeypatch.delenv("WORKTRACE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("WORKTRACE_GITLAB_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("WORKTRACE_GITLAB_BASE_URL", "https://gitlab.example")
    monkeypatch.setenv(
        variable, expected.get("PRIVATE-TOKEN", "oauth-secret").removeprefix("Bearer ")
    )
    credentials = gitlab_credentials()

    assert credentials is not None
    assert credentials.auth_scheme == scheme
    assert credentials.request_headers() == expected
    assert "pat-secret" not in repr(credentials)
    assert "oauth-secret" not in repr(credentials)
    assert "pat-secret" not in json.dumps({"credentials": repr(credentials)})
    assert "oauth-secret" not in json.dumps({"credentials": repr(credentials)})


def test_gitlab_credentials_are_unconfigured_without_any_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKTRACE_GITLAB_BASE_URL", raising=False)
    monkeypatch.delenv("WORKTRACE_GITLAB_TOKEN", raising=False)
    monkeypatch.delenv("WORKTRACE_GITLAB_OAUTH_TOKEN", raising=False)

    assert gitlab_credentials() is None


@pytest.mark.parametrize(
    "environment",
    (
        {"WORKTRACE_GITLAB_TOKEN": "pat-secret"},
        {"WORKTRACE_GITLAB_OAUTH_TOKEN": "oauth-secret"},
        {
            "WORKTRACE_GITLAB_BASE_URL": "https://gitlab.example",
            "WORKTRACE_GITLAB_TOKEN": "pat-secret",
            "WORKTRACE_GITLAB_OAUTH_TOKEN": "oauth-secret",
        },
    ),
)
def test_gitlab_credentials_reject_incomplete_or_ambiguous_environment(
    monkeypatch: pytest.MonkeyPatch, environment: dict[str, str]
) -> None:
    for name in (
        "WORKTRACE_GITLAB_BASE_URL",
        "WORKTRACE_GITLAB_TOKEN",
        "WORKTRACE_GITLAB_OAUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigurationError):
        gitlab_credentials()


@pytest.mark.parametrize("variable", ["WORKTRACE_GITLAB_TOKEN", "WORKTRACE_GITLAB_OAUTH_TOKEN"])
def test_doctor_live_uses_the_selected_gitlab_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variable: str
) -> None:
    config = _config(tmp_path)
    assert CliRunner().invoke(app, ["init", "--config", str(config)]).exit_code == 0
    secret = "fixture-secret"
    monkeypatch.setenv("WORKTRACE_GITLAB_BASE_URL", "https://gitlab.example")
    monkeypatch.setenv(variable, secret)
    captured: list[dict[str, str]] = []
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 7, "username": "fixture"}, request=request)
        if request.url.path == "/api/v4/projects/101":
            return httpx.Response(200, json={}, request=request)
        return httpx.Response(404, request=request)

    def client(
        *, base_url: str, headers: dict[str, str], timeout: float, **_: object
    ) -> httpx.Client:
        captured.append(dict(headers))
        return real_client(
            base_url=base_url,
            headers=headers,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr("worktrace.doctor.httpx.Client", client)
    result = CliRunner().invoke(app, ["doctor", "--live", "--config", str(config)])

    assert result.exit_code == 0, result.stdout
    assert captured == [
        {
            "Accept": "application/json",
            ("PRIVATE-TOKEN" if variable == "WORKTRACE_GITLAB_TOKEN" else "Authorization"): secret
            if variable == "WORKTRACE_GITLAB_TOKEN"
            else f"Bearer {secret}",
        }
    ]
    assert secret not in result.stdout


def test_doctor_live_rejects_unsafe_gitlab_origin_before_client_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    assert CliRunner().invoke(app, ["init", "--config", str(config)]).exit_code == 0
    monkeypatch.setenv("WORKTRACE_GITLAB_BASE_URL", "http://attacker.example/path")
    monkeypatch.setenv("WORKTRACE_GITLAB_OAUTH_TOKEN", "oauth-secret")
    constructed = False

    def forbidden_client(*_: object, **__: object) -> httpx.Client:
        nonlocal constructed
        constructed = True
        raise AssertionError("unsafe provider client was constructed")

    monkeypatch.setattr("worktrace.doctor.httpx.Client", forbidden_client)
    result = CliRunner().invoke(app, ["doctor", "--live", "--config", str(config)])

    assert result.exit_code == 2
    assert constructed is False
    assert "oauth-secret" not in result.stdout
