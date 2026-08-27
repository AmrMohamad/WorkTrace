from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from worktrace.adapters.base import ParticipationRole, ReferenceStrength
from worktrace.adapters.gitlab import GitLabAdapter, GitLabConfig
from worktrace.errors import ConfigurationError, PermanentSourceError, ScopeViolation
from worktrace.importers.orchestrator import record_to_object


def test_gitlab_snapshot_keeps_release_ladder_facts_separate() -> None:
    merge_request_document: dict[str, object] = {
        "project_id": 77,
        "iid": 12,
        "title": "MOB-42 Checkout",
        "description": "Review with dev@example.com",
        "state": "merged",
        "draft": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
        "merged_at": "2026-01-02T00:00:00Z",
        "source_branch": "feature/MOB-42",
        "target_branch": "main",
        "sha": "a" * 40,
        "merge_commit_sha": "b" * 40,
        "author": {"id": 1, "name": "Author", "username": "author"},
        "assignees": [{"id": 2, "name": "Assignee", "username": "assigned"}],
        "reviewers": [{"id": 3, "name": "Reviewer", "username": "review"}],
        "merge_user": {"id": 4, "name": "Merger", "username": "merge"},
        "labels": ["ios"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 1, "username": "author"}, request=request)
        if request.url.path.endswith("/repository/commits"):
            return httpx.Response(200, json=[], request=request)
        if request.url.path.endswith("/merge_requests/12"):
            return httpx.Response(200, json=merge_request_document, request=request)
        if request.url.path.endswith(("/commits", "/discussions")):
            return httpx.Response(200, json=[], request=request)
        if request.url.path.endswith("/changes"):
            return httpx.Response(
                200,
                json={"project_id": 77, "iid": 12, "changes": []},
                request=request,
            )
        if request.url.path.endswith("/merge_requests"):
            document: list[dict[str, object]] = [merge_request_document]
        elif request.url.path.endswith("/releases"):
            document = [
                {
                    "tag_name": "v0.1",
                    "name": "Release 0.1",
                    "description": "Release notes",
                    "released_at": "2026-01-03T00:00:00Z",
                    "commit": {"id": "b" * 40},
                    "author": {"id": 5, "name": "Release Author", "username": "release"},
                }
            ]
        else:
            document = [
                {
                    "project_id": 77,
                    "id": 9,
                    "status": "success",
                    "sha": "b" * 40,
                    "ref": "v0.1",
                    "created_at": "2026-01-03T01:00:00Z",
                    "updated_at": "2026-01-03T01:10:00Z",
                    "finished_at": "2026-01-03T01:10:00Z",
                    "environment": {"id": 1, "name": "production", "tier": "production"},
                    "user": {"id": 6, "name": "Deployer", "username": "deploy"},
                }
            ]
        return httpx.Response(200, json=document, request=request)

    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            GitLabAdapter(
                GitLabConfig(
                    base_url="https://gitlab.example",
                    source_instance="gitlab-main",
                    app_id="sample_store",
                    project_id=77,
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    jira_project_keys=("MOB",),
                    user_id=1,
                    production_environments=("production",),
                ),
                client,
            ).iter_pages()
        )

    merge_request = next(
        page.records[0] for page in pages if page.resource_type == "merge_requests"
    )
    release = next(page.records[0] for page in pages if page.resource_type == "releases")
    deployment = next(page.records[0] for page in pages if page.resource_type == "deployments")
    assert {item.role for item in merge_request.participations} == {
        ParticipationRole.AUTHOR,
        ParticipationRole.ASSIGNEE,
        ParticipationRole.REVIEWER,
        ParticipationRole.MERGER,
    }
    assert any(
        item.target_external_id == "MOB-42" and item.strength is ReferenceStrength.EXACT_TEXT
        for item in merge_request.references
    )
    assert release.identity.object_type == "release"
    assert deployment.identity.object_type == "deployment"
    assert deployment.participations[0].role is ParticipationRole.DEPLOYER
    assert deployment.payload["environment_name"] == "production"
    assert "released_to_users" not in release.payload
    assert "currently_enabled" not in deployment.payload
    assert "dev@example.com" not in repr(merge_request)


