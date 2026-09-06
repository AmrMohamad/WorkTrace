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
    jira_failures: dict[tuple[str, str], int] = field(default_factory=dict)
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
        jira_comments: dict[str, list[dict[str, object]]] | None = None,
        jira_changelog: dict[str, list[dict[str, object]]] | None = None,
        subresource_page_size: int = 100,
        jira_failures: dict[tuple[str, str], int] | None = None,
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
        failures = jira_failures if jira_failures is not None else {}
        fixture = cls(
            config,
            repository,
            CliRunner(),
            commits={"first": first, "second": second, "other": other},
            jira_failures=failures,
        )
        monkeypatch.setenv("WORKTRACE_JIRA_BASE_URL", "https://jira.fixture")
        monkeypatch.setenv("WORKTRACE_JIRA_EMAIL", "fixture@example.test")
        monkeypatch.setenv("WORKTRACE_JIRA_API_TOKEN", "fixture-token")
        available = jira_issues or {"DEMO-1": _issue("10001", "DEMO-1", "Later edited issue")}

        def handler(request: httpx.Request) -> httpx.Response:
            fixture.jira_requests.append(
                f"{request.method} {request.url.path}?{request.url.query.decode()}"
            )
            if request.url.path.endswith("/myself"):
                return httpx.Response(200, json={"accountId": "self-jira"}, request=request)
            if request.url.path.endswith("/search/jql"):
                raw = json.loads(request.content)
                jql = str(raw.get("jql", ""))
                fixture.jira_jql.append(jql)
                if 'project in ("DEMO")' not in jql:
                    raise AssertionError(f"unexpected Jira project scope: {jql}")
                if "key in" in jql:
                    reason = "exact_key"
                    keys = sorted(set(re.findall(r'"(DEMO-[0-9]+)"', jql)))
                    issues = [available[key] for key in keys if key in available]
                elif "updatedBy" in jql:
                    reason = "historical_updater"
                    issues = []
                elif "assignee WAS" in jql:
                    reason = "historical_assignee"
                    issues = []
                elif "creator =" in jql:
                    reason = "creator_created"
                    issues = []
                else:
                    raise AssertionError(f"unrecognized Jira discovery query: {jql}")
                if reason != "exact_key":
                    if (
                        "self-jira" not in jql
                        or '"2025-12-31"' not in jql
                        or '"2027-01-02"' not in jql
                    ):
                        raise AssertionError(f"invalid historical scope: {jql}")
                    issues = []
                    for issue in available.values():
                        selected_by = issue.get("_workflow_selected_by", [])
                        if isinstance(selected_by, list) and reason in selected_by:
                            issues.append(issue)
                return httpx.Response(
                    200,
                    json={"issues": issues, "isLast": True},
                    request=request,
                )
            if request.url.path.endswith("/comment"):
                issue_id = request.url.path.rsplit("/", 2)[-2]
                if issue_id not in {str(value["id"]) for value in available.values()}:
                    raise AssertionError(f"unknown Jira comment issue: {issue_id}")
                if ("comment", issue_id) in failures:
                    return httpx.Response(failures[("comment", issue_id)], request=request)
                comments = (jira_comments or {}).get(issue_id, [])
                start = int(request.url.params.get("startAt", "0"))
                page = comments[start : start + subresource_page_size]
                return httpx.Response(
                    200,
                    json={
                        "startAt": start,
                        "maxResults": subresource_page_size,
                        "comments": page,
                        "total": len(comments),
                    },
                    request=request,
                )
            if request.url.path.endswith("/changelog"):
                issue_id = request.url.path.rsplit("/", 2)[-2]
                if issue_id not in {str(value["id"]) for value in available.values()}:
                    raise AssertionError(f"unknown Jira changelog issue: {issue_id}")
                if ("changelog", issue_id) in failures:
                    return httpx.Response(failures[("changelog", issue_id)], request=request)
                changelog = (jira_changelog or {}).get(issue_id, [])
                start = int(request.url.params.get("startAt", "0"))
                page = changelog[start : start + subresource_page_size]
                return httpx.Response(
                    200,
                    json={
                        "startAt": start,
                        "maxResults": subresource_page_size,
                        "total": len(changelog),
                        "values": page,
                        "isLast": start + len(page) >= len(changelog),
                    },
                    request=request,
                )
            raise AssertionError(f"unexpected Jira request: {request.method} {request.url.path}")

        real_httpx_client = httpx.Client
        monkeypatch.setattr(
            "worktrace.cli.httpx.Client",
            lambda **_: real_httpx_client(
                base_url="https://jira.fixture", transport=httpx.MockTransport(handler)
            ),
        )
        return fixture
