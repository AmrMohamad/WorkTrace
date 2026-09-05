from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from typer.testing import CliRunner

from worktrace.adapters.jira import JiraAdapter, JiraConfig
from worktrace.cli import app
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import ConfigurationError
from worktrace.importers.orchestrator import import_snapshot
from worktrace.packets.activity import activity_period
from worktrace.packets.builder import PacketBuilder
from worktrace.packets.models import EvidenceRecord


def issue(number: int = 1, created: str | None = "2023-01-01T00:00:00Z") -> dict:
    return {
        "id": str(number),
        "key": f"DEMO-{number}",
        "fields": {
            "project": {"key": "DEMO"},
            "summary": "Historical investigation",
            "created": created,
            "updated": "2026-09-01T00:00:00Z",
            "creator": {"accountId": "colleague"},
            "assignee": {"accountId": "colleague"},
        },
    }


def history(number: int, at: str, before: str | None, after: str | None) -> dict:
    return {
        "id": str(number),
        "created": at,
        "author": {"accountId": "colleague"},
        "items": [{"field": "assignee", "from": before, "to": after}],
    }


class JiraFixture:
    def __init__(self, issues: list[dict] | None = None) -> None:
        self.issues = [issue()] if issues is None else issues
        self.comments: list[dict] = []
        self.histories: list[dict] = []
        self.queries: list[str] = []
        self.fail_discovery = False
        self.different_duplicate = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/myself"):
            data = {"accountId": "self", "timeZone": "America/New_York"}
        elif path.endswith("/search/jql"):
            query = json.loads(request.content)["jql"]
            self.queries.append(query)
            if self.fail_discovery:
                data = {"issues": self.issues, "isLast": False, "nextPageToken": "repeated"}
            else:
                data = {"issues": self.issues, "isLast": True}
                if self.different_duplicate and "assignee WAS" in query:
                    changed = json.loads(json.dumps(self.issues))
                    changed[0]["fields"]["summary"] = "Different later version"
                    data["issues"] = changed
        elif path.endswith("/comment"):
            data = {
                "comments": self.comments,
                "startAt": 0,
                "maxResults": 100,
                "total": len(self.comments),
            }
        elif path.endswith("/changelog"):
            data = {
                "values": self.histories,
                "startAt": 0,
                "maxResults": 100,
                "total": len(self.histories),
                "isLast": True,
            }
        else:
            raise AssertionError(f"Unexpected provider path: {path}")
        return httpx.Response(200, json=data)


def invoke(config: Path, *args: str, success: bool = True) -> dict:
    result = CliRunner().invoke(app, [*args, "--config", str(config)])
    if success:
        assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    else:
        assert result.exit_code != 0, result.output
    return json.loads(result.stdout) if result.stdout else {}


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in tuple(os.environ):
        if key.startswith("WORKTRACE_"):
            monkeypatch.delenv(key)
    for key, value in {
        "WORKTRACE_JIRA_BASE_URL": "https://jira.example",
        "WORKTRACE_JIRA_EMAIL": "synthetic@example.test",
        "WORKTRACE_JIRA_API_TOKEN": "synthetic-fixture-token",
    }.items():
        monkeypatch.setenv(key, value)
    config = tmp_path / "config.toml"
    config.write_text(f"""schema_version = 1
[data]
directory = {str(tmp_path / "data")!r}
[employment]
from = "2024-01-01"
to = "2024-01-31"
[identity]
display_name = "Fixture"
git_author_emails = ["synthetic@example.test"]
jira_account_id = "self"
[[apps]]
id = "sample"
name = "Sample"
jira_project_keys = ["DEMO"]
""")
    invoke(config, "init")
    return config


