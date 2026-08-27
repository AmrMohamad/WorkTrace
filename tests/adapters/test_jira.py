from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from worktrace.adapters.base import ParticipationRole
from worktrace.adapters.jira import JiraAdapter, JiraConfig
from worktrace.errors import PermanentSourceError, ScopeViolation


def _issue(
    project_key: str = "MOB",
    *,
    issue_id: str = "10001",
    issue_key: str | None = None,
    updated_at: str = "2026-01-02T10:00:00+00:00",
    parent: dict[str, object] | None = None,
    is_subtask: bool = False,
) -> dict[str, object]:
    key = issue_key or f"{project_key}-42"
    return {
        "id": issue_id,
        "key": key,
        "fields": {
            "project": {"key": project_key},
            "summary": "Customer email user@example.com",
            "description": {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "Safe text"}]}
                ],
            },
            "status": {"name": "Done", "statusCategory": {"key": "done"}},
            "issuetype": {"name": "Sub-task" if is_subtask else "Story", "subtask": is_subtask},
            "priority": {"name": "High"},
            "created": "2026-01-01T10:00:00+00:00",
            "updated": updated_at,
            "resolutiondate": "2026-01-02T10:00:00+00:00",
            "labels": ["ios"],
            "components": [{"name": "Checkout"}],
            "assignee": {
                "accountId": "actor-1",
                "displayName": "A Dev",
                "emailAddress": "dev@example.com",
            },
            "reporter": {"accountId": "actor-2", "displayName": "Reporter"},
            "creator": {"accountId": "actor-2", "displayName": "Reporter"},
            "parent": parent if parent is not None else {"key": f"{project_key}-1"},
            "subtasks": [],
            "issuelinks": [],
            "attachment": [{"content": "must not be requested or persisted"}],
        },
    }


def test_jira_page_is_scoped_normalized_and_redacted() -> None:
    observed_query = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_query
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"accountId": "actor-self"}, request=request)
        if request.url.path.endswith("/comment"):
            return httpx.Response(
                200,
                json={"startAt": 0, "maxResults": 100, "total": 0, "comments": []},
                request=request,
            )
        if request.url.path.endswith("/changelog"):
            return httpx.Response(
                200,
                json={
                    "startAt": 0,
                    "maxResults": 100,
                    "total": 0,
                    "isLast": True,
                    "values": [],
                },
                request=request,
            )
        if request.url.path.endswith("/issue/MOB-1"):
            return httpx.Response(
                200,
                json=_issue(issue_id="10000", issue_key="MOB-1", parent={}),
                request=request,
            )
        observed_query = str(json.loads(request.content)["jql"])
        return httpx.Response(
            200,
            json={
                "issues": [
                    _issue(),
                    _issue(issue_id="10002", updated_at="2026-02-02T10:00:00+00:00"),
                ],
                "isLast": True,
            },
            request=request,
        )

    with httpx.Client(
        base_url="https://jira.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            JiraAdapter(
                JiraConfig(
                    base_url="https://jira.example",
                    source_instance="jira-main",
                    app_id="sample_store",
                    project_keys=("MOB",),
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    account_id="actor-self",
                ),
                client,
            ).iter_pages()
        )

    record = pages[0].records[0]
    assert "MOB" in observed_query
    assert 'updated >= "2026-01-01"' in observed_query
    assert 'updated < "2026-02-01"' in observed_query
    assert len(pages[0].records) == 1
    assert record.payload["status"] == "Done"
    assert "user@example.com" not in repr(record)
    assert "attachment" not in record.payload
    assert {item.role for item in record.participations} == {
        ParticipationRole.ASSIGNEE,
        ParticipationRole.REPORTER,
        ParticipationRole.CREATOR,
    }
    assert record.payload["parent"] == {"id": None, "key": "MOB-1"}
    assert not any(ref.reference_type == "jira_subtask_of" for ref in record.references)
    assert any(ref.reference_type == "jira_hierarchy_context" for ref in record.references)