def test_gitlab_uses_date_filters_and_validated_link_pagination() -> None:
    merge_request_calls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 41, "username": "fixture-self"}, request=request)
        if request.url.path.endswith("/repository/commits"):
            return httpx.Response(200, json=[], request=request)
        if "/merge_requests/" in request.url.path and request.url.path.rsplit("/", 1)[-1].isdigit():
            iid = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(
                200,
                json={
                    "project_id": 77,
                    "iid": iid,
                    "title": "In window",
                    "updated_at": "2026-01-15T00:00:00Z",
                    "created_at": "2026-01-14T00:00:00Z",
                },
                request=request,
            )
        if request.url.path.endswith(("/commits", "/discussions")):
            return httpx.Response(200, json=[], request=request)
        if request.url.path.endswith("/changes"):
            iid = int(request.url.path.split("/")[-2])
            return httpx.Response(
                200,
                json={"project_id": 77, "iid": iid, "changes": []},
                request=request,
            )
        if request.url.path.endswith("/merge_requests"):
            merge_request_calls.append(request.url)
            page = request.url.params.get("page")
            headers = (
                {
                    "Link": (
                        "<https://gitlab.example/api/v4/projects/77/merge_requests?"
                        'page=2&per_page=100>; rel="next"'
                    )
                }
                if page == "1"
                else {}
            )
            document = [
                {
                    "project_id": 77,
                    "iid": int(page or "1"),
                    "title": "In window",
                    "updated_at": "2026-01-15T00:00:00Z",
                    "created_at": "2026-01-14T00:00:00Z",
                },
                {
                    "project_id": 77,
                    "iid": 99,
                    "title": "Outside window",
                    "updated_at": "2026-02-15T00:00:00Z",
                    "created_at": "2026-02-14T00:00:00Z",
                },
            ]
            return httpx.Response(200, json=document, headers=headers, request=request)
        return httpx.Response(200, json=[], request=request)

    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            GitLabAdapter(
                GitLabConfig(
                    base_url="https://gitlab.example",
                    source_instance="gitlab-main",
                    app_id="sample_store",
                    project_id=77,
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    user_id=41,
                ),
                client,
            ).iter_pages()
        )

    assert len(merge_request_calls) == 6
    assert merge_request_calls[0].params["updated_after"].startswith("2026-01-01")
    assert merge_request_calls[0].params["updated_before"].startswith("2026-01-31")
    merge_request_pages = [page for page in pages if page.resource_type == "merge_requests"]
    assert [page.cursor for page in merge_request_pages] == ["hydrate:0", "hydrate:1"]
    assert [len(page.records) for page in merge_request_pages] == [1, 1]


