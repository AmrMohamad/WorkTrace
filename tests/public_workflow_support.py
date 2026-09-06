"""Hermetic public-workflow fixture; discovery always uses real CLI imports."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from typer.testing import CliRunner

from worktrace.cli import app  # type: ignore[import-untyped]
from worktrace.mcp_server.tools import WorkTraceTools  # type: ignore[import-untyped]


def _git(path: Path, *args: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True, env=env
    )
    return result.stdout.strip()


def _issue(issue_id: str, key: str, summary: str) -> dict[str, object]:
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "project": {"key": "DEMO"},
            "summary": summary,
            "description": {"type": "doc", "content": []},
            "status": {"name": "Done", "statusCategory": {"key": "done"}},
            "issuetype": {"name": "Story", "subtask": False},
            "created": "2026-03-01T12:00:00Z",
            "updated": "2026-05-01T12:00:00Z",
            "assignee": {"accountId": "self-jira", "displayName": "Renamed Engineer"},
            "reporter": {"accountId": "colleague", "displayName": "Fixture Engineer"},
            "creator": {"accountId": "colleague", "displayName": "Fixture Engineer"},
            "parent": None,
            "subtasks": [],
            "issuelinks": [],
            "labels": [],
            "components": [],
            "fixVersions": [],
            "priority": {"name": "Medium"},
            "resolutiondate": None,
        },
    }


@dataclass
class PublicWorkflowFixture:
    config_path: Path
    repository_path: Path
    runner: CliRunner
    jira_requests: list[str] = field(default_factory=list)
    jira_jql: list[str] = field(default_factory=list)
    commits: dict[str, str] = field(default_factory=dict)
    dense_object_query: str = "Later edited issue"
    dense_issue_key: str = "DEMO-1"

    @property
    def tools(self) -> WorkTraceTools:
        return WorkTraceTools(config_path=self.config_path)

    def invoke(self, *args: str) -> Any:
        return self.runner.invoke(app, [*args, "--config", str(self.config_path)])

    @classmethod
    def create(
        cls,
        tmp_path: Path,
        monkeypatch: Any,
        *,
        bulk_keys: int = 0,
        jira_issues: dict[str, dict[str, object]] | None = None,
        jira_comments: list[dict[str, object]] | None = None,
        jira_changelog: list[dict[str, object]] | None = None,
        dense_context: bool = False,
    ) -> PublicWorkflowFixture:
        for key in list(os.environ):
            if key.startswith("WORKTRACE_"):
                monkeypatch.delenv(key, raising=False)
        repository = tmp_path / "repository"
        subprocess.run(["git", "init", "-q", "-b", "main", str(repository)], check=True)
        env = os.environ.copy()
        env.update(
            {
                "GIT_AUTHOR_NAME": "Renamed Engineer",
                "GIT_AUTHOR_EMAIL": "old-alias@example.test",
                "GIT_COMMITTER_NAME": "Fixture Engineer",
                "GIT_COMMITTER_EMAIL": "integrator@example.test",
                "GIT_AUTHOR_DATE": "2026-04-01T12:00:00Z",
                "GIT_COMMITTER_DATE": "2026-04-01T12:00:00Z",
            }
        )
        (repository / "first.txt").write_text("first\n", encoding="utf-8")
        _git(repository, "add", "first.txt", env=env)
        _git(repository, "commit", "-m", "DEMO-1 author evidence", env=env)
        first = _git(repository, "rev-parse", "HEAD", env=env)
        (repository / "second.txt").write_text("second\n", encoding="utf-8")
        _git(repository, "add", "second.txt", env=env)
        _git(repository, "commit", "-m", f"DEMO-1 context {first}", env=env)
        second = _git(repository, "rev-parse", "HEAD", env=env)
        other_env = dict(env)
        other_env.update(
            {
                "GIT_AUTHOR_NAME": "Fixture Engineer",
                "GIT_AUTHOR_EMAIL": "same-name-other@example.test",
                "GIT_COMMITTER_NAME": "Renamed Engineer",
                "GIT_COMMITTER_EMAIL": "old-alias@example.test",
            }
        )
        (repository / "other.txt").write_text("other\n", encoding="utf-8")
        _git(repository, "add", "other.txt", env=other_env)
        _git(repository, "commit", "-m", "DEMO-1 same-name collaborator", env=other_env)
        other = _git(repository, "rev-parse", "HEAD", env=other_env)
        if dense_context:
            (repository / "dense.txt").write_text("dense\n", encoding="utf-8")
            _git(repository, "add", "dense.txt", env=env)
            _git(repository, "commit", "-m", f"DEMO-1 overlap {first} {second}", env=env)
        if bulk_keys > 1:
            (repository / "bulk.txt").write_text("bulk\n", encoding="utf-8")
            _git(repository, "add", "bulk.txt", env=env)
            _git(
                repository,
                "commit",
                "-m",
                "bulk " + " ".join(f"DEMO-{number}" for number in range(2, bulk_keys + 1)),
                env=env,
            )

        config = tmp_path / "config.toml"
        config.write_text(
            f"""schema_version = 1