def search(
    builder: PacketBuilder,
    *,
    first: str | None = None,
    last: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    return builder.search_evidence(
        "",
        "sample",
        source_types=(),
        actor_id=None,
        module=None,
        date_from=first,
        date_to=last,
        limit=limit,
        offset=offset,
    )


def test_cli_late_edits_discovery_union_and_canonical_packet(workspace: Path) -> None:
    fixture = JiraFixture()
    fixture.different_duplicate = True
    fixture.comments = [
        {
            "id": "10",
            "created": "2024-01-10T12:00:00Z",
            "updated": "2026-09-01T00:00:00Z",
            "author": {"accountId": "self"},
            "updateAuthor": {"accountId": "colleague"},
            "body": "Current investigation wording synthetic@example.test",
        }
    ]
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
    assert len(fixture.queries) == 3
    assert all("updated <" not in query and "updated >=" not in query for query in fixture.queries)
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM jira_import_stage").fetchone()[0] == 0
        rows = list(connection.execute("SELECT id,data_json FROM observations"))
        values = [json.loads(row["data_json"]) for row in rows]
        ticket = next(value for value in values if value.get("key") == "DEMO-1")
        assert ticket["summary"] == "Historical investigation"
        assert set(ticket["selected_by"]) == {
            "historical_updater",
            "historical_assignee",
            "creator_created",
        }
        assert ticket["observation_semantics"] == "current_source_version"
        comment = next(value for value in values if value.get("issue_id") == "1")
        assert comment["creation_author_id"] == "self"
        assert comment["update_author_id"] == "colleague"
        assert comment["historical_wording_unknown"] is True
        assert "synthetic@example.test" not in json.dumps(values)
        scope = json.loads(connection.execute("SELECT scope_json FROM sync_runs").fetchone()[0])
        assert scope["work_timezone"] == "UTC"
        builder = PacketBuilder(connection, config)
        candidates = builder.list_candidates(
            "sample", date_from="2024-01-01", date_to="2024-01-31", limit=20, offset=0
        )
        assert len(candidates["candidates"]) == 1
        candidate = candidates["candidates"][0]
        assert candidate["period_from"].startswith("2024-01-10")
        assert candidate["period_to"].startswith("2024-01-10")
        packet = builder.build_packet(candidate["candidate_id"])
        questions = {q["question_id"]: q for group in packet["sections"].values() for q in group}
        assert len(questions) == 30
        assert questions["identity.when"]["status"] == "supported"
        assert questions["action.implemented"]["status"] == "unknown"
        assert questions["result.release"]["status"] == "unknown"
        found = search(builder, first="2024-01-10", last="2024-01-10")["results"]
        assert {row["kind"] for row in found} == {"jira_issue", "jira_issue_comment"}
        assert questions["identity.when"]["supporting_evidence_ids"]
        assert any(
            "first issue version" in text
            for text in builder.source_status("sample")["jira"]["instances"][0]["limitations"]
        )


def test_historical_match_without_surviving_events_stays_undated(workspace: Path) -> None:
    with respx.mock() as mock:
        mock.route().mock(side_effect=JiraFixture())
        invoke(workspace, "import", "all", "sample")
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        builder = PacketBuilder(connection, config)
        candidates = builder.list_candidates(
            "sample", date_from=None, date_to=None, limit=20, offset=0
        )
        assert len(candidates["candidates"]) == 1
        assert candidates["candidates"][0]["period_status"] == "unknown"
        assert search(builder)["results"][0]["date_from"] is None
        assert not search(builder, first="2024-01-01")["results"]
        assert not builder.list_candidates(
            "sample", date_from="2024-01-01", date_to=None, limit=20, offset=0
        )["candidates"]


@pytest.mark.parametrize("broken", [False, True])
def test_assignment_boundary_pairing_and_citations(workspace: Path, broken: bool) -> None:
    fixture = JiraFixture()
    fixture.histories = [
        history(2, "2024-02-10T00:00:00Z", "wrong" if broken else "self", "other"),
        history(1, "2023-12-10T00:00:00Z", "other", "self"),
    ]  # Provider order is deliberately reversed.
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        builder = PacketBuilder(connection, config)
        items = builder.list_candidates("sample", date_from=None, date_to=None, limit=20, offset=0)[
            "candidates"
        ]
        candidate = items[0]
        assert candidate["period_status"] == ("unknown" if broken else "known")
        if not broken:
            assert candidate["period_from"].startswith("2024-01-01")
            assert candidate["period_to"].startswith("2024-01-31")
            packet = builder.build_packet(candidate["candidate_id"])
            when = next(
                q
                for group in packet["sections"].values()
                for q in group
                if q["question_id"] == "identity.when"
            )
            history_ids = {
                str(row[0])
                for row in connection.execute(
                    "SELECT id FROM observations "
                    "WHERE json_extract(data_json,'$.boundary_context')=1"
                )
            }
            assert history_ids <= set(when["supporting_evidence_ids"])
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM candidate_members WHERE context_only=1"
            ).fetchone()[0]
            == 2
        )


def test_interrupted_discovery_retains_previous_authority_and_is_not_reused(
    workspace: Path,
) -> None:
    fixture = JiraFixture([issue(created="2024-01-02T00:00:00Z")])
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
        fixture.issues = [issue(2)]
        fixture.fail_discovery = True
        invoke(workspace, "import", "all", "sample", success=False)
        config = load_config(workspace)
        with connect(config.database_path) as connection:
            assert {
                r["external_id"] for r in search(PacketBuilder(connection, config))["results"]
            } == {"1"}
            assert connection.execute("SELECT COUNT(*) FROM jira_import_stage").fetchone()[0] == 1
        fixture.fail_discovery = False
        fixture.issues = [issue(3)]
        invoke(workspace, "import", "all", "sample")
    with connect(config.database_path) as connection:
        assert {r["external_id"] for r in search(PacketBuilder(connection, config))["results"]} == {
            "3"
        }
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_objects WHERE external_id='2'"
            ).fetchone()[0]
            == 0
        )


