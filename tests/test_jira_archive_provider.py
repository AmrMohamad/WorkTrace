from __future__ import annotations

import json

import httpx
import pytest

from worktrace.adapters.retry import RetryPolicy
from worktrace.archive.jira.provider import JiraArchiveProvider
from worktrace.errors import InvalidCredentials, PermanentSourceError, PermissionDenied


def _provider(handler):
    client = httpx.Client(
        base_url="https://jira.example.test",
        auth=("fixture@example.test", "fixture-token"),
        trust_env=False,
        follow_redirects=False,
        transport=httpx.MockTransport(handler),
    )
    return JiraArchiveProvider(
        client,
        origin="https://jira.example.test",
        retry_policy=RetryPolicy(max_attempts=1, base_delay_seconds=0, max_delay_seconds=0),
    )


@pytest.mark.parametrize(
    ("status", "error"),
    [(302, PermanentSourceError), (401, InvalidCredentials), (403, PermissionDenied)],
)
def test_attachment_content_refuses_redirect_and_auth_failures(
    status: int, error: type[Exception]
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.path == "/rest/api/3/attachment/content/10"
        assert request.url.params["redirect"] == "false"
        return httpx.Response(
            status, headers={"Location": "https://evil.example/bytes"}, request=request
        )

    provider = _provider(handler)
    with pytest.raises(error), provider.attachment_content("10"):
        raise AssertionError("unreachable")
    assert calls == 1
    provider.close()


def test_attachment_content_retries_transient_then_streams_without_redirects() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, request=request)
        return httpx.Response(200, content=b"bytes", request=request)

    client = httpx.Client(
        base_url="https://jira.example.test",
        trust_env=False,
        follow_redirects=False,
        transport=httpx.MockTransport(handler),
    )
    provider = JiraArchiveProvider(
        client,
        origin="https://jira.example.test",
        retry_policy=RetryPolicy(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
    )
    with provider.attachment_content("10") as response:
        assert response.read() == b"bytes"
    assert calls == 2
    provider.close()


def test_preview_search_uses_enhanced_search_json_contract() -> None:
    observed: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/search/jql"
        body = json.loads(request.content)
        assert isinstance(body, dict)
        observed.append(body)
        if not isinstance(body.get("expand"), str):
            return httpx.Response(400, json={"errorMessages": ["expand must be a string"]})
        return httpx.Response(
            200,
            json={"issues": [{"id": "10001"}], "nextPageToken": "next", "isLast": False},
            request=request,
        )

    provider = _provider(handler)
    page = provider.search_metadata("project = DEMO", next_token="prior")
    assert page.next_token == "next"
    assert page.is_last is False
    assert len(observed) == 1
    body = observed[0]
    assert body["fields"] == [
        "id",
        "key",
        "project",
        "parent",
        "subtasks",
        "issuelinks",
        "attachment",
    ]
    assert body["fieldsByKeys"] is False
    assert body["expand"] == ""
    assert body["nextPageToken"] == "prior"
    provider.close()


@pytest.mark.parametrize("payload", [[], [{"id": "10", "object": {"url": "https://jira.test"}}]])
def test_remote_links_accepts_bounded_metadata_list(payload: list[object]) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=payload, request=request))
    assert provider.remote_links("1") == payload
    provider.close()


@pytest.mark.parametrize("payload", [{"id": "10"}, ["malformed"]])
def test_remote_links_rejects_malformed_top_level_or_item(payload: object) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=payload, request=request))
    with pytest.raises(PermanentSourceError):
        provider.remote_links("1")
    provider.close()


@pytest.mark.parametrize("path", ["/watchers", "/votes"])
def test_watchers_and_votes_require_object_shapes(path: str) -> None:
    provider = _provider(lambda request: httpx.Response(200, json=[], request=request))
    with pytest.raises(PermanentSourceError):
        (provider.watchers if path == "/watchers" else provider.votes)("1")
    provider.close()
