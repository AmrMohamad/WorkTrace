from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from worktrace.adapters.git_local import LocalGitAdapter, LocalGitConfig
from worktrace.doctor import _git
from worktrace.git_environment import local_git_environment

_SYNTHETIC_OVERRIDES = {
    "WORKTRACE_JIRA_API_TOKEN": "synthetic-jira-token",
    "WORKTRACE_JIRA_NEW_CREDENTIAL": "synthetic-future-jira",
    "WORKTRACE_GITLAB_TOKEN": "synthetic-gitlab-token",
    "WORKTRACE_GITLAB_NEW_CREDENTIAL": "synthetic-future-gitlab",
    "WORKTRACE_EMAIL_HMAC_KEY": "synthetic-hmac-key",
    "WORKTRACE_EMAIL_HMAC_FUTURE": "synthetic-future-hmac",
    "GIT_CONFIG_COUNT": "1",
    "GIT_CONFIG_KEY_0": "remote.origin.url",
    "GIT_CONFIG_VALUE_0": "https://wrong.example.test/repo.git",
    "GIT_CONFIG_KEY_99": "orphaned-key",
    "GIT_CONFIG_VALUE_99": "orphaned-value",
    "GIT_CONFIG_PARAMETERS": "'remote.origin.url=wrong'",
    "GIT_CONFIG": "/nonexistent/synthetic-config",
    "GIT_CONFIG_GLOBAL": "/nonexistent/synthetic-global",
    "GIT_CONFIG_SYSTEM": "/nonexistent/synthetic-system",
    "GIT_DIR": "/nonexistent/synthetic-git-dir",
    "GIT_WORK_TREE": "/nonexistent/synthetic-work-tree",
    "GIT_COMMON_DIR": "/nonexistent/synthetic-common-dir",
    "GIT_INDEX_FILE": "/nonexistent/synthetic-index",
    "GIT_OBJECT_DIRECTORY": "/nonexistent/synthetic-objects",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/nonexistent/synthetic-alternates",
    "GIT_NAMESPACE": "synthetic-namespace",
    "GIT_SHALLOW_FILE": "/nonexistent/synthetic-shallow",
    "GIT_GRAFT_FILE": "/nonexistent/synthetic-grafts",
    "GIT_REPLACE_REF_BASE": "refs/synthetic-replacements",
    "GIT_CEILING_DIRECTORIES": "/nonexistent/synthetic-ceiling",
    "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1",
    "GIT_EXEC_PATH": "/nonexistent/synthetic-executables",
    "GIT_EXTERNAL_DIFF": "synthetic-external-diff",
    "GIT_DIFF_OPTS": "--synthetic-option",
    "GIT_ASKPASS": "synthetic-askpass",
    "SSH_ASKPASS": "synthetic-ssh-askpass",
    "GIT_SSH": "synthetic-ssh",
    "GIT_SSH_COMMAND": "synthetic-ssh-command",
    "GIT_SSH_VARIANT": "synthetic-ssh-variant",
    "GIT_PAGER": "synthetic-pager",
    "GIT_TRACE": "/nonexistent/synthetic-trace",
    "GIT_TRACE2_EVENT": "/nonexistent/synthetic-trace-event",
}


def test_local_git_environment_filters_without_mutating_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = {
        **_SYNTHETIC_OVERRIDES,
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/home",
        "LANG": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "1",
        "GIT_TERMINAL_PROMPT": "1",
        "GIT_NO_REPLACE_OBJECTS": "0",
    }
    monkeypatch.setattr(os, "environ", parent)
    before = dict(parent)

    assert local_git_environment() == {
        "PATH": "/synthetic/bin",
        "HOME": "/synthetic/home",
        "LANG": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    assert parent == before


def test_importer_and_doctor_use_sanitized_git_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    setup_environment = local_git_environment()
    setup_environment.update(
        GIT_AUTHOR_NAME="Fixture Author",
        GIT_AUTHOR_EMAIL="original@example.test",
        GIT_COMMITTER_NAME="Fixture Committer",
        GIT_COMMITTER_EMAIL="committer@example.test",
    )

    def setup(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *args],
            check=True,
            capture_output=True,
            env=setup_environment,
            shell=False,
        )

    setup("init", "-q")
    setup("remote", "add", "origin", "https://example.test/fixture.git")
    (repository / ".mailmap").write_text(
        "Canonical Author <canonical@example.test> <original@example.test>\n",
        encoding="utf-8",
    )
    (repository / "fixture.txt").write_text("synthetic content\n", encoding="utf-8")
    (repository / ".gitattributes").write_text("*.txt diff=fixture\n", encoding="utf-8")
    setup("config", "diff.fixture.command", "false")
    setup("config", "diff.fixture.textconv", "false")
    setup("add", ".")
    setup("commit", "-q", "-m", "DEMO-1 synthetic change")

    for name, value in _SYNTHETIC_OVERRIDES.items():
        monkeypatch.setenv(name, value)
    for name, value in {
        "GIT_OPTIONAL_LOCKS": "1",
        "GIT_TERMINAL_PROMPT": "1",
        "GIT_NO_REPLACE_OBJECTS": "0",
    }.items():
        monkeypatch.setenv(name, value)

    run = subprocess.run
    commands: list[list[str]] = []

    def capture(command: list[str], **kwargs: Any) -> Any:
        environment = kwargs["env"]
        assert not (_SYNTHETIC_OVERRIDES.keys() & environment.keys())
        assert environment["GIT_OPTIONAL_LOCKS"] == "0"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert kwargs["shell"] is False
        commands.append(command)
        return run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", capture)
    adapter = LocalGitAdapter(
        LocalGitConfig(
            repository_path=repository,
            source_instance="fixture-local",
            app_id="fixture",
            email_key=b"synthetic-key",
            jira_project_keys=("DEMO",),
        )
    )
    pages = list(adapter.iter_pages())
    commit = next(page.records[0] for page in pages if page.resource_type == "commit")
    assert commit.payload["subject"] == "DEMO-1 synthetic change"
    assert any(actor.actor.display_name == "Canonical Author" for actor in commit.participations)
    assert _git(["rev-parse", "--show-toplevel"], repository).stdout.strip() == str(repository)
    assert _git(["config", "--get", "remote.origin.url"], repository).stdout.strip() == (
        "https://example.test/fixture.git"
    )
    assert _git(["--version"]).returncode == 0
    show_commands = [command for command in commands if "show" in command]
    assert show_commands
    assert all(
        "--no-ext-diff" in command and "--no-textconv" in command for command in show_commands
    )
    assert os.environ["WORKTRACE_JIRA_API_TOKEN"] == "synthetic-jira-token"