def test_jira_imports_paged_comments_and_transition_intervals_redacted() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "jira"
    comments = json.loads((fixture_root / "comments.json").read_text())
    changelog = json.loads((fixture_root / "changelog.json").read_text())
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"accountId": "actor-self"}, request=request)
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": [_issue()], "isLast": True}, request=request)
        if request.url.path.endswith("/issue/MOB-1"):
            return httpx.Response(
                200,
                json=_issue(issue_id="10000", issue_key="MOB-1", parent={}),
                request=request,
            )
        start_at = int(request.url.params.get("startAt", "0"))
        if request.url.path.endswith("/comment"):
            page_comments = comments["comments"][start_at : start_at + 1]
            return httpx.Response(
                200,
                json={
                    "startAt": start_at,
                    "maxResults": 1,
                    "total": len(comments["comments"]),
                    "comments": page_comments,
                },
                request=request,
            )
        if request.url.path.endswith("/changelog"):
            page_values = changelog["values"][start_at : start_at + 1]
            return httpx.Response(
                200,
                json={
                    "startAt": start_at,
                    "maxResults": 1,
                    "total": len(changelog["values"]),
                    "isLast": start_at + 1 >= len(changelog["values"]),
                    "values": page_values,
                },
                request=request,
            )
        raise AssertionError(f"unexpected Jira request: {request.url.path}")

    with httpx.Client(
        base_url="https://jira.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            JiraAdapter(
                JiraConfig(
                    base_url="https://jira.example",
                    source_instance="jira-main",
                    app_id="sample_store",
                    project_keys=("MOB",),
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    account_id="actor-self",
                    page_size=1,
                ),
                client,
            ).iter_pages()
        )

    comment_pages = [page for page in pages if page.resource_type == "issue_comment"]
    changelog_pages = [page for page in pages if page.resource_type == "issue_changelog"]
    assert [page.cursor for page in comment_pages] == ["MOB-42:0", "MOB-42:1"]
    assert [page.cursor for page in changelog_pages] == [
        "MOB-42:0",
        "MOB-42:1",
        "MOB-42:2",
    ]
    nested_paths = [path for path in requested_paths if path.endswith(("/comment", "/changelog"))]
    assert all("/issue/10001/" in path for path in nested_paths)
    assert comment_pages[0].records[0].participations[0].role is ParticipationRole.AUTHOR
    assert comment_pages[0].records[0].untrusted_text_fields == ("body",)
    assert "fixture-secret-must-redact" not in repr(comment_pages)
    assert "customer@example.test" not in repr(comment_pages)

    first_assignment = changelog_pages[0].records[0]
    reassignment = changelog_pages[1].records[0]
    status_change = changelog_pages[2].records[0]
    assert first_assignment.payload["transitions"][0]["field"] == "assignee"
    assert reassignment.payload["interval_observations"] == [
        {
            "field": "assignee",
            "value_id": "actor-self",
            "value": "Fixture Engineer",
            "effective_to": "2026-01-12T12:00:00.000+0000",
        },
        {
            "field": "assignee",
            "value_id": "actor-other",
            "value": "Fixture Collaborator",
            "effective_from": "2026-01-12T12:00:00.000+0000",
        },
    ]
    assignees = [
        participation
        for participation in reassignment.participations
        if participation.role is ParticipationRole.ASSIGNEE
    ]
    assert assignees[0].effective_to == "2026-01-12T12:00:00.000+0000"
    assert assignees[1].effective_from == "2026-01-12T12:00:00.000+0000"
    assert status_change.payload["transitions"][0]["field"] == "status"


