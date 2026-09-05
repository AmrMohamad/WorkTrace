from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, Protocol, cast

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from typer.testing import CliRunner

from worktrace.adapters.base import (
    NormalizedPage,
    NormalizedRecord,
    UnavailableObjectDescriptor,
)
from worktrace.candidates.decisions import append_decision
from worktrace.cli import app
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.importers.orchestrator import import_snapshot
from worktrace.mcp_server.tools import WorkTraceTools
from worktrace.normalize.records import build_record
from worktrace.normalize.redaction import Redactor
from worktrace.packets.builder import build_phase4_packet
from worktrace.services import export_app


def _run(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _repository_config(
    tmp_path: Path,
    repository: Path,
    *,
    providers: bool = False,
) -> Path:
    provider_identity = ""
    provider_scope = ""
    if providers:
        provider_identity = """
jira_account_id = "account-7"
gitlab_user_id = 7
gitlab_username = "fixture"
"""
        provider_scope = """
jira_project_keys = ["DEMO"]
gitlab_project_ids = [101]
production_environments = ["production"]
"""
    config = tmp_path / "config.toml"
    config.write_text(
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
{provider_identity}
[[apps]]
id = "sample_store"
name = "Sample Store"
repo_paths = [{str(repository)!r}]
{provider_scope}
""",
        encoding="utf-8",
    )
    return config


class _Response:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://provider.example.test/resource")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("fixture failure", request=request, response=response)


class _DoctorClient:
    requests: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, *, base_url: str, **_: object) -> None:
        self.base_url = base_url

    def __enter__(self) -> _DoctorClient:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get(self, path: str) -> _Response:
        self.requests.append((self.base_url, path))
        if path == "/rest/api/3/myself":
            return _Response(200, {"accountId": "account-7"})
        if path == "/api/v4/user":
            return _Response(200, {"id": 7, "username": "fixture"})
        if path in ("/rest/api/3/project/DEMO", "/api/v4/projects/101"):
            return _Response(200, {})
        return _Response(404, {})


def test_doctor_live_checks_provider_identity_and_project_visibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _run("git", "init", "-q", str(repository), cwd=tmp_path)
    config = _repository_config(tmp_path, repository, providers=True)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0

    credentials = {
        "WORKTRACE_JIRA_BASE_URL": "https://jira.example.test",
        "WORKTRACE_JIRA_EMAIL": "fixture@example.test",
        "WORKTRACE_JIRA_API_TOKEN": "jira-secret-value",
        "WORKTRACE_GITLAB_BASE_URL": "https://gitlab.example.test",
        "WORKTRACE_GITLAB_TOKEN": "gitlab-secret-value",
    }
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    _DoctorClient.requests = []
    monkeypatch.setattr("worktrace.doctor.httpx.Client", _DoctorClient)

    result = runner.invoke(app, ["doctor", "--live", "--config", str(config)])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    live_checks = {
        (check["name"], check["scope"]): check["status"]
        for check in payload["checks"]
        if str(check["scope"]).startswith("live:")
    }
    assert live_checks == {
        ("jira_identity", "live:jira"): "pass",
        ("jira_project_visibility", "live:app:sample_store:jira:DEMO"): "pass",
        ("gitlab_identity", "live:gitlab"): "pass",
        ("gitlab_project_visibility", "live:app:sample_store:gitlab:101"): "pass",
    }
    assert _DoctorClient.requests == [
        ("https://jira.example.test", "/rest/api/3/myself"),
        ("https://jira.example.test", "/rest/api/3/project/DEMO"),
        ("https://gitlab.example.test", "/api/v4/user"),
        ("https://gitlab.example.test", "/api/v4/projects/101"),
    ]
    assert all(secret not in result.stdout for secret in credentials.values())


def _app_config() -> AppConfig:
    return AppConfig(
        id="sample_store",
        name="Sample Store",
        market="XX",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(),
        repo_paths=(),
        jira_key_patterns=(r"DEMO-[0-9]+",),
        production_environments=(),
        release_tag_patterns=(),
        ignored_paths=(),
    )


def _record(title: str) -> NormalizedRecord:
    return build_record(
        source_kind="jira",
        source_instance="jira-main",
        object_type="issue",
        external_id="10001",
        app_id="sample_store",
        observed_at="2026-01-11T12:00:00Z",
        source_updated_at="2026-01-10T10:00:00Z",
        payload={"title": title, "body": "bounded fixture", "key": "DEMO-1"},
        redactor=Redactor(email_key=b"fixture-only-key"),
    )


@dataclass
class _Adapter:
    page: NormalizedPage

    def iter_pages(self) -> Iterator[NormalizedPage]:
        yield self.page


def _page(
    *,
    records: tuple[NormalizedRecord, ...] = (),
    unavailable: tuple[UnavailableObjectDescriptor, ...] = (),
) -> NormalizedPage:
    return NormalizedPage(
        source_kind="jira",
        source_instance="jira-main",
        resource_type="issues",
        cursor=None,
        next_cursor=None,
        is_last=True,
        records=records,
        unavailable_objects=unavailable,
    )


def test_exact_object_unavailable_then_reappearance_is_append_only(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "ledger.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, '', '')",
            ("sample_store", "Sample Store"),
        )
        repository = EvidenceRepository(connection)
        app_config = _app_config()
        kwargs = {
            "source": "jira",
            "source_instance": "jira-main",
            "date_from": date(2026, 1, 1),
            "date_to": date(2026, 1, 31),
        }

        first = import_snapshot(
            app_config,
            _Adapter(_page(records=(_record("Visible first"),))),
            repository,
            **kwargs,
        )
        missing = import_snapshot(
            app_config,
            _Adapter(
                _page(
                    unavailable=(
                        UnavailableObjectDescriptor(kind="jira_issue", external_id="10001"),
                    )
                )
            ),
            repository,
            **kwargs,
        )
        object_id = str(connection.execute("SELECT id FROM source_objects").fetchone()[0])
        contribution_id = "contribution:availability-fixture"
        append_decision(
            connection,
            "confirm_candidate",
            "candidate:availability-fixture",
            {
                "contribution_id": contribution_id,
                "app_id": "sample_store",
                "title": "Availability fixture",
                "type": "unknown",
                "members": [object_id],
                "context_members": [],
            },
        )
        packet_config = WorkTraceConfig(
            schema_version=1,
            data_directory=tmp_path,
            employment_from=date(2024, 1, 1),
            employment_to=date(2026, 12, 31),
            identity=IdentityConfig("Fixture", (), (), None, None, None),
            apps=(app_config,),
            config_path=tmp_path / "fixture.toml",
        )
        unavailable_packet = build_phase4_packet(connection, contribution_id, packet_config)
        summary = cast(dict[str, object], unavailable_packet["evidence_summary"])
        members = cast(list[dict[str, object]], summary["members"])
        assert summary["unsupported_member_ids"] == [object_id]
        assert members == []
        contradictions = cast(list[dict[str, object]], summary["contradictions"])
        unavailable_contradiction = next(
            item for item in contradictions if item["kind"] == "source_unavailable"
        )
        assert str(unavailable_contradiction["evidence_ids"][0]).startswith("availability:")
        returned = import_snapshot(
            app_config,
            _Adapter(_page(records=(_record("Visible again"),))),
            repository,
            **kwargs,
        )

        assert first.status == missing.status == returned.status == "complete"
        state = connection.execute(
            "SELECT availability, availability_reason FROM source_objects"
        ).fetchone()
        assert tuple(state) == ("visible", "reappeared")
        events = connection.execute(
            """
            SELECT state, reason FROM source_object_availability_events
            ORDER BY observed_at, rowid
            """
        ).fetchall()
        assert [tuple(event) for event in events] == [
            ("visible", "observed"),
            ("unavailable", "not_found"),
            ("visible", "reappeared"),
        ]
        current = repository.current_observations("sample_store")
        assert len(current) == 1
        assert current[0]["title"] == "Visible again"
        assert connection.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 2
    finally:
        connection.close()


def _insert_export_observation(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    object_id: str,
    external_id: str,
    title: str,
    completed_at: str,
    selection_policy_version: int,
) -> None:
    execute = connection.execute
    execute(
        """
        INSERT INTO sync_runs(
            id, app_id, source, source_instance, status, started_at, completed_at,
            adapter_version, scope_json, completeness
        ) VALUES (?, 'sample_store', 'jira', 'jira-main', 'complete', ?, ?,
                  'fixture', ?, 'complete_for_scope')
        """,
        (
            run_id,
            completed_at,
            completed_at,
            json.dumps(
                {
                    "date_from": "2024-01-01",
                    "date_to": "2026-12-31",
                    "selection_policy_version": selection_policy_version,
                }
            ),
        ),
    )
    execute(
        """
        INSERT INTO source_objects(
            id, app_id, source, source_instance, kind, external_id,
            first_seen_run_id, last_seen_run_id
        ) VALUES (?, 'sample_store', 'jira', 'jira-main', 'jira_issue', ?, ?, ?)
        """,
        (object_id, external_id, run_id, run_id),
    )
    execute(
        """
        INSERT INTO observations(
            id, source_object_id, sync_run_id, fetched_at, payload_hash, title,
            data_json, completeness, adapter_version, normalization_version,
            redaction_version
        ) VALUES (?, ?, ?, ?, ?, ?, '{}', 'complete_for_scope', 'fixture', '2', '1')
        """,
        (f"obs:{external_id}", object_id, run_id, completed_at, f"hash:{external_id}", title),
    )


def test_export_excludes_newer_obsolete_selection_policy_run(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger.sqlite3"
    connection = connect(database_path)
    try:
        migrate(connection, database_path)
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, '', '')",
            ("sample_store", "Sample Store"),
        )
        _insert_export_observation(
            connection,
            run_id="run:scoped",
            object_id="obj:scoped",
            external_id="10001",
            title="Scoped evidence",
            completed_at="2026-01-01T00:00:00+00:00",
            selection_policy_version=2,
        )
        _insert_export_observation(
            connection,
            run_id="run:obsolete",
            object_id="obj:obsolete",
            external_id="10002",
            title="Unrelated whole-project evidence",
            completed_at="2026-02-01T00:00:00+00:00",
            selection_policy_version=1,
        )
        connection.commit()

        destination = tmp_path / "export.json"
        assert export_app(connection, "sample_store", destination) == 1
        payload = json.loads(destination.read_text(encoding="utf-8"))

        assert [row["id"] for row in payload["sync_runs"]] == ["run:scoped"]
        assert [row["id"] for row in payload["source_objects"]] == ["obj:scoped"]
        assert [row["id"] for row in payload["observations"]] == ["obs:10001"]
        serialized = json.dumps(payload, sort_keys=True)
        assert "Unrelated whole-project evidence" not in serialized
        assert "run:obsolete" not in serialized
    finally:
        connection.close()


def test_cli_git_only_journey_reaches_packet_gaps_and_mcp_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _run("git", "init", "-q", str(repository), cwd=tmp_path)
    _run("git", "config", "user.name", "Fixture Engineer", cwd=repository)
    _run("git", "config", "user.email", "fixture@example.test", cwd=repository)
    (repository / "checkout.py").write_text("print('bounded fixture')\n", encoding="utf-8")
    _run("git", "add", "checkout.py", cwd=repository)
    _run("git", "commit", "-q", "-m", "DEMO-1 implement checkout fixture", cwd=repository)
    config = _repository_config(tmp_path, repository)
    runner = CliRunner()

    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, initialized.stdout
    imported = runner.invoke(
        app,
        [
            "import",
            "all",
            "sample_store",
            "2024-01-01",
            "2026-12-31",
            "--config",
            str(config),
        ],
    )
    assert imported.exit_code == 0, imported.stdout
    import_payload = json.loads(imported.stdout)
    assert import_payload["status"] == "complete"
    assert len(import_payload["sources"]) == 1
    assert import_payload["sources"][0]["status"] == "complete"
    assert import_payload["candidate_count"] >= 1

    candidates = runner.invoke(app, ["candidates", "list", "sample_store", "--config", str(config)])
    assert candidates.exit_code == 0, candidates.stdout
    candidate_id = json.loads(candidates.stdout)[0]["id"]
    shown = runner.invoke(app, ["candidates", "show", candidate_id, "--config", str(config)])
    assert shown.exit_code == 0, shown.stdout
    object_id = json.loads(shown.stdout)["members"][0]["source_object_id"]
    status = runner.invoke(app, ["status", "sample_store", "--config", str(config)])
    search = runner.invoke(app, ["search", "sample_store", "checkout", "--config", str(config)])
    assert status.exit_code == search.exit_code == 0
    assert json.loads(search.stdout)
    confirmed = runner.invoke(app, ["confirm", candidate_id, "--config", str(config)])
    assert confirmed.exit_code == 0, confirmed.stdout
    contribution_id = json.loads(confirmed.stdout)["contribution_id"]

    packet = runner.invoke(app, ["packet", contribution_id, "--config", str(config)])
    gaps = runner.invoke(app, ["gaps", contribution_id, "--config", str(config)])
    assert packet.exit_code == 0, packet.stdout
    assert gaps.exit_code == 0, gaps.stdout
    packet_payload = json.loads(packet.stdout)
    assert packet_payload["schema_version"] == 2
    assert packet_payload["contribution"]["id"] == contribution_id
    assert sum(len(questions) for questions in packet_payload["sections"].values()) == 30
    assert isinstance(json.loads(gaps.stdout), dict)

    ignored = runner.invoke(
        app, ["ignore", candidate_id, "--reason", "fixture reason", "--config", str(config)]
    )
    assert ignored.exit_code == 0, ignored.stdout
    rejected_ignored = runner.invoke(
        app, ["split", candidate_id, object_id, "--config", str(config)]
    )
    assert rejected_ignored.exit_code != 0
    restored_ignore = runner.invoke(
        app, ["undo", json.loads(ignored.stdout)["decision_id"], "--config", str(config)]
    )
    assert restored_ignore.exit_code == 0, restored_ignore.stdout

    decision_commands = (
        ["rename", candidate_id, "Checkout contribution", "--config", str(config)],
        ["add-member", candidate_id, object_id, "--config", str(config)],
        ["remove-member", candidate_id, object_id, "--config", str(config)],
    )
    decision_ids: list[str] = []
    for command in decision_commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, f"{command}: {result.stdout} {result.stderr}"
        decision_ids.append(json.loads(result.stdout)["decision_id"])
    rejected_removed = runner.invoke(
        app, ["split", candidate_id, object_id, "--config", str(config)]
    )
    assert rejected_removed.exit_code != 0
    restored_remove = runner.invoke(app, ["undo", decision_ids[-1], "--config", str(config)])
    assert restored_remove.exit_code == 0, restored_remove.stdout
    split = runner.invoke(app, ["split", candidate_id, object_id, "--config", str(config)])
    assert split.exit_code == 0, split.stdout
    decision_ids.append(json.loads(split.stdout)["decision_id"])
    attested = runner.invoke(
        app,
        [
            "attest",
            contribution_id,
            "currently_enabled",
            "Fixture attestation",
            "--config",
            str(config),
        ],
    )
    assert attested.exit_code == 0, attested.stdout
    decision_ids.append(json.loads(attested.stdout)["decision_id"])
    undone = runner.invoke(app, ["undo", decision_ids[-1], "--config", str(config)])
    assert undone.exit_code == 0, undone.stdout

    manual = runner.invoke(
        app,
        [
            "evidence",
            "add",
            "sample_store",
            "Measured fixture",
            "Metric stayed bounded",
            "--config",
            str(config),
        ],
    )
    assert manual.exit_code == 0, manual.stdout
    for rebuild_command in ("references", "candidates", "all"):
        rebuilt = runner.invoke(
            app,
            ["rebuild", rebuild_command, "sample_store", "--config", str(config)],
        )
        assert rebuilt.exit_code == 0, rebuilt.stdout

    exported_path = tmp_path / "cli-export.json"
    exported = runner.invoke(
        app,
        ["export", "sample_store", str(exported_path), "--config", str(config)],
    )
    backup_path = tmp_path / "cli-ledger.backup"
    backed_up = runner.invoke(
        app,
        ["backup", "--destination", str(backup_path), "--config", str(config)],
    )
    assert exported.exit_code == backed_up.exit_code == 0
    assert exported_path.exists() and backup_path.exists()

    observed: list[Path | None] = []
    monkeypatch.setattr("worktrace.mcp_server.server.run", observed.append)
    mcp = runner.invoke(app, ["serve-mcp", "--config", str(config)])
    assert mcp.exit_code == 0, mcp.stdout
    assert observed == [config]


def test_import_all_uses_git_then_gitlab_then_jira_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _run("git", "init", "-q", str(repository), cwd=tmp_path)
    config = _repository_config(tmp_path, repository, providers=True)
    runner = CliRunner()
    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0

    constructed: list[str] = []

    class AdapterConfigView(Protocol):
        source_instance: str

    class EmptyAdapter:
        def __init__(self, adapter_config: object, *_: object) -> None:
            name = type(adapter_config).__name__
            source = {
                "LocalGitConfig": "git",
                "GitLabConfig": "gitlab",
                "JiraConfig": "jira",
            }[name]
            constructed.append(source)
            self.source = source
            self.source_instance = str(cast(AdapterConfigView, adapter_config).source_instance)

        def resolved_self_ids(self, configured_email_hashes: set[str]) -> set[str]:
            return {"7", *configured_email_hashes}

        def resolved_self_id(self) -> str:
            return "account-7"

        def iter_pages(self) -> Iterator[NormalizedPage]:
            yield NormalizedPage(
                source_kind=self.source,
                source_instance=self.source_instance,
                resource_type="fixture",
                cursor=None,
                next_cursor=None,
                is_last=True,
                records=(),
            )

    class ContextClient:
        def __init__(self, **_: object) -> None:
            pass

        def __enter__(self) -> ContextClient:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    monkeypatch.setattr("worktrace.cli.LocalGitAdapter", EmptyAdapter)
    monkeypatch.setattr("worktrace.cli.GitLabAdapter", EmptyAdapter)
    monkeypatch.setattr("worktrace.cli.JiraAdapter", EmptyAdapter)
    monkeypatch.setattr("worktrace.cli.httpx.Client", ContextClient)
    monkeypatch.setattr(
        "worktrace.cli.gitlab_credentials",
        lambda: SimpleNamespace(base_url="https://gitlab.example.test", token="fixture"),
    )
    monkeypatch.setattr(
        "worktrace.cli.jira_credentials",
        lambda: SimpleNamespace(
            base_url="https://jira.example.test", email="fixture@example.test", token="fixture"
        ),
    )

    result = runner.invoke(
        app,
        [
            "import",
            "all",
            "sample_store",
            "2024-01-01",
            "2026-12-31",
            "--config",
            str(config),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert constructed == ["git", "gitlab", "jira"]
    assert len(json.loads(result.stdout)["sources"]) == 3


@pytest.mark.asyncio
async def test_mcp_stdio_initializes_and_lists_exactly_seven_read_only_tools(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _run("git", "init", "-q", str(repository), cwd=tmp_path)
    config = _repository_config(tmp_path, repository)
    initialized = CliRunner().invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, initialized.stdout

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "worktrace", "serve-mcp", "--config", str(config)],
        cwd=Path(__file__).parents[1],
    )
    async with (
        stdio_client(parameters) as (read_stream, write_stream),
        ClientSession(read_stream, write_stream) as session,
    ):
        result = await session.initialize()
        tools = await session.list_tools()

    assert result.server_info.name == "WorkTrace"
    assert {tool.name for tool in tools.tools} == {
        "list_contribution_candidates",
        "get_contribution_summary",
        "build_phase4_packet",
        "list_evidence_gaps",
        "search_evidence",
        "get_evidence_excerpt",
        "get_evidence_context",
    }


def test_confirmed_contribution_projects_post_confirm_cli_decisions_and_undo(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "decision-lineage-repository"
    _run("git", "init", "-q", str(repository), cwd=tmp_path)
    _run("git", "config", "user.name", "Fixture Engineer", cwd=repository)
    _run("git", "config", "user.email", "fixture@example.test", cwd=repository)
    (repository / "first.py").write_text("FIRST = True\n", encoding="utf-8")
    _run("git", "add", "first.py", cwd=repository)
    _run("git", "commit", "-q", "-m", "implement first fixture", cwd=repository)
    (repository / "second.py").write_text("SECOND = True\n", encoding="utf-8")
    _run("git", "add", "second.py", cwd=repository)
    _run("git", "commit", "-q", "-m", "implement second fixture", cwd=repository)
    config = _repository_config(tmp_path, repository)
    runner = CliRunner()

    assert runner.invoke(app, ["init", "--config", str(config)]).exit_code == 0
    imported = runner.invoke(
        app,
        [
            "import",
            "all",
            "sample_store",
            "2024-01-01",
            "2026-12-31",
            "--config",
            str(config),
        ],
    )
    assert imported.exit_code == 0, imported.stdout
    listed = runner.invoke(app, ["candidates", "list", "sample_store", "--config", str(config)])
    assert listed.exit_code == 0, listed.stdout
    candidate_ids = [str(item["id"]) for item in json.loads(listed.stdout)]
    assert len(candidate_ids) == 2
    primary_id, secondary_id = candidate_ids
    primary = runner.invoke(app, ["candidates", "show", primary_id, "--config", str(config)])
    secondary = runner.invoke(app, ["candidates", "show", secondary_id, "--config", str(config)])
    assert primary.exit_code == secondary.exit_code == 0
    primary_object_ids = {
        str(member["source_object_id"]) for member in json.loads(primary.stdout)["members"]
    }
    secondary_object_id = str(json.loads(secondary.stdout)["members"][0]["source_object_id"])

    confirmed = runner.invoke(app, ["confirm", primary_id, "--config", str(config)])
    assert confirmed.exit_code == 0, confirmed.stdout
    contribution_id = str(json.loads(confirmed.stdout)["contribution_id"])
    renamed = runner.invoke(
        app,
        [
            "rename",
            primary_id,
            "Canonical confirmed contribution",
            "--config",
            str(config),
        ],
    )
    added = runner.invoke(
        app,
        ["add-member", primary_id, secondary_object_id, "--config", str(config)],
    )
    removed = runner.invoke(
        app,
        ["remove-member", primary_id, secondary_object_id, "--config", str(config)],
    )
    assert renamed.exit_code == added.exit_code == removed.exit_code == 0

    tools = WorkTraceTools(config_path=config)
    candidate_after_remove = tools.get_contribution_summary(contribution_id=primary_id)
    contribution_after_remove = tools.get_contribution_summary(contribution_id=contribution_id)
    assert candidate_after_remove["contribution"]["title"] == ("Canonical confirmed contribution")
    assert contribution_after_remove["contribution"]["title"] == (
        "Canonical confirmed contribution"
    )
    assert {item["object_id"] for item in candidate_after_remove["members"]} == (primary_object_ids)
    assert {item["object_id"] for item in contribution_after_remove["members"]} == (
        primary_object_ids
    )

    remove_decision_id = str(json.loads(removed.stdout)["decision_id"])
    undone = runner.invoke(app, ["undo", remove_decision_id, "--config", str(config)])
    assert undone.exit_code == 0, undone.stdout
    candidate_after_undo = tools.get_contribution_summary(contribution_id=primary_id)
    contribution_after_undo = tools.get_contribution_summary(contribution_id=contribution_id)
    expected_members = primary_object_ids | {secondary_object_id}
    assert {item["object_id"] for item in candidate_after_undo["members"]} == expected_members
    assert {item["object_id"] for item in contribution_after_undo["members"]} == expected_members
    assert (
        contribution_after_undo["contribution"]["title"]
        == (candidate_after_undo["contribution"]["title"])
    )
    candidate_list = tools.list_contribution_candidates(app_id="sample_store")
    list_item = next(
        item for item in candidate_list["candidates"] if item["candidate_id"] == primary_id
    )
    assert list_item["confirmed_contribution_id"] == contribution_id
    assert list_item["title"] == "Canonical confirmed contribution"
    packet = tools.build_phase4_packet(contribution_id=contribution_id)
    assert packet["contribution"]["title"] == "Canonical confirmed contribution"
    assert {item["object_id"] for item in packet["evidence_summary"]["members"]} == (
        expected_members
    )