def test_gitlab_imports_paged_mr_evidence_without_diffs_and_redacts_text() -> None:
    fixture_root = Path(__file__).parents[1] / "fixtures" / "gitlab"
    commits = json.loads((fixture_root / "mr_7_commits.json").read_text())
    discussions = json.loads((fixture_root / "mr_7_discussions.json").read_text())
    changes = json.loads((fixture_root / "mr_7_changes.json").read_text())
    user = json.loads((fixture_root / "user.json").read_text())
    calls: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json=user, request=request)
        if request.url.path.endswith("/repository/commits"):
            return httpx.Response(200, json=[], request=request)
        if request.url.path.endswith("/merge_requests/7"):
            return httpx.Response(
                200,
                json={
                    "project_id": 101,
                    "iid": 7,
                    "title": "DEMO-101 fixture MR",
                    "created_at": "2026-01-09T00:00:00Z",
                    "updated_at": "2026-01-12T00:00:00Z",
                },
                request=request,
            )
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(
                200,
                json=[
                    {
                        "project_id": 101,
                        "iid": 7,
                        "title": "DEMO-101 fixture MR",
                        "created_at": "2026-01-09T00:00:00Z",
                        "updated_at": "2026-01-12T00:00:00Z",
                    }
                ],
                request=request,
            )
        if request.url.path.endswith("/commits"):
            page = request.url.params.get("page")
            headers = (
                {
                    "Link": (
                        "<https://gitlab.example/api/v4/projects/101/merge_requests/7/"
                        'commits?page=2&per_page=1>; rel="next"'
                    )
                }
                if page == "1"
                else {}
            )
            return httpx.Response(
                200,
                json=(
                    [
                        *commits,
                        {
                            "id": "f" * 40,
                            "title": "Outside authorized window",
                            "message": "must not persist",
                            "authored_date": "2026-02-10T10:00:00.000Z",
                            "committed_date": "2026-02-10T11:00:00.000Z",
                        },
                    ]
                    if page == "1"
                    else []
                ),
                headers=headers,
                request=request,
            )
        if request.url.path.endswith("/discussions"):
            return httpx.Response(
                200,
                json=[
                    *discussions,
                    {
                        "id": "discussion-outside",
                        "notes": [
                            {
                                "id": 7199,
                                "body": "must not persist",
                                "created_at": "2026-02-11T10:00:00.000Z",
                                "updated_at": "2026-02-11T10:00:00.000Z",
                            }
                        ],
                    },
                ],
                request=request,
            )
        if request.url.path.endswith("/changes"):
            return httpx.Response(200, json=changes, request=request)
        return httpx.Response(200, json=[], request=request)

    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            GitLabAdapter(
                GitLabConfig(
                    base_url="https://gitlab.example",
                    source_instance="gitlab-main",
                    app_id="sample_store",
                    project_id=101,
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    jira_project_keys=("DEMO",),
                    user_id=41,
                    page_size=1,
                ),
                client,
            ).iter_pages()
        )

    commit_pages = [page for page in pages if page.resource_type == "merge_request_commits"]
    discussion_page = next(
        page for page in pages if page.resource_type == "merge_request_discussions"
    )
    changes_page = next(
        page for page in pages if page.resource_type == "merge_request_changed_paths"
    )
    assert [page.cursor for page in commit_pages] == ["7:1", "7:2"]
    assert commit_pages[0].next_cursor == "7:2"
    commit = commit_pages[0].records[0]
    assert {participation.role for participation in commit.participations} == {
        ParticipationRole.AUTHOR,
        ParticipationRole.COMMITTER,
        ParticipationRole.CO_AUTHOR,
    }
    assert any(reference.target_external_id == "DEMO-101" for reference in commit.references)
    assert "self@example.test" not in repr(commit)
    assert all(
        record.identity.external_id != f"101:7:{'f' * 40}" for record in commit_pages[0].records
    )

    assert len(discussion_page.records) == 2
    assert all(record.untrusted_text_fields == ("body",) for record in discussion_page.records)
    assert "fixture-discussion-secret" not in repr(discussion_page)
    assert all(
        record.references[0].target_external_id == "101:7" for record in discussion_page.records
    )
    source_note = discussion_page.records[0]
    assert source_note.participations[0].role is ParticipationRole.AUTHOR
    persisted_note = record_to_object(source_note, set())
    assert persisted_note.identity.kind == "gitlab_merge_request_discussion_note"
    assert persisted_note.participations[0].role == "gitlab_discussion_author"

    changed_paths = changes_page.records[0].payload["changed_paths"]
    assert changed_paths == [
        {
            "old_path": "Sources/Checkout/NameValidator.swift",
            "new_path": "Sources/Checkout/NameValidator.swift",
            "new_file": False,
            "renamed_file": False,
            "deleted_file": False,
        }
    ]
    assert "diff" not in repr(changes_page)
    assert any(url.path.endswith("/merge_requests/7/changes") for url in calls)