def test_date_filter_precedes_offset_pagination(workspace: Path) -> None:
    fixture = JiraFixture(
        [issue(n, "2024-01-15T00:00:00Z" if n % 2 else None) for n in range(1, 12)]
    )
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        builder = PacketBuilder(connection, config)
        offset, delivered = 0, set()
        while True:
            page = search(builder, first="2024-01-01", limit=2, offset=offset)
            ids = {r["external_id"] for r in page["results"]}
            assert not delivered & ids
            delivered |= ids
            assert page["date_filter_policy"] == "undated_excluded"
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert delivered == {str(n) for n in range(1, 12, 2)}


def test_exact_key_discovery_does_not_manufacture_self(workspace: Path) -> None:
    config = load_config(workspace)
    fixture = JiraFixture()
    with (
        connect(config.database_path) as connection,
        httpx.Client(
            base_url="https://jira.example", transport=httpx.MockTransport(fixture)
        ) as client,
    ):
        adapter = JiraAdapter(
            JiraConfig(
                "https://jira.example",
                "jira",
                "sample",
                ("DEMO",),
                b"test",
                date(2024, 1, 1),
                date(2024, 1, 31),
                discovered_issue_keys=("DEMO-1",),
            ),
            client,
        )
        result = import_snapshot(
            config.app("sample"),
            adapter,
            EvidenceRepository(connection),
            source="jira",
            source_instance="jira",
            date_from=config.employment_from,
            date_to=config.employment_to,
        )
        assert result.status == "complete"
        data = json.loads(connection.execute("SELECT data_json FROM observations").fetchone()[0])
        assert data["selected_by"] == ["exact_key"]
        assert connection.execute("SELECT COUNT(*) FROM actors WHERE is_self=1").fetchone()[0] == 0


def test_timezone_validation_and_dst_window(workspace: Path) -> None:
    assert load_config(workspace).employment_timezone == "UTC"
    text = workspace.read_text().replace(
        "[employment]", '[employment]\ntimezone="America/New_York"'
    )
    workspace.write_text(text)
    assert load_config(workspace).employment_timezone == "America/New_York"
    with httpx.Client(base_url="https://jira.example") as client:
        adapter = JiraAdapter(
            JiraConfig(
                "https://jira.example",
                "jira",
                "sample",
                ("DEMO",),
                b"test",
                date(2024, 3, 10),
                date(2024, 3, 10),
                work_timezone="America/New_York",
                account_id="self",
            ),
            client,
        )
        start, end = adapter.work_window
        assert end - start == timedelta(hours=23)
        assert adapter._subresource_in_window(
            {"created": "2024-03-11T03:59:59Z"}, ("created",), "comment"
        )
        assert not adapter._subresource_in_window(
            {"created": "2024-03-11T04:00:00Z"}, ("created",), "comment"
        )
    workspace.write_text(text.replace("America/New_York", "Invalid/Zone"))
    with pytest.raises(ConfigurationError, match="timezone"):
        load_config(workspace)


def test_activity_dates_do_not_use_freshness_or_context() -> None:
    record = EvidenceRecord(
        "obs:1",
        "obj:1",
        "sample",
        "git",
        "git",
        "git_commit",
        "sha",
        None,
        None,
        {},
        "complete",
        "2024-01-31T00:00:00Z",
        "2024-01-31T00:00:00Z",
        "visible",
        None,
        None,
        None,
        True,
    )
    dated = replace(
        record, data={"authored_at": "2024-01-02T00:00:00Z", "committed_at": "2024-01-03T00:00:00Z"}
    )
    context = replace(
        record,
        observation_id="obs:2",
        object_id="obj:2",
        context_only=True,
        data={"authored_at": "2024-01-20T00:00:00Z"},
    )

    def period(records):
        return activity_period(
            records,
            zone="UTC",
            first=date(2024, 1, 1),
            last=date(2024, 1, 31),
            children=lambda _: (),
        )

    assert period([record]).status == "unknown"
    known = period([dated, context])
    assert known.status == "known"
    assert known.fields()["date_to"].startswith("2024-01-03")
    assert known.evidence_ids == ("obs:1",)
    assert period([dated, replace(record, observation_id="obs:3")]).status == "partially_known"
