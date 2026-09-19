"""Dedicated, scope-bound Jira provider for the archive collector."""

from __future__ import annotations

import contextlib
import json
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from worktrace.adapters.retry import RetryPolicy, request_with_retry
from worktrace.archive.jira.repository import canonical_jira_origin
from worktrace.errors import (
    ConfigurationError,
    InvalidCredentials,
    PermanentSourceError,
    PermissionDenied,
    RetryExhausted,
)

PREVIEW_FIELDS = "id,key,project,parent,subtasks,issuelinks,attachment"
_RETRY_POLICY = RetryPolicy(max_attempts=3, base_delay_seconds=0.25, max_delay_seconds=30.0)


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    account_id: str
    timezone: str | None


@dataclass(frozen=True, slots=True)
class MetadataPage:
    issues: tuple[dict[str, object], ...]
    next_token: str | None
    is_last: bool


class JiraArchiveProvider:
    """Jira REST client with exact-origin credentials and bounded retries."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        origin: str,
        retry_policy: RetryPolicy = _RETRY_POLICY,
    ) -> None:
        self.origin = canonical_jira_origin(origin)
        client_origin = canonical_jira_origin(str(client.base_url).rstrip("/"))
        if self.origin != client_origin:
            raise ConfigurationError("Jira client origin does not match configured scope")
        self.client = client
        self.retry_policy = retry_policy

    @classmethod
    def from_credentials(
        cls,
        *,
        base_url: str,
        email: str,
        token: str,
        timeout: float = 30.0,
    ) -> JiraArchiveProvider:
        origin = canonical_jira_origin(base_url)
        client = httpx.Client(
            base_url=origin,
            auth=(email, token),
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
        )
        return cls(client, origin=origin)

    def close(self) -> None:
        self.client.close()

    def verify_identity(self, expected_account_id: str | None = None) -> ProviderIdentity:
        response = self._json("GET", "/rest/api/3/myself", resource="identity")
        account_id = response.get("accountId")
        if not isinstance(account_id, str) or not account_id:
            raise PermanentSourceError("Jira identity omitted accountId")
        if expected_account_id is not None and account_id != expected_account_id:
            raise PermissionDenied("Jira authenticated identity does not match configuration")
        timezone = response.get("timeZone")
        return ProviderIdentity(account_id, timezone if isinstance(timezone, str) else None)

    def search_metadata(self, jql: str, *, next_token: str | None = None) -> MetadataPage:
        body: dict[str, object] = {
            "jql": jql,
            "maxResults": 100,
            "fields": PREVIEW_FIELDS.split(","),
            "fieldsByKeys": False,
            "expand": [],
        }
        if next_token is not None:
            body["nextPageToken"] = next_token
        document = self._json("POST", "/rest/api/3/search/jql", json_body=body, resource="preview")
        issues = document.get("issues")
        if not isinstance(issues, list) or not all(isinstance(item, dict) for item in issues):
            raise PermanentSourceError("Jira preview search omitted issues")
        continuation = document.get("nextPageToken")
        is_last = document.get("isLast")
        if not isinstance(is_last, bool):
            raise PermanentSourceError("Jira preview pagination omitted isLast")
        if continuation is not None and not isinstance(continuation, str):
            raise PermanentSourceError("Jira preview pagination returned an invalid token")
        if not is_last and not isinstance(continuation, str):
            raise PermanentSourceError("Jira preview pagination omitted its continuation token")
        return MetadataPage(
            tuple(item for item in issues),
            continuation if isinstance(continuation, str) else None,
            is_last,
        )

    def issue_metadata(self, issue_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}",
            params={"fields": PREVIEW_FIELDS, "fieldsByKeys": "false", "expand": ""},
            resource="issue metadata",
        )

    def issue_full(self, issue_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}",
            params={
                "fields": "*all",
                "fieldsByKeys": "false",
                "expand": "renderedFields,names,schema",
            },
            resource="issue",
        )

    def assignment_changelog(self, issue_id: str, *, start_at: int = 0) -> dict[str, object]:
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}/changelog",
            params={"startAt": start_at, "maxResults": 100},
            resource="assignment changelog",
        )

    def resource_page(self, issue_id: str, kind: str, *, start_at: int = 0) -> dict[str, object]:
        endpoints = {
            "comments": "comment",
            "changelog": "changelog",
            "worklogs": "worklog",
            "issue_properties": "properties",
        }
        endpoint = endpoints.get(kind)
        if endpoint is None:
            raise ConfigurationError(f"unsupported Jira archive resource kind: {kind}")
        params: dict[str, str | int] = {"startAt": start_at, "maxResults": 100}
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}/{endpoint}",
            params=params,
            resource=kind,
        )

    def remote_links(self, issue_id: str) -> list[dict[str, object]]:
        value = self._json_value(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}/remotelink",
            resource="remote links",
            exact_object=True,
        )
        if not isinstance(value, list) or len(value) > 1000:
            raise PermanentSourceError("Jira remote links response must be a bounded list")
        if not all(isinstance(item, dict) for item in value):
            raise PermanentSourceError("Jira remote links response contained malformed metadata")
        return [dict(item) for item in value]

    def watchers(self, issue_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}/watchers",
            resource="watchers",
            exact_object=True,
        )

    def votes(self, issue_id: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}/votes",
            resource="votes",
            exact_object=True,
        )

    def issue_property(self, issue_id: str, key: str) -> dict[str, object]:
        return self._json(
            "GET",
            f"/rest/api/3/issue/{quote(issue_id, safe='')}/properties/{quote(key, safe='')}",
            resource="issue property",
            exact_object=True,
        )

    @contextlib.contextmanager
    def attachment_content(self, attachment_id: str) -> Iterator[httpx.Response]:
        endpoint = f"/rest/api/3/attachment/content/{quote(attachment_id, safe='')}"
        params = {"redirect": "false"}
        for attempt in range(1, self.retry_policy.max_attempts + 1):
            request = self.client.build_request("GET", endpoint, params=params)
            try:
                response = self.client.send(request, stream=True, follow_redirects=False)
            except httpx.RequestError:
                if attempt == self.retry_policy.max_attempts:
                    raise RetryExhausted(
                        "Jira attachment transport failed after three attempts"
                    ) from None
                time.sleep(self.retry_policy.delay_for(attempt))
                continue
            status = response.status_code
            if 200 <= status < 300:
                try:
                    yield response
                finally:
                    response.close()
                return
            retryable = status == 429 or 500 <= status < 600
            retry_after = response.headers.get("Retry-After")
            response.close()
            if status == 401:
                raise InvalidCredentials("Jira rejected the configured credentials")
            if status == 403:
                raise PermissionDenied("Jira denied attachment access")
            if 300 <= status < 400:
                raise PermanentSourceError("Jira attachment response attempted a redirect")
            if not retryable or attempt == self.retry_policy.max_attempts:
                raise PermanentSourceError(f"Jira attachment request failed with HTTP {status}")
            time.sleep(self.retry_policy.delay_for(attempt, retry_after))
        raise AssertionError("bounded Jira attachment retry loop exited unexpectedly")

    def _json(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: object | None = None,
        resource: str,
        exact_object: bool = False,
    ) -> dict[str, object]:
        value = self._json_value(
            method,
            endpoint,
            params=params,
            json_body=json_body,
            resource=resource,
            exact_object=exact_object,
        )
        if not isinstance(value, dict):
            raise PermanentSourceError(f"Jira returned an invalid {resource} document")
        return value

    def _json_value(
        self,
        method: str,
        endpoint: str,
        *,
        params: Mapping[str, str | int] | None = None,
        json_body: object | None = None,
        resource: str,
        exact_object: bool = False,
    ) -> object:
        response = request_with_retry(
            self.client,
            method,
            endpoint,
            params=params,
            json_body=json_body,
            policy=self.retry_policy,
            exact_object=exact_object,
        )
        try:
            document = response.json()
        except (ValueError, json.JSONDecodeError):
            raise PermanentSourceError(f"Jira returned invalid {resource} JSON") from None
        return document