def test_jira_verifies_identity_unions_discovery_and_hydrates_true_subtask_root() -> None:
    jql_queries: list[str] = []
    child = _issue(
        issue_id="10001",
        issue_key="MOB-42",
        parent={"id": "10000", "key": "MOB-1"},
        is_subtask=True,
    )
    child["fields"]["fixVersions"] = [
        {
            "id": "900",
            "name": "Mobile 1.2",
            "released": True,
            "archived": False,
            "releaseDate": "2026-01-20",
        }
    ]
    root = _issue(issue_id="10000", issue_key="MOB-1", parent={})
    root["fields"]["subtasks"] = [
        {
            "id": "10001",
            "key": "MOB-42",
            "fields": {"issuetype": {"name": "Sub-task", "subtask": True}},
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/myself"):
            return httpx.Response(200, json={"accountId": "actor-self"}, request=request)
        if request.url.path.endswith("/search/jql"):
            body = json.loads(request.content)
            jql_queries.append(str(body["jql"]))
            assert request.method == "POST"
            assert "fixVersions" in body["fields"]
            return httpx.Response(200, json={"issues": [child], "isLast": True}, request=request)
        if request.url.path.endswith(("/comment", "/changelog")):
            key = "comments" if request.url.path.endswith("/comment") else "values"
            return httpx.Response(
                200,
                json={"startAt": 0, "maxResults": 100, "total": 0, key: []},
                request=request,
            )
        if request.url.path.endswith("/issue/10000"):
            return httpx.Response(200, json=root, request=request)
        raise AssertionError(f"unexpected Jira request: {request.url}")

    with httpx.Client(
        base_url="https://jira.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            JiraAdapter(
                JiraConfig(
                    base_url="https://jira.example",
                    source_instance="jira-main",
                    app_id="sample_store",
                    project_keys=("MOB",),
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    account_id="actor-self",
                    discovered_issue_keys=("MOB-42",),
                ),
                client,
            ).iter_pages()
        )

    assert len(jql_queries) == 2
    assert "updatedBy" in jql_queries[0]
    assert "assignee WAS" in jql_queries[0]
    assert 'key in ("MOB-42")' in jql_queries[1]
    issue = next(page.records[0] for page in pages if page.resource_type == "issue")
    assert issue.payload["fix_versions"][0]["name"] == "Mobile 1.2"
    assert issue.payload["fix_versions"][0]["archived"] is False
    assert {(ref.reference_type, ref.target_external_id) for ref in issue.references} >= {
        ("jira_subtask_of", "10000")
    }
    hierarchy = next(page.records[0] for page in pages if page.resource_type == "issue_hierarchy")
    assert {(ref.reference_type, ref.target_external_id) for ref in hierarchy.references} >= {
        ("jira_parent_of", "10001")
    }


def test_jira_exact_root_404_emits_unavailable_but_nested_404_remains_permanent() -> None:
    child = _issue(
        issue_id="10001",
        issue_key="MOB-42",
        parent={"id": "10000", "key": "MOB-1"},
        is_subtask=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": [child], "isLast": True}, request=request)
        if request.url.path.endswith("/comment"):
            return httpx.Response(
                200,
                json={"startAt": 0, "maxResults": 100, "total": 0, "comments": []},
                request=request,
            )
        if request.url.path.endswith("/changelog"):
            return httpx.Response(
                200,
                json={"startAt": 0, "maxResults": 100, "total": 0, "values": []},
                request=request,
            )
        if request.url.path.endswith("/issue/10000"):
            return httpx.Response(404, request=request)
        raise AssertionError(f"unexpected Jira request: {request.url}")

    with httpx.Client(
        base_url="https://jira.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            JiraAdapter(
                JiraConfig(
                    base_url="https://jira.example",
                    source_instance="jira-main",
                    app_id="sample_store",
                    project_keys=("MOB",),
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    discovered_issue_keys=("MOB-42",),
                ),
                client,
            ).iter_pages()
        )

    unavailable = next(page for page in pages if page.unavailable_objects)
    assert unavailable.records == ()
    assert unavailable.unavailable_objects[0].kind == "jira_issue"
    assert unavailable.unavailable_objects[0].external_id == "10000"

    def nested_404(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/search/jql"):
            return httpx.Response(200, json={"issues": [child], "isLast": True}, request=request)
        return httpx.Response(404, request=request)

    with (
        httpx.Client(
            base_url="https://jira.example",
            transport=httpx.MockTransport(nested_404),
        ) as client,
        pytest.raises(PermanentSourceError, match="HTTP 404"),
    ):
        list(
            JiraAdapter(
                JiraConfig(
                    base_url="https://jira.example",
                    source_instance="jira-main",
                    app_id="sample_store",
                    project_keys=("MOB",),
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    discovered_issue_keys=("MOB-42",),
                ),
                client,
            ).iter_pages()
        )


def test_jira_rejects_out_of_scope_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"issues": [_issue("OTHER")], "isLast": True},
            request=request,
        )

    with httpx.Client(
        base_url="https://jira.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = JiraAdapter(
            JiraConfig(
                base_url="https://jira.example",
                source_instance="jira-main",
                app_id="sample_store",
                project_keys=("MOB",),
                email_key=b"test-key",
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 31),
                discovered_issue_keys=("MOB-42",),
            ),
            client,
        )
        with pytest.raises(ScopeViolation):
            list(adapter.iter_pages())