def test_gitlab_verifies_user_deduplicates_discovery_and_filters_production() -> None:
    role_filters: list[str] = []
    hydrated_calls = 0
    merge_request = {
        "project_id": 101,
        "iid": 7,
        "title": "DEMO-101 discovered MR",
        "created_at": "2026-01-09T00:00:00Z",
        "updated_at": "2026-01-12T00:00:00Z",
        "author": {"id": 41, "username": "fixture-self", "name": "Fixture Engineer"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal hydrated_calls
        path = request.url.path
        if path == "/api/v4/user":
            return httpx.Response(
                200,
                json={
                    "id": 41,
                    "username": "fixture-self",
                    "email": "self@example.test",
                },
                request=request,
            )
        if path.endswith("/repository/commits"):
            assert request.url.params["author"] == "self@example.test"
            return httpx.Response(
                200,
                json=[{"id": "a" * 40, "committed_date": "2026-01-10T00:00:00Z"}],
                request=request,
            )
        if path.endswith(f"/repository/commits/{'a' * 40}/merge_requests"):
            return httpx.Response(200, json=[merge_request], request=request)
        if path.endswith("/merge_requests/7"):
            hydrated_calls += 1
            return httpx.Response(200, json=merge_request, request=request)
        if path.endswith("/merge_requests"):
            role_filter = next(
                (key for key in ("author_id", "assignee_id") if key in request.url.params),
                "reviews_for_me",
            )
            role_filters.append(role_filter)
            if role_filter == "reviews_for_me":
                assert request.url.params["scope"] == "reviews_for_me"
                assert "reviewer_id" not in request.url.params
            else:
                assert request.url.params[role_filter] == "41"
                assert request.url.params["scope"] == "all"
            return httpx.Response(200, json=[merge_request], request=request)
        if path.endswith(("/commits", "/discussions")):
            return httpx.Response(200, json=[], request=request)
        if path.endswith("/changes"):
            return httpx.Response(
                200,
                json={"project_id": 101, "iid": 7, "changes": []},
                request=request,
            )
        if path.endswith("/releases"):
            return httpx.Response(200, json=[], request=request)
        if path.endswith("/deployments"):
            assert request.url.params["status"] == "success"
            assert request.url.params["environment"] == "production"
            base = {
                "project_id": 101,
                "sha": "a" * 40,
                "created_at": "2026-01-15T00:00:00Z",
                "updated_at": "2026-01-15T00:10:00Z",
            }
            return httpx.Response(
                200,
                json=[
                    {
                        **base,
                        "id": 1,
                        "status": "success",
                        "environment": {"name": "production"},
                    },
                    {
                        **base,
                        "id": 2,
                        "status": "failed",
                        "environment": {"name": "production"},
                    },
                    {
                        **base,
                        "id": 3,
                        "status": "success",
                        "environment": {"name": "staging"},
                    },
                ],
                request=request,
            )
        raise AssertionError(f"unexpected GitLab request: {request.url}")

    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            GitLabAdapter(
                GitLabConfig(
                    base_url="https://gitlab.example",
                    source_instance="gitlab-main",
                    app_id="sample_store",
                    project_id=101,
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    user_id=41,
                    username="fixture-self",
                    production_environments=("production",),
                    relevant_commit_shas=("a" * 40,),
                ),
                client,
            ).iter_pages()
        )

    assert role_filters == ["author_id", "assignee_id", "reviews_for_me"]
    assert hydrated_calls == 1
    merge_request_records = [
        record
        for page in pages
        if page.resource_type == "merge_requests"
        for record in page.records
    ]
    assert len(merge_request_records) == 1
    deployment_records = [
        record for page in pages if page.resource_type == "deployments" for record in page.records
    ]
    assert [record.payload["id"] for record in deployment_records] == ["1"]
    assert deployment_records[0].payload["environment_name"] == "production"


def test_gitlab_exact_mr_404_emits_unavailable_but_nested_404_is_not_parent_loss() -> None:
    merge_request = {
        "project_id": 101,
        "iid": 7,
        "title": "DEMO-101 availability fixture",
        "created_at": "2026-01-09T00:00:00Z",
        "updated_at": "2026-01-12T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/user":
            return httpx.Response(200, json={"id": 41, "username": "fixture-self"}, request=request)
        if path.endswith("/repository/commits"):
            return httpx.Response(200, json=[], request=request)
        if path.endswith("/merge_requests/7"):
            return httpx.Response(404, request=request)
        if path.endswith("/merge_requests"):
            return httpx.Response(200, json=[merge_request], request=request)
        if path.endswith("/releases"):
            return httpx.Response(200, json=[], request=request)
        raise AssertionError(f"unexpected GitLab request: {request.url}")

    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        pages = list(
            GitLabAdapter(
                GitLabConfig(
                    base_url="https://gitlab.example",
                    source_instance="gitlab-main",
                    app_id="sample_store",
                    project_id=101,
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    user_id=41,
                ),
                client,
            ).iter_pages()
        )

    unavailable = next(page for page in pages if page.unavailable_objects)
    assert unavailable.records == ()
    assert unavailable.unavailable_objects[0].kind == "gitlab_mr"
    assert unavailable.unavailable_objects[0].external_id == "101:7"

    def nested_404(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v4/user":
            return httpx.Response(200, json={"id": 41, "username": "fixture-self"}, request=request)
        if path.endswith("/repository/commits"):
            return httpx.Response(200, json=[], request=request)
        if path.endswith("/merge_requests/7"):
            return httpx.Response(200, json=merge_request, request=request)
        if path.endswith("/merge_requests"):
            return httpx.Response(200, json=[merge_request], request=request)
        if path.endswith("/commits"):
            return httpx.Response(404, request=request)
        raise AssertionError(f"unexpected GitLab request: {request.url}")

    with (
        httpx.Client(
            base_url="https://gitlab.example",
            transport=httpx.MockTransport(nested_404),
        ) as client,
        pytest.raises(PermanentSourceError, match="HTTP 404"),
    ):
        list(
            GitLabAdapter(
                GitLabConfig(
                    base_url="https://gitlab.example",
                    source_instance="gitlab-main",
                    app_id="sample_store",
                    project_id=101,
                    email_key=b"test-key",
                    date_from=date(2026, 1, 1),
                    date_to=date(2026, 1, 31),
                    user_id=41,
                ),
                client,
            ).iter_pages()
        )


def test_gitlab_rejects_cross_origin_next_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v4/user":
            return httpx.Response(200, json={"id": 41, "username": "fixture-self"}, request=request)
        return httpx.Response(
            200,
            json=[],
            headers={
                "Link": (
                    "<https://attacker.example/api/v4/projects/77/merge_requests?page=2>; "
                    'rel="next"'
                )
            },
            request=request,
        )

    with httpx.Client(
        base_url="https://gitlab.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = GitLabAdapter(
            GitLabConfig(
                base_url="https://gitlab.example",
                source_instance="gitlab-main",
                app_id="sample_store",
                project_id=77,
                email_key=b"test-key",
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 31),
                user_id=41,
            ),
            client,
        )
        with pytest.raises(ScopeViolation):
            list(adapter.iter_pages())


def test_gitlab_requires_configured_user_identity() -> None:
    with (
        httpx.Client(base_url="https://gitlab.example") as client,
        pytest.raises(ConfigurationError, match="configured user identity"),
    ):
        GitLabAdapter(
            GitLabConfig(
                base_url="https://gitlab.example",
                source_instance="gitlab-main",
                app_id="sample_store",
                project_id=77,
                email_key=b"test-key",
                date_from=date(2026, 1, 1),
                date_to=date(2026, 1, 31),
            ),
            client,
        )
