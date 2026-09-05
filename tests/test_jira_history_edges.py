from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from tests.test_jira_history import JiraFixture, history, invoke, issue, search
from tests.test_jira_history import workspace as workspace
from worktrace.adapters.jira import JiraAdapter, JiraConfig
from worktrace.config import load_config
from worktrace.db.connection import connect
from worktrace.db.repository import EvidenceRepository
from worktrace.errors import ScopeViolation
from worktrace.importers.jira_staging import JiraStage
from worktrace.packets.builder import PacketBuilder


def imported_histories(workspace: Path, fixture: JiraFixture) -> list[dict]:
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
    with connect(load_config(workspace).database_path) as connection:
        return [
            json.loads(row[0])
            for row in connection.execute(
                "SELECT data_json FROM observations "
                "WHERE json_extract(data_json, '$.transitions') IS NOT NULL"
            )
        ]


@pytest.mark.parametrize("missing", ["start", "end"])
def test_missing_assignment_endpoint_remains_undated(workspace: Path, missing: str) -> None:
    fixture = JiraFixture()
    fixture.histories = [
        history(1, "2023-12-10T00:00:00Z", "other", "self")
        if missing == "end"
        else history(2, "2024-02-10T00:00:00Z", "self", "other")
    ]
    records = imported_histories(workspace, fixture)
    assert all(not record.get("assignment_intervals") for record in records)
    with connect(load_config(workspace).database_path) as connection:
        result = search(PacketBuilder(connection, load_config(workspace)))["results"][0]
        assert result["date_from"] is None
        assert result["date_to"] is None
        assert result["period_status"] == "unknown"


def test_reassignment_keeps_separate_actor_intervals_and_nearest_boundaries(
    workspace: Path,
) -> None:
    fixture = JiraFixture()
    fixture.histories = [
        history(1, "2023-11-01T00:00:00Z", "someone", "other"),
        history(2, "2023-12-10T00:00:00Z", "other", "self"),
        history(3, "2024-01-10T00:00:00Z", "self", "other"),
        history(4, "2024-01-20T00:00:00Z", "other", "self"),
        history(5, "2024-02-10T00:00:00Z", "self", "other"),
        history(6, "2024-03-10T00:00:00Z", "other", "someone"),
    ]
    records = imported_histories(workspace, fixture)
    assert {record["created_at"] for record in records} == {
        value["created"] for value in fixture.histories[1:5]
    }
    intervals = [item for record in records for item in record.get("assignment_intervals", [])]
    assert [item["actor_id"] for item in sorted(intervals, key=lambda item: item["from"])] == [
        "self",
        "other",
        "self",
    ]
    assert all(item["start_history_id"] and item["end_history_id"] for item in intervals)
    assert sum(record["boundary_context"] for record in records) == 2


def test_same_instant_with_different_offsets_does_not_pair_assignments(workspace: Path) -> None:
    fixture = JiraFixture()
    fixture.histories = [
        history(1, "2023-12-10T00:00:00Z", "other", "self"),
        history(2, "2024-02-10T01:00:00+01:00", "self", "other"),
        history(3, "2024-02-10T00:00:00Z", "other", "third"),
    ]
    records = imported_histories(workspace, fixture)
    assert all(not record.get("assignment_intervals") for record in records)
    assert any(record.get("ambiguous_time") for record in records)


def test_assignment_order_uses_instants_across_dst_fallback(workspace: Path) -> None:
    workspace.write_text(
        workspace.read_text()
        .replace('from = "2024-01-01"', 'from = "2024-11-03"')
        .replace('to = "2024-01-31"', 'to = "2024-11-03"')
        .replace("[employment]", '[employment]\ntimezone = "America/New_York"')
    )
    fixture = JiraFixture()
    fixture.histories = [
        history(2, "2024-11-03T01:15:00-05:00", "self", "other"),
        history(1, "2024-11-03T01:45:00-04:00", "other", "self"),
    ]
    records = imported_histories(workspace, fixture)
    intervals = [item for record in records for item in record.get("assignment_intervals", [])]
    assert len(intervals) == 1
    assert intervals[0]["actor_id"] == "self"
    assert (
        datetime.fromisoformat(intervals[0]["to"]) - datetime.fromisoformat(intervals[0]["from"])
    ).total_seconds() == 30 * 60


