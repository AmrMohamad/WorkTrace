"""Scoped Jira Cloud REST v3 full-snapshot adapter."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import quote, urlsplit

import httpx

from worktrace.adapters.base import (
    NormalizedPage,
    NormalizedRecord,
    Participation,
    ParticipationRole,
    Reference,
    ReferenceStrength,
    UnavailableObjectDescriptor,
)
from worktrace.adapters.retry import DEFAULT_RETRY_POLICY, RetryPolicy, request_with_retry
from worktrace.errors import (
    ConfigurationError,
    PermanentSourceError,
    ScopeViolation,
    SourceObjectUnavailable,
)
from worktrace.normalize import (
    Redactor,
    actor_identity,
    build_record,
    extract_jira_text,
    observed_now,
)

_PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
_ISSUE_KEY = re.compile(r"^([A-Z][A-Z0-9_]{1,9})-[1-9][0-9]*$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
_ISSUE_FIELDS = (
    "summary,description,status,issuetype,priority,created,updated,resolutiondate,"
    "labels,components,fixVersions,parent,subtasks,issuelinks,assignee,reporter,creator,project"
)


@dataclass(frozen=True, slots=True)
class JiraConfig:
    base_url: str
    source_instance: str
    app_id: str
    project_keys: tuple[str, ...]
    email_key: bytes
    date_from: date
    date_to: date
    account_id: str | None = None
    discovered_issue_keys: tuple[str, ...] = ()
    exact_key_chunk_size: int = 50
    max_hierarchy_roots: int = 100
    max_hierarchy_depth: int = 3
    page_size: int = 100
    retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _identifier(value: object) -> str | None:
    if isinstance(value, str | int):
        return str(value)
    return None


class JiraAdapter:
    """Reads only configured Jira project keys through fixed REST endpoints."""

    def __init__(self, config: JiraConfig, client: httpx.Client) -> None:
        projects = tuple(dict.fromkeys(key.strip().upper() for key in config.project_keys))
        if not projects or any(_PROJECT_KEY.fullmatch(key) is None for key in projects):
            raise ConfigurationError("Jira project keys must use the provider's canonical syntax")
        if config.page_size < 1 or config.page_size > 100:
            raise ConfigurationError("Jira page_size must be between 1 and 100")
        if config.date_from > config.date_to:
            raise ConfigurationError("Jira date_from must not be after date_to")
        if config.account_id is not None and _ACCOUNT_ID.fullmatch(config.account_id) is None:
            raise ConfigurationError("Jira account_id contains unsupported characters")
        discovered_keys = tuple(
            dict.fromkeys(key.strip().upper() for key in config.discovered_issue_keys)
        )
        if any(
            (match := _ISSUE_KEY.fullmatch(key)) is None or match.group(1) not in projects
            for key in discovered_keys
        ):
            raise ScopeViolation("Discovered Jira keys must remain in configured projects")
        if config.account_id is None and not discovered_keys:
            raise ConfigurationError(
                "Jira import requires a configured account_id or exact discovered issue keys"
            )
        if config.exact_key_chunk_size < 1 or config.exact_key_chunk_size > 100:
            raise ConfigurationError("Jira exact_key_chunk_size must be between 1 and 100")
        if config.max_hierarchy_roots < 0 or config.max_hierarchy_depth < 0:
            raise ConfigurationError("Jira hierarchy bounds must not be negative")
        configured_origin = self._origin(config.base_url)
        client_origin = self._origin(str(client.base_url))
        if configured_origin != client_origin:
            raise ScopeViolation("Jira client origin does not match configured scope")
        self._config = config
        self._projects = projects
        self._discovered_keys = discovered_keys
        self._client = client
        self._redactor = Redactor(config.email_key)
        self._identity_verified = False
        self._window_start = datetime.combine(config.date_from, time.min, tzinfo=UTC)
        self._window_end = datetime.combine(
            config.date_to + timedelta(days=1),
            time.min,
            tzinfo=UTC,
        )

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parts = urlsplit(url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ConfigurationError("Jira base_url must be an HTTP(S) origin")
        if parts.scheme == "http" and parts.hostname.casefold() not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ConfigurationError("Jira credentials require HTTPS outside loopback tests")
        return parts.scheme, parts.hostname.casefold(), parts.port

    def iter_pages(self) -> Iterator[NormalizedPage]:
        observed_at = observed_now()
        self._verify_identity()
        unique_issues: dict[str, object] = {}
        for query_index, (jql, enforce_window) in enumerate(self._discovery_queries()):
            for cursor, next_cursor, is_last, raw_issues in self._search_pages(jql):
                page_issues: list[object] = []
                for issue in raw_issues:
                    issue_id, _ = self._issue_identity(issue)
                    if enforce_window and not self._issue_in_window(issue):
                        continue
                    if issue_id in unique_issues:
                        continue
                    unique_issues[issue_id] = issue
                    page_issues.append(issue)
                yield NormalizedPage(
                    source_kind="jira",
                    source_instance=self._config.source_instance,
                    resource_type="issue",
                    cursor=f"{query_index}:{cursor or 'first'}",
                    next_cursor=(
                        f"{query_index}:{next_cursor}" if next_cursor is not None else None
                    ),
                    is_last=is_last,
                    records=tuple(
                        self._normalize_issue(issue, observed_at) for issue in page_issues
                    ),
                )
        for issue in unique_issues.values():
            issue_id, issue_key = self._issue_identity(issue)
            yield from self._comment_pages(issue_id, issue_key, observed_at)
            yield from self._changelog_pages(issue_id, issue_key, observed_at)
        yield from self._hierarchy_pages(tuple(unique_issues.values()), observed_at)

    def resolved_self_id(self) -> str:
        """Verify and return the configured account before accepting its actor policy."""
        if self._config.account_id is None:
            raise ConfigurationError("Jira self identity requires a configured account_id")
        self._verify_identity()
        return self._config.account_id

    def _verify_identity(self) -> None:
        if self._identity_verified:
            return
        if self._config.account_id is None:
            return
        response = request_with_retry(
            self._client,
            "GET",
            "/rest/api/3/myself",
            policy=self._config.retry_policy,
        )
        document = self._response_mapping(response, "identity")
        if _text(document.get("accountId")) != self._config.account_id:
            raise ScopeViolation("Jira authenticated identity does not match configured account")
        self._identity_verified = True

    def _discovery_queries(self) -> tuple[tuple[str, bool], ...]:
        quoted_projects = ", ".join(f'"{key}"' for key in self._projects)
        exclusive_end = self._config.date_to + timedelta(days=1)
        window = (
            f'updated >= "{self._config.date_from.isoformat()}" '
            f'AND updated < "{exclusive_end.isoformat()}"'
        )
        queries: list[tuple[str, bool]] = []
        if self._config.account_id is not None:
            account = f'"{self._config.account_id}"'
            start = self._config.date_from.isoformat()
            end = exclusive_end.isoformat()
            participation = (
                f'issue in updatedBy({account}, "{start}", "{end}") '
                f"OR reporter = {account} OR creator = {account} OR assignee = {account} "
                f'OR assignee WAS {account} DURING ("{start}", "{end}")'
            )
            queries.append(
                (
                    f"project in ({quoted_projects}) AND {window} "
                    f"AND ({participation}) ORDER BY key ASC",
                    True,
                )
            )
        for offset in range(0, len(self._discovered_keys), self._config.exact_key_chunk_size):
            keys = self._discovered_keys[offset : offset + self._config.exact_key_chunk_size]
            quoted_keys = ", ".join(f'"{key}"' for key in keys)
            queries.append(
                (
                    f"project in ({quoted_projects}) AND key in ({quoted_keys}) ORDER BY key ASC",
                    False,
                )
            )
        return tuple(queries)

    def _search_pages(
        self,
        jql: str,
    ) -> Iterator[tuple[str | None, str | None, bool, list[object]]]:
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            body: dict[str, object] = {
                "jql": jql,
                "maxResults": self._config.page_size,
                "fields": _ISSUE_FIELDS.split(","),
            }
            if cursor is not None:
                body["nextPageToken"] = cursor
            response = request_with_retry(
                self._client,
                "POST",
                "/rest/api/3/search/jql",
                json_body=body,
                policy=self._config.retry_policy,
            )
            document = self._response_mapping(response, "search")
            raw_issues = document.get("issues")
            if not isinstance(raw_issues, list):
                raise PermanentSourceError("Jira search response omitted the issues list")
            next_cursor = _text(document.get("nextPageToken"))
            is_last_value = document.get("isLast")
            is_last = (
                bool(is_last_value) if isinstance(is_last_value, bool) else next_cursor is None
            )
            if not is_last and next_cursor is None:
                raise PermanentSourceError("Jira pagination did not provide a continuation token")
            if next_cursor is not None:
                if next_cursor in seen:
                    raise PermanentSourceError("Jira pagination repeated a continuation token")
                seen.add(next_cursor)
            yield cursor, next_cursor, is_last, raw_issues
            if is_last:
                break
            cursor = next_cursor

    def _hierarchy_pages(
        self,
        discovered_issues: tuple[object, ...],
        observed_at: str,
    ) -> Iterator[NormalizedPage]:
        seen_ids = {self._issue_identity(issue)[0] for issue in discovered_issues}
        queued: set[str] = set()
        queue: list[tuple[str, str | None, int]] = []
        for issue in discovered_issues:
            parent = _mapping(_mapping(issue).get("fields")).get("parent")
            parent_value = _mapping(parent)
            parent_id = _identifier(parent_value.get("id"))
            target = parent_id or _text(parent_value.get("key"))
            if target is not None and target not in seen_ids and target not in queued:
                queue.append((target, parent_id, 1))
                queued.add(target)

        hydrated = 0
        while queue and hydrated < self._config.max_hierarchy_roots:
            target, stable_external_id, depth = queue.pop(0)
            if depth > self._config.max_hierarchy_depth:
                continue
            try:
                response = request_with_retry(
                    self._client,
                    "GET",
                    f"/rest/api/3/issue/{quote(target, safe='')}",
                    params={"fields": _ISSUE_FIELDS},
                    policy=self._config.retry_policy,
                    exact_object=stable_external_id is not None,
                )
            except SourceObjectUnavailable:
                assert stable_external_id is not None
                yield NormalizedPage(
                    source_kind="jira",
                    source_instance=self._config.source_instance,
                    resource_type="issue_hierarchy",
                    cursor=f"{stable_external_id}:{depth}",
                    next_cursor=None,
                    is_last=True,
                    records=(),
                    unavailable_objects=(
                        UnavailableObjectDescriptor(
                            kind="jira_issue",
                            external_id=stable_external_id,
                        ),
                    ),
                )
                continue
            document = self._response_mapping(response, "hierarchy issue")
            issue_id, issue_key = self._issue_identity(document)
            if issue_id in seen_ids:
                continue
            seen_ids.add(issue_id)
            hydrated += 1
            yield NormalizedPage(
                source_kind="jira",
                source_instance=self._config.source_instance,
                resource_type="issue_hierarchy",
                cursor=f"{issue_key}:{depth}",
                next_cursor=None,
                is_last=True,
                records=(self._normalize_issue(document, observed_at),),
            )
            parent = _mapping(_mapping(document.get("fields")).get("parent"))
            parent_id = _identifier(parent.get("id"))
            parent_target = parent_id or _text(parent.get("key"))
            if (
                parent_target is not None
                and parent_target not in seen_ids
                and parent_target not in queued
            ):
                queue.append((parent_target, parent_id, depth + 1))
                queued.add(parent_target)

    def _issue_identity(self, raw_issue: object) -> tuple[str, str]:
        issue = _mapping(raw_issue)
        issue_id = _text(issue.get("id"))
        key = _text(issue.get("key"))
        fields = _mapping(issue.get("fields"))
        project_key = (
            _text(_mapping(fields.get("project")).get("key")) or (key or "").partition("-")[0]
        ).upper()
        if issue_id is None or key is None:
            raise PermanentSourceError("Jira issue omitted its stable identity")
        if project_key not in self._projects or not key.upper().startswith(f"{project_key}-"):
            raise ScopeViolation("Jira returned an issue outside configured projects")
        return issue_id, key

    def _comment_pages(
        self,
        issue_id: str,
        issue_key: str,
        observed_at: str,
    ) -> Iterator[NormalizedPage]:
        endpoint = f"/rest/api/3/issue/{quote(issue_id, safe='')}/comment"
        start_at = 0
        seen: set[int] = set()
        while True:
            response = request_with_retry(
                self._client,
                "GET",
                endpoint,
                params={"startAt": start_at, "maxResults": self._config.page_size},
                policy=self._config.retry_policy,
            )
            document = self._response_mapping(response, "comment")
            comments = document.get("comments")
            if not isinstance(comments, list):
                raise PermanentSourceError("Jira comment response omitted the comments list")
            records = tuple(
                self._normalize_comment(issue_id, issue_key, comment, observed_at)
                for comment in comments
                if self._subresource_in_window(comment, ("updated", "created"), "comment")
            )
            next_start = self._next_offset(document, start_at, len(comments), "comment")
            if next_start is not None:
                if next_start in seen:
                    raise PermanentSourceError("Jira comment pagination repeated an offset")
                seen.add(next_start)
            yield NormalizedPage(
                source_kind="jira",
                source_instance=self._config.source_instance,
                resource_type="issue_comment",
                cursor=f"{issue_key}:{start_at}",
                next_cursor=f"{issue_key}:{next_start}" if next_start is not None else None,
                is_last=next_start is None,
                records=records,
            )
            if next_start is None:
                break
            start_at = next_start

    def _changelog_pages(
        self,
        issue_id: str,
        issue_key: str,
        observed_at: str,
    ) -> Iterator[NormalizedPage]:
        endpoint = f"/rest/api/3/issue/{quote(issue_id, safe='')}/changelog"
        start_at = 0
        seen: set[int] = set()
        while True:
            response = request_with_retry(
                self._client,
                "GET",
                endpoint,
                params={"startAt": start_at, "maxResults": self._config.page_size},
                policy=self._config.retry_policy,
            )
            document = self._response_mapping(response, "changelog")
            histories = document.get("values")
            if not isinstance(histories, list):
                raise PermanentSourceError("Jira changelog response omitted the values list")
            normalized = (
                self._normalize_changelog(issue_id, issue_key, history, observed_at)
                for history in histories
                if self._subresource_in_window(history, ("created",), "changelog")
            )
            records = tuple(record for record in normalized if record is not None)
            next_start = self._next_offset(document, start_at, len(histories), "changelog")
            if next_start is not None:
                if next_start in seen:
                    raise PermanentSourceError("Jira changelog pagination repeated an offset")
                seen.add(next_start)
            yield NormalizedPage(
                source_kind="jira",
                source_instance=self._config.source_instance,
                resource_type="issue_changelog",
                cursor=f"{issue_key}:{start_at}",
                next_cursor=f"{issue_key}:{next_start}" if next_start is not None else None,
                is_last=next_start is None,
                records=records,
            )
            if next_start is None:
                break
            start_at = next_start

    @staticmethod
    def _response_mapping(response: httpx.Response, resource: str) -> Mapping[str, object]:
        try:
            document = response.json()
        except ValueError:
            raise PermanentSourceError(f"Jira returned invalid {resource} JSON") from None
        if not isinstance(document, Mapping):
            raise PermanentSourceError(f"Jira returned an invalid {resource} document")
        return document

    @staticmethod
    def _next_offset(
        document: Mapping[str, object],
        current: int,
        item_count: int,
        resource: str,
    ) -> int | None:
        total = document.get("total")
        returned_start = document.get("startAt")
        max_results = document.get("maxResults")
        if not isinstance(total, int) or total < 0:
            raise PermanentSourceError(f"Jira {resource} pagination omitted a valid total")
        if not isinstance(returned_start, int) or returned_start != current:
            raise PermanentSourceError(f"Jira {resource} pagination returned an invalid offset")
        if not isinstance(max_results, int) or max_results < 1:
            raise PermanentSourceError(f"Jira {resource} pagination omitted a valid page size")
        consumed = returned_start + item_count
        if consumed >= total:
            return None
        if item_count < 1:
            raise PermanentSourceError(f"Jira {resource} pagination made no progress")
        return consumed

    def _subresource_in_window(
        self,
        value: object,
        timestamp_fields: tuple[str, ...],
        resource: str,
    ) -> bool:
        item = _mapping(value)
        raw = next(
            (_text(item.get(field)) for field in timestamp_fields if _text(item.get(field))),
            None,
        )
        if raw is None:
            raise PermanentSourceError(f"Jira {resource} omitted its scope timestamp")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise PermanentSourceError(f"Jira {resource} returned an invalid timestamp") from None
        if parsed.tzinfo is None:
            raise PermanentSourceError(f"Jira {resource} timestamp omitted its timezone")
        return self._window_start <= parsed.astimezone(UTC) < self._window_end

    def _normalize_comment(
        self,
        issue_id: str,
        issue_key: str,
        raw_comment: object,
        observed_at: str,
    ) -> NormalizedRecord:
        comment = _mapping(raw_comment)
        comment_id = _identifier(comment.get("id"))
        if comment_id is None:
            raise PermanentSourceError("Jira comment omitted its stable identity")
        author = _mapping(comment.get("author"))
        account_id = _text(author.get("accountId"))
        participations = (
            (
                Participation(
                    actor=actor_identity(
                        source_kind="jira",
                        source_instance=self._config.source_instance,
                        redactor=self._redactor,
                        provider_actor_id=account_id,
                        display_name=_text(author.get("displayName")),
                        email=_text(author.get("emailAddress")),
                    ),
                    role=ParticipationRole.AUTHOR,
                    effective_from=_text(comment.get("created")),
                ),
            )
            if account_id
            else ()
        )
        return build_record(
            source_kind="jira",
            source_instance=self._config.source_instance,
            object_type="issue_comment",
            external_id=f"{issue_id}:{comment_id}",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=_text(comment.get("updated")) or _text(comment.get("created")),
            payload={
                "id": comment_id,
                "issue_id": issue_id,
                "issue_key": issue_key,
                "body": extract_jira_text(comment.get("body")),
                "created_at": _text(comment.get("created")),
                "updated_at": _text(comment.get("updated")),
            },
            redactor=self._redactor,
            participations=participations,
            references=(self._issue_reference("jira_comment_issue", issue_id),),
            untrusted_text_fields=("body",),
        )

    def _normalize_changelog(
        self,
        issue_id: str,
        issue_key: str,
        raw_history: object,
        observed_at: str,
    ) -> NormalizedRecord | None:
        history = _mapping(raw_history)
        history_id = _identifier(history.get("id"))
        created_at = _text(history.get("created"))
        if history_id is None or created_at is None:
            raise PermanentSourceError("Jira changelog omitted stable identity or time")
        raw_items = history.get("items")
        if not isinstance(raw_items, list):
            raise PermanentSourceError("Jira changelog omitted its items list")
        transitions: list[dict[str, object]] = []
        interval_observations: list[dict[str, object]] = []
        participations: list[Participation] = []
        for raw_item in raw_items:
            item = _mapping(raw_item)
            field = (_text(item.get("field")) or "").casefold()
            if field not in {"status", "assignee"}:
                continue
            from_id = _identifier(item.get("from"))
            to_id = _identifier(item.get("to"))
            from_value = _text(item.get("fromString"))
            to_value = _text(item.get("toString"))
            transitions.append(
                {
                    "field": field,
                    "from_id": from_id,
                    "from_value": from_value,
                    "to_id": to_id,
                    "to_value": to_value,
                    "changed_at": created_at,
                }
            )
            if from_id or from_value:
                interval_observations.append(
                    {
                        "field": field,
                        "value_id": from_id,
                        "value": from_value,
                        "effective_to": created_at,
                    }
                )
            if to_id or to_value:
                interval_observations.append(
                    {
                        "field": field,
                        "value_id": to_id,
                        "value": to_value,
                        "effective_from": created_at,
                    }
                )
            if field == "assignee":
                if from_id:
                    participations.append(
                        self._jira_actor_participation(from_id, from_value, effective_to=created_at)
                    )
                if to_id:
                    participations.append(
                        self._jira_actor_participation(to_id, to_value, effective_from=created_at)
                    )
        if not transitions:
            return None
        author = _mapping(history.get("author"))
        author_id = _text(author.get("accountId"))
        if author_id:
            participations.append(
                Participation(
                    actor=actor_identity(
                        source_kind="jira",
                        source_instance=self._config.source_instance,
                        redactor=self._redactor,
                        provider_actor_id=author_id,
                        display_name=_text(author.get("displayName")),
                        email=_text(author.get("emailAddress")),
                    ),
                    role=ParticipationRole.AUTHOR,
                    effective_from=created_at,
                )
            )
        return build_record(
            source_kind="jira",
            source_instance=self._config.source_instance,
            object_type="issue_changelog",
            external_id=f"{issue_id}:{history_id}",
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=created_at,
            payload={
                "id": history_id,
                "issue_id": issue_id,
                "issue_key": issue_key,
                "created_at": created_at,
                "transitions": transitions,
                "interval_observations": interval_observations,
            },
            redactor=self._redactor,
            participations=participations,
            references=(self._issue_reference("jira_changelog_issue", issue_id),),
            untrusted_text_fields=("transitions[].from_value", "transitions[].to_value"),
        )

    def _jira_actor_participation(
        self,
        actor_id: str,
        display_name: str | None,
        *,
        effective_from: str | None = None,
        effective_to: str | None = None,
    ) -> Participation:
        return Participation(
            actor=actor_identity(
                source_kind="jira",
                source_instance=self._config.source_instance,
                redactor=self._redactor,
                provider_actor_id=actor_id,
                display_name=display_name,
            ),
            role=ParticipationRole.ASSIGNEE,
            effective_from=effective_from,
            effective_to=effective_to,
        )

    def _issue_in_window(self, raw_issue: object) -> bool:
        raw_updated = _text(_mapping(_mapping(raw_issue).get("fields")).get("updated"))
        if raw_updated is None:
            raise PermanentSourceError("Jira issue omitted its scope timestamp")
        try:
            parsed = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
        except ValueError:
            raise PermanentSourceError("Jira issue returned an invalid timestamp") from None
        if parsed.tzinfo is None:
            raise PermanentSourceError("Jira issue timestamp omitted its timezone")
        timestamp = parsed.astimezone(UTC)
        return self._window_start <= timestamp < self._window_end

    def _normalize_issue(self, raw_issue: object, observed_at: str) -> NormalizedRecord:
        issue = _mapping(raw_issue)
        issue_id, key = self._issue_identity(issue)
        fields = _mapping(issue.get("fields"))
        project_key = (
            _text(_mapping(fields.get("project")).get("key")) or key.partition("-")[0]
        ).upper()

        participations: list[Participation] = []
        for field_name, role in (
            ("assignee", ParticipationRole.ASSIGNEE),
            ("reporter", ParticipationRole.REPORTER),
            ("creator", ParticipationRole.CREATOR),
        ):
            actor = _mapping(fields.get(field_name))
            account_id = _text(actor.get("accountId"))
            if account_id is None:
                continue
            participations.append(
                Participation(
                    actor=actor_identity(
                        source_kind="jira",
                        source_instance=self._config.source_instance,
                        redactor=self._redactor,
                        provider_actor_id=account_id,
                        display_name=_text(actor.get("displayName")),
                        email=_text(actor.get("emailAddress")),
                    ),
                    role=role,
                )
            )

        references: list[Reference] = []
        issue_type = _mapping(fields.get("issuetype"))
        is_subtask = issue_type.get("subtask") is True
        parent = _mapping(fields.get("parent"))
        parent_id = _identifier(parent.get("id"))
        parent_key = _text(parent.get("key"))
        if is_subtask and (parent_id or parent_key):
            references.append(
                self._issue_reference("jira_subtask_of", parent_id or parent_key or "")
            )
        elif parent_id or parent_key:
            references.append(
                self._issue_reference("jira_hierarchy_context", parent_id or parent_key or "")
            )
        subtasks = fields.get("subtasks")
        true_subtasks: list[dict[str, str | None]] = []
        if isinstance(subtasks, list):
            for subtask in subtasks:
                subtask_value = _mapping(subtask)
                subtask_type = _mapping(_mapping(subtask_value.get("fields")).get("issuetype"))
                if subtask_type.get("subtask") is not True:
                    continue
                subtask_id = _identifier(subtask_value.get("id"))
                subtask_key = _text(subtask_value.get("key"))
                if subtask_id or subtask_key:
                    target = subtask_id or subtask_key or ""
                    references.append(self._issue_reference("jira_parent_of", target))
                    true_subtasks.append({"id": subtask_id, "key": subtask_key})
        links = fields.get("issuelinks")
        if isinstance(links, list):
            for link in links:
                link_mapping = _mapping(link)
                linked = _mapping(
                    link_mapping.get("outwardIssue") or link_mapping.get("inwardIssue")
                )
                linked_id = _identifier(linked.get("id"))
                linked_key = _text(linked.get("key"))
                if linked_id or linked_key:
                    references.append(
                        self._issue_reference("jira_links_to_issue", linked_id or linked_key or "")
                    )

        status = _mapping(fields.get("status"))
        priority = _mapping(fields.get("priority"))
        labels = fields.get("labels")
        components = fields.get("components")
        fix_versions = fields.get("fixVersions")
        summary = _text(fields.get("summary"))
        description = extract_jira_text(fields.get("description"))
        return build_record(
            source_kind="jira",
            source_instance=self._config.source_instance,
            object_type="issue",
            external_id=issue_id,
            app_id=self._config.app_id,
            observed_at=observed_at,
            source_updated_at=_text(fields.get("updated")),
            payload={
                "id": issue_id,
                "key": key,
                "project_key": project_key,
                "summary": summary,
                "description": description,
                "status": _text(status.get("name")),
                "status_category": _text(_mapping(status.get("statusCategory")).get("key")),
                "issue_type": _text(issue_type.get("name")),
                "is_subtask": is_subtask,
                "parent": {"id": parent_id, "key": parent_key} if parent_id or parent_key else None,
                "subtasks": true_subtasks,
                "priority": _text(priority.get("name")),
                "created_at": _text(fields.get("created")),
                "updated_at": _text(fields.get("updated")),
                "resolution_at": _text(fields.get("resolutiondate")),
                "labels": [item for item in labels if isinstance(item, str)]
                if isinstance(labels, list)
                else [],
                "components": [
                    name
                    for item in components
                    if (name := _text(_mapping(item).get("name"))) is not None
                ]
                if isinstance(components, list)
                else [],
                "fix_versions": [
                    {
                        "id": _identifier(version.get("id")),
                        "name": _text(version.get("name")),
                        "released": version.get("released")
                        if isinstance(version.get("released"), bool)
                        else None,
                        "archived": version.get("archived")
                        if isinstance(version.get("archived"), bool)
                        else None,
                        "release_date": _text(version.get("releaseDate")),
                    }
                    for item in fix_versions
                    if (version := _mapping(item))
                ]
                if isinstance(fix_versions, list)
                else [],
            },
            redactor=self._redactor,
            participations=participations,
            references=references,
            untrusted_text_fields=("summary", "description", "fix_versions[].name"),
        )

    @staticmethod
    def _issue_reference(reference_type: str, external_id: str) -> Reference:
        return Reference(
            reference_type=reference_type,
            target_external_id=external_id,
            strength=ReferenceStrength.STRUCTURED,
            target_source_kind="jira",
            target_object_type="issue",
        )