[data]
directory = {str(tmp_path / "data")!r}
[employment]
from = "2026-01-01"
to = "2026-12-31"
[identity]
display_name = "Fixture Engineer"
git_author_emails = ["old-alias@example.test"]
git_author_names = ["Renamed Engineer"]
jira_account_id = "self-jira"
[[apps]]
id = "sample"
name = "Sample"
repo_paths = [{str(repository)!r}]
jira_project_keys = ["DEMO"]
gitlab_project_ids = []
""",
            encoding="utf-8",
        )
        fixture = cls(
            config,
            repository,
            CliRunner(),
            commits={"first": first, "second": second, "other": other},
        )
        monkeypatch.setenv("WORKTRACE_JIRA_BASE_URL", "https://jira.fixture")
        monkeypatch.setenv("WORKTRACE_JIRA_EMAIL", "fixture@example.test")
        monkeypatch.setenv("WORKTRACE_JIRA_API_TOKEN", "fixture-token")

        def handler(request: httpx.Request) -> httpx.Response:
            fixture.jira_requests.append(f"{request.method} {request.url.path}")
            if request.url.path.endswith("/myself"):
                return httpx.Response(200, json={"accountId": "self-jira"}, request=request)
            if request.url.path.endswith("/search/jql"):
                raw = json.loads(request.content)
                jql = str(raw.get("jql", ""))
                fixture.jira_jql.append(jql)
                keys = sorted(set(re.findall(r'"(DEMO-[0-9]+)"', jql)))
                available = jira_issues or {
                    "DEMO-1": _issue("10001", "DEMO-1", "Later edited issue")
                }
                return httpx.Response(
                    200,
                    json={
                        "issues": (
                            [available[key] for key in keys if key in available]
                            if keys
                            else list(available.values())
                        ),
                        "isLast": True,
                    },
                    request=request,
                )
            if request.url.path.endswith("/comment"):
                return httpx.Response(
                    200,
                    json={
                        "startAt": 0,
                        "maxResults": 100,
                        "comments": jira_comments or [],
                        "total": len(jira_comments or []),
                    },
                    request=request,
                )
            if request.url.path.endswith("/changelog"):
                return httpx.Response(
                    200,
                    json={
                        "startAt": 0,
                        "maxResults": 100,
                        "total": len(jira_changelog or []),
                        "values": jira_changelog or [],
                        "isLast": True,
                    },
                    request=request,
                )
            raise AssertionError(f"unexpected Jira request: {request.method} {request.url.path}")

        client = httpx.Client(
            base_url="https://jira.fixture", transport=httpx.MockTransport(handler)
        )
        monkeypatch.setattr("worktrace.cli.httpx.Client", lambda **_: client)
        return fixture