@pytest.mark.parametrize("timestamp", ["not-a-date", "2024-01-10T12:00:00"])
def test_invalid_history_time_preserves_previous_complete_authority(
    workspace: Path, timestamp: str
) -> None:
    fixture = JiraFixture([issue(created="2024-01-02T00:00:00Z")])
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
        fixture.issues = [issue(2)]
        fixture.histories = [history(1, timestamp, "other", "self")]
        invoke(workspace, "import", "all", "sample", success=False)
    config = load_config(workspace)
    with connect(config.database_path) as connection:
        results = search(PacketBuilder(connection, config))["results"]
        assert {row["external_id"] for row in results} == {"1"}


def test_failed_hydration_keeps_redacted_first_staged_version_and_prior_observations(
    workspace: Path,
) -> None:
    fixture = JiraFixture([issue(created="2024-01-02T00:00:00Z")])
    config = load_config(workspace)
    with respx.mock() as mock:
        mock.route().mock(side_effect=fixture)
        invoke(workspace, "import", "all", "sample")
    with connect(config.database_path) as connection:
        before = dict(connection.execute("SELECT id,data_json FROM observations"))
    fixture.issues = [issue(2)]
    fixture.issues[0]["fields"]["summary"] = "First synthetic@example.test wording"
    fixture.different_duplicate = True

    def fail_hydration(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/changelog"):
            return httpx.Response(403, json={"errorMessages": ["Denied"]})
        return fixture(request)

    with respx.mock() as mock:
        mock.route().mock(side_effect=fail_hydration)
        invoke(workspace, "import", "all", "sample", success=False)
    with connect(config.database_path) as connection:
        after = dict(connection.execute("SELECT id,data_json FROM observations"))
        assert all(after[key] == value for key, value in before.items())
        results = search(PacketBuilder(connection, config))["results"]
        assert {row["external_id"] for row in results} == {"1"}
        staged = connection.execute("SELECT record_json FROM jira_import_stage").fetchall()
        assert len(staged) == 1
        assert "synthetic@example.test" not in staged[0][0]
        payload = json.loads(staged[0][0])["payload"]
        assert payload["summary"].startswith("First ")
        assert "Different later version" not in payload["summary"]
        assert len(payload["selected_by"]) == 3


def test_staged_page_rolls_back_when_second_record_escapes_scope(workspace: Path) -> None:
    fixture = JiraFixture([issue(created="2024-01-02T00:00:00Z")])
    imported_histories(workspace, fixture)
    config = load_config(workspace)
    with (
        connect(config.database_path) as connection,
        httpx.Client(
            base_url="https://jira.example", transport=httpx.MockTransport(fixture)
        ) as client,
    ):
        before = dict(connection.execute("SELECT id,data_json FROM observations"))
        adapter = JiraAdapter(
            JiraConfig(
                "https://jira.example",
                "jira",
                "sample",
                ("DEMO",),
                b"test",
                date(2024, 1, 1),
                date(2024, 1, 31),
                account_id="self",
            ),
            client,
        )
        record = next(adapter.iter_discovery_pages()).records[0]
        invalid = replace(
            record, identity=replace(record.identity, app_id="out-of-scope", external_id="2")
        )
        repository = EvidenceRepository(connection)
        run = repository.start_sync_run("sample", "jira", "jira", {})
        with pytest.raises(ScopeViolation, match="source scope"):
            JiraStage(repository, run).put_page("issue", (record, invalid))
        assert connection.execute("SELECT COUNT(*) FROM jira_import_stage").fetchone()[0] == 0
        assert dict(connection.execute("SELECT id,data_json FROM observations")) == before


def test_hierarchy_root_bound_limits_requests_and_reports_omission() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(200, json=issue(10))

    with httpx.Client(
        base_url="https://jira.example", transport=httpx.MockTransport(respond)
    ) as client:
        adapter = JiraAdapter(
            JiraConfig(
                "https://jira.example",
                "jira",
                "sample",
                ("DEMO",),
                b"test",
                date(2024, 1, 1),
                date(2024, 1, 31),
                max_hierarchy_roots=1,
                account_id="self",
            ),
            client,
        )
        children = [issue(1), issue(2)]
        children[0]["fields"]["parent"] = {"id": "10", "key": "DEMO-10"}
        children[1]["fields"]["parent"] = {"id": "20", "key": "DEMO-20"}
        records = [adapter._normalize_issue(child, "2026-09-01T00:00:00Z") for child in children]
        pages = list(adapter.hierarchy_pages(records, "2026-09-01T00:00:00Z", lambda _: False))
    assert requests == ["/rest/api/3/issue/10"]
    assert sum(len(page.records) for page in pages) == 1
    assert any("root bound" in limitation for page in pages for limitation in page.limitations)
