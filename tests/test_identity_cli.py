from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from typer.testing import CliRunner

from worktrace.cli import app
from worktrace.errors import DatabaseError
from worktrace.mcp_server.tools import WorkTraceTools


@pytest.mark.parametrize("schema_version", [3, 99])
def test_mcp_rejects_unsupported_schema_without_mutating_it(
    tmp_path: Path, schema_version: int
) -> None:
    config, _, database = _workspace(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA user_version={schema_version}")
    with pytest.raises(DatabaseError, match="unsupported database schema"):
        WorkTraceTools(config_path=config).list_contribution_candidates(app_id="sample_store")
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == schema_version


@pytest.fixture(autouse=True)
def _isolate_identity_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("WORKTRACE_"):
            monkeypatch.delenv(name)


def _git(repository: Path, *arguments: str, environment: dict[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.strip()


def _workspace(tmp_path: Path, *, providers: bool = False) -> tuple[Path, Path, Path]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    for index, (name, email, title) in enumerate(
        [
            ("Same Name", "colleague@example.test", "DEMO-1 collaborator change"),
            ("Changed Name", "old-work@example.test", "DEMO-2 historical alias change"),
        ],
        start=1,
    ):
        (repository / f"change{index}.txt").write_text(title, encoding="utf-8")
        _git(repository, "add", f"change{index}.txt")
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith("GIT_")
        }
        environment.update(
            GIT_AUTHOR_NAME=name,
            GIT_AUTHOR_EMAIL=email,
            GIT_COMMITTER_NAME="Integrator",
            GIT_COMMITTER_EMAIL="current@example.test",
            GIT_AUTHOR_DATE=f"2026-01-0{index}T10:00:00+00:00",
            GIT_COMMITTER_DATE=f"2026-01-0{index}T11:00:00+00:00",
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=os.devnull,
        )
        _git(
            repository,
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            title,
            environment=environment,
        )
    config = tmp_path / "config.toml"
    config.write_text(
        f"""schema_version = 1
[data]
directory = {str(tmp_path / "data")!r}
[employment]
from = "2026-01-01"
to = "2026-01-31"
[identity]
display_name = "Same Name"
git_author_emails = ["current@example.test", "old-work@example.test"]
git_author_names = ["Same Name"]
jira_account_id = "fixture-account"
gitlab_username = "fixture-engineer"
[[apps]]
id = "sample_store"
name = "Sample Store"
market = "XX"
business_type = "fixture"
jira_project_keys = {'["DEMO"]' if providers else "[]"}
gitlab_project_ids = {"[77]" if providers else "[]"}
repo_paths = [{str(repository)!r}]
""",
        encoding="utf-8",
    )
    _invoke(config, "init")
    return config, repository, tmp_path / "data" / "worktrace.sqlite3"


def _invoke(config: Path, *arguments: str) -> Any:
    result = CliRunner().invoke(app, [*arguments, "--config", str(config)])
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    return json.loads(result.stdout)


def _database_state(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        return "\n".join(connection.iterdump())


def test_public_git_import_preserves_exact_identity_and_packet_roles(tmp_path: Path) -> None:
    config, repository, database = _workspace(tmp_path)
    _invoke(config, "import", "git", "sample_store", str(repository))
    with sqlite3.connect(database) as connection:
        observed = connection.execute(
            "SELECT DISTINCT a.display_name,a.is_self,p.role FROM actors a "
            "JOIN participations p ON p.actor_id=a.id ORDER BY a.display_name,p.role"
        ).fetchall()
        unsupported_implementation = {
            row[0]
            for row in connection.execute(
                "SELECT p.id FROM participations p JOIN actors a ON a.id=p.actor_id "
                "WHERE a.is_self=0 OR p.role='git_committer'"
            )
        }
    assert ("Same Name", 0, "git_author") in observed
    assert ("Changed Name", 1, "git_author") in observed
    assert ("Integrator", 1, "git_committer") in observed
    _invoke(config, "rebuild", "all", "sample_store")
    candidates = _invoke(config, "candidates", "list", "sample_store")
    assert candidates
    packets = [_invoke(config, "packet", candidate["id"]) for candidate in candidates]
    supported_implementation: set[str] = set()
    for packet in packets:
        questions = {
            question["question_id"]: question
            for section in packet["sections"].values()
            for question in section
        }
        assert len(questions) == 30
        evidence = set(questions["action.implemented"]["supporting_evidence_ids"])
        assert not evidence.intersection(unsupported_implementation)
        supported_implementation.update(evidence)
        assert questions["result.business"]["status"] in {"unknown", "unresolved"}
        assert questions["result.business"]["answer_draft"] is None
    assert supported_implementation
    serialized = json.dumps(packets)
    assert "git_author" in serialized
    assert "action.implemented" in serialized
    assert "old-work@example.test" not in serialized
    assert "colleague@example.test" not in serialized


def test_init_with_populated_ledger_never_recreates_missing_key(tmp_path: Path) -> None:
    config, repository, database = _workspace(tmp_path)
    _invoke(config, "import", "git", "sample_store", str(repository))
    before = _database_state(database)
    key = database.parent / "email-hmac.key"
    key.unlink()
    result = CliRunner().invoke(app, ["init", "--config", str(config)])
    assert result.exit_code != 0
    assert not key.exists()
    assert _database_state(database) == before


def test_repair_cli_dry_run_stale_proposal_and_apply_preserve_evidence(tmp_path: Path) -> None:
    config, repository, database = _workspace(tmp_path)
    _invoke(config, "import", "git", "sample_store", str(repository))
    _invoke(config, "rebuild", "all", "sample_store")
    candidates = _invoke(config, "candidates", "list", "sample_store")
    assert candidates
    for candidate in candidates:
        _invoke(config, "confirm", candidate["id"])
    with sqlite3.connect(database) as connection:
        # Reproduce persisted legacy name-based classification, without fabricating discovery.
        connection.execute(
            "UPDATE actors SET is_self=1,identity_policy_version=0 WHERE display_name='Same Name'"
        )
        connection.execute(
            "UPDATE actors SET identity_policy_version=0 WHERE display_name='Changed Name'"
        )
        evidence_before = {
            table: connection.execute(f"SELECT id FROM {table} ORDER BY id").fetchall()
            for table in ("source_objects", "observations", "participations", "human_decisions")
        }
    before = _database_state(database)
    proposal = _invoke(config, "repair-identities", "sample_store")
    assert proposal["demotions"] == 1
    assert proposal["applied"] is False
    assert proposal["confirmed_targets"]
    assert _database_state(database) == before
    stale = CliRunner().invoke(
        app,
        [
            "repair-identities",
            "sample_store",
            "--apply",
            "--expected-proposal",
            "outdated-proposal",
            "--config",
            str(config),
        ],
    )
    assert stale.exit_code != 0
    assert "proposal changed" in f"{stale.output} {stale.exception}"
    assert _database_state(database) == before
    repaired = _invoke(
        config,
        "repair-identities",
        "sample_store",
        "--apply",
        "--expected-proposal",
        proposal["proposal_token"],
    )
    assert repaired["applied"] is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT is_self FROM actors WHERE display_name='Same Name'"
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM identity_repair_audit").fetchone() == (1,)
        for table, ids in evidence_before.items():
            assert connection.execute(f"SELECT id FROM {table} ORDER BY id").fetchall() == ids
    _invoke(config, "rebuild", "all", "sample_store")
    identity = _invoke(config, "status", "sample_store")["identity"]
    assert identity["valid"] is True
    assert identity["requires_rereview"]


def test_repair_verifies_providers_only_when_explicitly_requested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, repository, _ = _workspace(tmp_path, providers=True)
    _invoke(config, "import", "git", "sample_store", str(repository))
    for key, value in {
        "WORKTRACE_JIRA_BASE_URL": "https://jira.example",
        "WORKTRACE_JIRA_EMAIL": "fixture@example.test",
        "WORKTRACE_JIRA_API_TOKEN": "synthetic-jira-token",
        "WORKTRACE_GITLAB_BASE_URL": "https://gitlab.example",
        "WORKTRACE_GITLAB_TOKEN": "synthetic-gitlab-token",
    }.items():
        monkeypatch.setenv(key, value)
    with respx.mock(assert_all_called=False) as mocked:
        jira = mocked.get("https://jira.example/rest/api/3/myself").mock(
            return_value=httpx.Response(200, json={"accountId": "fixture-account"})
        )
        gitlab = mocked.get("https://gitlab.example/api/v4/user").mock(
            return_value=httpx.Response(
                200,
                json={"id": 42, "username": "fixture-engineer", "email": "current@example.test"},
            )
        )
        ordinary = _invoke(config, "repair-identities", "sample_store", "--dry-run")
        assert ordinary["verified_sources"] == []
        assert not mocked.calls
        verified = _invoke(config, "repair-identities", "sample_store", "--verify-providers")
        assert verified["verified_sources"] == ["gitlab", "jira"]
        assert jira.call_count == gitlab.call_count == 1
        assert len(mocked.calls) == 2
