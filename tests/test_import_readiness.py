from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from tests.test_jira_history import JiraFixture, invoke
from tests.test_jira_history import workspace as workspace
from worktrace.cli import app
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.import_status import legacy_preflight_ids, readiness_contract, source_readiness
from worktrace.db.queries import source_status
from worktrace.db.repository import EvidenceRepository, stable_id
from worktrace.errors import WorkTraceError
from worktrace.packets.builder import PacketBuilder


@pytest.mark.parametrize("command", ["jira", "all"])
def test_missing_credentials_are_session_preflight_only(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    for name in ("BASE_URL", "EMAIL", "API_TOKEN"):
        monkeypatch.delenv("WORKTRACE_JIRA_" + name)
    output = invoke(workspace, "import", command, "sample", success=False)
    attempt = output["sources"][0] if command == "all" else output
    assert attempt["status"] == "not_started"
    assert attempt["reason"] == "credentials_missing"
    assert attempt["source_instance"] is None
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0
        audit = json.loads(
            connection.execute("SELECT summary_json FROM import_sessions").fetchone()[0]
        )
        assert audit["sources"][0]["preflight"]["status"] == "not_started"
        cli = source_status(connection, "sample")[0]
        mcp = PacketBuilder(connection, config).source_status("sample")["jira"]
        assert cli["preflight"] == mcp["preflight"]
        assert mcp["last_authoritative_snapshots"] == []


@pytest.mark.parametrize("failure", ["mismatch", "unauthorized", "invalid_origin"])
def test_provider_identity_and_origin_are_established_before_run(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    if failure == "invalid_origin":
        monkeypatch.setenv("WORKTRACE_JIRA_BASE_URL", "http://jira.example")
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get("https://jira.example/rest/api/3/myself").mock(
            return_value=httpx.Response(
                401 if failure == "unauthorized" else 200, json={"accountId": "other"}
            )
        )
        output = invoke(workspace, "import", "jira", "sample", success=False)
        assert output["status"] == "not_started"
        assert output["reason"] in {"origin_invalid", "identity_unverified"}
        assert route.called == (failure != "invalid_origin")
    with connect(load_config(workspace).database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 0


def test_later_success_clears_current_preflight_and_retains_audit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WORKTRACE_JIRA_API_TOKEN")
    failed = invoke(workspace, "import", "jira", "sample", success=False)
    monkeypatch.setenv("WORKTRACE_JIRA_API_TOKEN", "synthetic-fixture-token")
    with respx.mock() as mock:
        mock.route().mock(side_effect=JiraFixture())
        successful = invoke(workspace, "import", "jira", "sample")
    assert successful["coverage"] == "limited"
    assert successful["jira_seed_selection"]["omitted_count"] == 0
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        state = source_readiness(connection, "sample")["jira"]
        assert state["preflight"][0]["status"] == "ready"
        assert state["preflight"][0]["session_id"] == successful["session_id"]
        assert state["last_authoritative_snapshots"][0]["run_id"] == successful["run_id"]
        assert (
            state["last_authoritative_snapshots"][0]["jira_seed_selection"]
            == successful["jira_seed_selection"]
        )
        old = connection.execute(
            "SELECT summary_json FROM import_sessions WHERE id=?", (failed["session_id"],)
        ).fetchone()
        assert json.loads(old[0])["sources"][0]["status"] == "not_started"
        # A subsequent preflight failure does not replace the successful source snapshot.
    monkeypatch.delenv("WORKTRACE_JIRA_API_TOKEN")
    invoke(workspace, "import", "jira", "sample", success=False)
    with connect(config.database_path) as connection:
        state = source_readiness(connection, "sample")["jira"]
        assert state["preflight"][0]["status"] == "not_started"
        assert state["last_authoritative_snapshots"][0]["run_id"] == successful["run_id"]


def test_all_continues_jira_after_missing_gitlab_preflight(workspace: Path) -> None:
    text = workspace.read_text().replace(
        'jira_project_keys = ["DEMO"]', 'jira_project_keys = ["DEMO"]\ngitlab_project_ids = [101]'
    )
    workspace.write_text(text)
    with respx.mock() as mock:
        mock.route().mock(side_effect=JiraFixture())
        output = invoke(workspace, "import", "all", "sample", success=False)
    assert [(item["source"], item["status"]) for item in output["sources"]] == [
        ("gitlab", "not_started"),
        ("jira", "complete"),
    ]
    with connect(load_config(workspace).database_path) as connection:
        assert [row[0] for row in connection.execute("SELECT source FROM sync_runs")] == ["jira"]
        scope = json.loads(connection.execute("SELECT scope_json FROM sync_runs").fetchone()[0])
        assert scope["seed_input_authority"][0]["status"] == "not_started"


def test_legacy_placeholder_recognition_is_exact_and_read_only(workspace: Path) -> None:
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        repository = EvidenceRepository(connection)
        synthetic = stable_id("source", "sample", "jira", "configured")
        expected = repository.start_sync_run("sample", "jira", synthetic, {})
        repository.finish_sync_run(
            expected, "failed", "source_unavailable", "Jira credentials are not configured"
        )
        real = repository.start_sync_run("sample", "jira", "real-jira", {})
        repository.finish_sync_run(
            real, "failed", "source_unavailable", "Jira credentials are not configured"
        )
        other_error = repository.start_sync_run("sample", "jira", synthetic, {})
        repository.finish_sync_run(
            other_error, "failed", "source_unavailable", "Provider request failed"
        )
        assert legacy_preflight_ids(connection, "sample") == {expected}
        state = source_readiness(connection, "sample")["jira"]
        assert state["legacy_preflight_audit"] == [
            {"run_id": expected, "reason": "credentials_missing"}
        ]
        rows = source_status(connection, "sample")
        assert {row["source_instance"] for row in rows} == {synthetic, "real-jira"}
        assert connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0] == 3


def test_snapshot_readiness_is_instance_specific_and_legacy_coverage_is_labelled(
    workspace: Path,
) -> None:
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        repository = EvidenceRepository(connection)
        for source, instance in (("gitlab", "project-b"), ("jira", "old-jira")):
            run = repository.start_sync_run(
                "sample", source, instance, {"selection_policy_version": 2}
            )
            repository.finish_sync_run(run, "complete", "complete_for_scope")
        other = readiness_contract(
            connection, "sample", "gitlab", "partial", "partial", source_instance="project-a"
        )
        assert other["snapshot_state"] == "unavailable"
        same = readiness_contract(
            connection, "sample", "gitlab", "partial", "partial", source_instance="project-b"
        )
        assert same["snapshot_state"] == "previous_retained"
        missing_origin = readiness_contract(
            connection, "sample", "gitlab", "not_started", "unknown"
        )
        assert missing_origin["snapshot_state"] == "unavailable"
        snapshot = source_readiness(connection, "sample")["jira"]["last_authoritative_snapshots"][0]
        assert snapshot["selector_policy"] == "legacy"
        assert snapshot["coverage"] == "unknown"
        assert source_status(connection, "sample")[1]["authoritative_current"] is True


def test_placeholder_like_run_with_observations_remains_real_health(workspace: Path) -> None:
    with respx.mock() as mock:
        mock.route().mock(side_effect=JiraFixture())
        output = invoke(workspace, "import", "jira", "sample")
    with connect(load_config(workspace).database_path) as connection:
        connection.execute(
            "UPDATE sync_runs SET source_instance=?, status='failed', "
            "completeness='source_unavailable', "
            "error_summary='Jira credentials are not configured' WHERE id=?",
            (stable_id("source", "sample", "jira", "configured"), output["run_id"]),
        )
        assert legacy_preflight_ids(connection, "sample") == set()
        assert source_status(connection, "sample")[0]["source_instance"] is not None


def test_rebuild_failure_finalizes_session_audit(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: object) -> None:
        raise WorkTraceError("synthetic rebuild failure")

    monkeypatch.setattr("worktrace.cli.rebuild_references", fail)
    with respx.mock() as mock:
        mock.route().mock(side_effect=JiraFixture())
        result = CliRunner().invoke(app, ["import", "all", "sample", "--config", str(workspace)])
        assert result.exit_code != 0
    with connect(load_config(workspace).database_path) as connection:
        session = connection.execute("SELECT * FROM import_sessions").fetchone()
        assert session["status"] == "partial"
        assert session["completed_at"] is not None
        audit = json.loads(session["summary_json"])
        assert audit["sources"][0]["status"] == "complete"
        assert audit["error"] == "source_or_rebuild_failed"
