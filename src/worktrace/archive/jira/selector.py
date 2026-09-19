"""Preview-bound Jira root and one-hop context selection."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from worktrace.archive.jira.provider import MetadataPage, ProviderIdentity
from worktrace.archive.jira.repository import site_id_for_origin
from worktrace.errors import (
    ConfigurationError,
    PermanentSourceError,
    PermissionDenied,
    ScopeViolation,
)
from worktrace.vault.format import canonical_json

TOKEN_DOMAIN = b"worktrace:jira-scope-approval:v1\0"
PREVIEW_SCHEMA_VERSION = 1
POLICY_VERSION = 1


class SelectionProvider(Protocol):
    def search_metadata(self, jql: str, *, next_token: str | None = None) -> MetadataPage: ...

    def assignment_changelog(self, issue_id: str, *, start_at: int = 0) -> dict[str, object]: ...

    def issue_metadata(self, issue_id: str) -> dict[str, object]: ...

    def verify_identity(self, expected_account_id: str | None = None) -> ProviderIdentity: ...


@dataclass(frozen=True, slots=True)
class PreviewSelection:
    site_id: str
    account_id: str
    interval_from: date
    interval_to: date
    timezone: str
    policy_version: int
    roots: tuple[dict[str, object], ...]
    context: tuple[dict[str, object], ...]
    unresolved_refs: tuple[dict[str, object], ...]
    attachment_estimate: dict[str, object]
    limitations: tuple[str, ...]
    scope_hash: str
    provider_view_hash: str
    preview_id: str
    approval_token: str
    config_fingerprint: str
    provider_view: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": PREVIEW_SCHEMA_VERSION,
            "site_id": self.site_id,
            "account_id": self.account_id,
            "interval": {
                "from": self.interval_from.isoformat(),
                "to": self.interval_to.isoformat(),
                "timezone": self.timezone,
            },
            "policy_version": self.policy_version,
            "roots": list(self.roots),
            "context": list(self.context),
            "unresolved_refs": list(self.unresolved_refs),
            "attachment_estimate": self.attachment_estimate,
            "limitations": list(self.limitations),
            "scope_hash": self.scope_hash,
            "provider_view_hash": self.provider_view_hash,
            "preview_id": self.preview_id,
            "approval_token": self.approval_token,
        }


def token_hash(token: str) -> str:
    raw = _decode_token(token)
    return hashlib.sha256(TOKEN_DOMAIN + raw).hexdigest()


def _decode_token(token: str) -> bytes:
    if not isinstance(token, str) or len(token) != 43 or "=" in token:
        raise ConfigurationError("approval token must be 43-character base64url")
    try:
        raw = base64.urlsafe_b64decode(token + "=")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ConfigurationError("approval token is invalid base64url") from exc
    if len(raw) != 32:
        raise ConfigurationError("approval token must encode 256 bits")
    return raw


def _new_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value.isdigit() else None


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def sanitize_metadata(raw: dict[str, object]) -> dict[str, object]:
    """Project preview metadata before it can reach persistence or output."""
    fields = _mapping(raw.get("fields"))
    issue_id = _id(raw.get("id"))
    key = _text(raw.get("key"))
    project = _mapping(fields.get("project"))
    result: dict[str, object] = {
        "issue_id": issue_id,
        "issue_key": key,
        "project_id": _id(project.get("id")),
        "project_key": _text(project.get("key")),
        "parent": _relation(_mapping(fields.get("parent"))),
        "subtasks": _relations(fields.get("subtasks")),
        "issue_links": _links(fields.get("issuelinks")),
        "attachments": _attachments(fields.get("attachment")),
    }
    if issue_id is None or key is None or result["project_key"] is None:
        raise PermanentSourceError("Jira preview issue omitted stable identity")
    return result


def _relation(value: dict[str, object]) -> dict[str, object] | None:
    issue_id = _id(value.get("id"))
    key = _text(value.get("key"))
    if issue_id is None and key is None:
        return None
    return {"issue_id": issue_id, "issue_key": key}


def _relations(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result = [_relation(_mapping(item)) for item in value]
    return [item for item in result if item is not None]


def _links(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for raw in value:
        link = _mapping(raw)
        link_type = _mapping(link.get("type"))
        for direction in ("outwardIssue", "inwardIssue"):
            endpoint = _relation(_mapping(link.get(direction)))
            if endpoint is not None:
                result.append(
                    {
                        "link_id": _text(link.get("id")),
                        "link_type_id": _text(link_type.get("id")),
                        "link_type_name": _text(link_type.get("name")),
                        "direction": direction,
                        **endpoint,
                    }
                )
    return result


def _attachments(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, object]] = []
    for raw in value:
        item = _mapping(raw)
        attachment_id = _id(item.get("id"))
        if attachment_id is None:
            continue
        size = item.get("size")
        result.append(
            {
                "attachment_id": attachment_id,
                "declared_size": size if isinstance(size, int) and size >= 0 else None,
                "mime_type": _text(item.get("mimeType")),
            }
        )
    return result


def _timestamp(value: object) -> datetime | None:
    text = _text(value)
    if text is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def assignment_overlap(
    changelog_pages: list[dict[str, object]],
    account_id: str,
    start: datetime,
    end: datetime,
) -> str:
    """Return verified_overlap, boundary_unknown, or excluded."""
    events: list[tuple[datetime, str | None, str | None]] = []
    complete = True
    for page in changelog_pages:
        values = page.get("values")
        if not isinstance(values, list):
            complete = False
            continue
        for history in values:
            item = _mapping(history)
            at = _timestamp(item.get("created"))
            if at is None:
                complete = False
                continue
            changes = item.get("items")
            if not isinstance(changes, list):
                changes = []
            for change in changes:
                change_map = _mapping(change)
                if _text(change_map.get("field")) != "assignee":
                    continue
                before = _text(change_map.get("from")) or _text(change_map.get("fromString"))
                after = _text(change_map.get("to")) or _text(change_map.get("toString"))
                events.append((at, before, after))
    events.sort(key=lambda item: item[0])
    if not complete or not events:
        return "boundary_unknown"
    for index, (at, before, after) in enumerate(events):
        interval_end = events[index + 1][0] if index + 1 < len(events) else end
        holder = after or before
        if holder == account_id and at < end and interval_end > start:
            return "verified_overlap"
    return "excluded"


class JiraSelector:
    def __init__(
        self,
        provider: SelectionProvider,
        *,
        origin: str,
        account_id: str,
        date_from: date,
        date_to: date,
        timezone: str,
        config_fingerprint: str,
        policy_version: int = POLICY_VERSION,
        context_depth: int = 1,
    ) -> None:
        if context_depth != 1:
            raise ConfigurationError("Jira archive context-depth must be exactly one")
        if date_from > date_to:
            raise ConfigurationError("Jira archive interval is invalid")
        try:
            self.zone = ZoneInfo(timezone)
        except Exception as exc:
            raise ConfigurationError("Jira archive timezone is invalid") from exc
        self.provider = provider
        self.origin = origin
        self.account_id = account_id
        self.date_from = date_from
        self.date_to = date_to
        self.config_fingerprint = config_fingerprint
        self.policy_version = policy_version
        self.start = datetime.combine(date_from, time.min, tzinfo=self.zone).astimezone(UTC)
        self.end = datetime.combine(
            date_to + timedelta(days=1), time.min, tzinfo=self.zone
        ).astimezone(UTC)

    def preview(self) -> PreviewSelection:
        identity = self.provider.verify_identity(self.account_id)
        roots: dict[str, dict[str, object]] = {}
        page_markers: list[dict[str, object]] = []
        jql_start = (self.start.astimezone(self.zone).date() - timedelta(days=1)).isoformat()
        jql_end = (self.end.astimezone(self.zone).date() + timedelta(days=1)).isoformat()
        jql = (
            f'assignee WAS "{self.account_id}" DURING ("{jql_start}", "{jql_end}") ORDER BY id ASC'
        )
        token: str | None = None
        while True:
            page = self.provider.search_metadata(jql, next_token=token)
            page_markers.append({"next_token": page.next_token, "is_last": page.is_last})
            for raw in page.issues:
                projection = sanitize_metadata(raw)
                issue_id = str(projection["issue_id"])
                roots.setdefault(issue_id, projection)
            if page.is_last:
                break
            if page.next_token is None:
                raise PermanentSourceError("Jira preview pagination stopped without a token")
            token = page.next_token

        selected: list[dict[str, object]] = []
        limitations: list[str] = []
        for issue_id, projection in sorted(roots.items()):
            histories: list[dict[str, object]] = []
            start_at = 0
            while True:
                changelog_page = self.provider.assignment_changelog(issue_id, start_at=start_at)
                histories.append(changelog_page)
                values = changelog_page.get("values")
                if not isinstance(values, list):
                    break
                total = changelog_page.get("total")
                if not isinstance(total, int) or start_at + len(values) >= total:
                    break
                if not values:
                    break
                start_at += len(values)
            boundary = assignment_overlap(histories, self.account_id, self.start, self.end)
            if boundary != "excluded":
                selected.append(
                    {
                        "issue_id": issue_id,
                        "issue_key": projection["issue_key"],
                        "project_id": projection["project_id"],
                        "project_key": projection["project_key"],
                        "role": "root",
                        "selection_reason": "verified_assignment_overlap"
                        if boundary == "verified_overlap"
                        else "assignment_boundary_unknown",
                        "assignment_status": boundary,
                        "boundary_status": boundary,
                        "attachments": projection["attachments"],
                        "metadata": projection,
                    }
                )
                if boundary == "boundary_unknown":
                    limitations.append(f"assignment boundary unknown for Jira issue {issue_id}")

        context, unresolved = self._context(selected)
        roots_public = tuple(_public_issue(item) for item in selected)
        context_public = tuple(context)
        attachments = [
            item for root in selected for item in cast(list[dict[str, object]], root["attachments"])
        ]
        provider_view: dict[str, object] = {
            "roots": roots_public,
            "context": context_public,
            "unresolved_refs": unresolved,
            "pagination": page_markers,
        }
        provider_hash = "sha256:" + hashlib.sha256(canonical_json(provider_view)).hexdigest()
        scope_view = {
            "site_id": site_id_for_origin(self.origin),
            "account_id": identity.account_id,
            "interval": {
                "from": self.date_from.isoformat(),
                "to": self.date_to.isoformat(),
                "timezone": self.zone.key,
            },
            "policy_version": self.policy_version,
            "roots": roots_public,
            "context": context_public,
            "provider_view_hash": provider_hash,
            "config_fingerprint": self.config_fingerprint,
        }
        scope_hash = "sha256:" + hashlib.sha256(canonical_json(scope_view)).hexdigest()
        approval_token = _new_token()
        return PreviewSelection(
            site_id=cast(str, scope_view["site_id"]),
            account_id=identity.account_id,
            interval_from=self.date_from,
            interval_to=self.date_to,
            timezone=self.zone.key,
            policy_version=self.policy_version,
            roots=roots_public,
            context=context_public,
            unresolved_refs=tuple(unresolved),
            attachment_estimate={
                "visible_count": len(attachments),
                "visible_bytes": sum(
                    int(cast(int, item["declared_size"]))
                    for item in attachments
                    if item["declared_size"] is not None
                ),
                "unknown_size_count": sum(item["declared_size"] is None for item in attachments),
            },
            limitations=tuple(sorted(set(limitations))),
            scope_hash=scope_hash,
            provider_view_hash=provider_hash,
            preview_id="preview:" + secrets.token_hex(16),
            approval_token=approval_token,
            config_fingerprint=self.config_fingerprint,
            provider_view=provider_view,
        )

    def _context(
        self, roots: list[dict[str, object]]
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        root_ids = {str(item["issue_id"]) for item in roots}
        context: dict[tuple[str, str], dict[str, object]] = {}
        unresolved: dict[tuple[str, str], dict[str, object]] = {}
        for root in roots:
            metadata = cast(dict[str, object], root["metadata"])
            relations: list[tuple[str, str, dict[str, object]]] = []
            parent = metadata.get("parent")
            if isinstance(parent, dict):
                relations.append(("jira_parent_of", "parent", parent))
            subtasks = metadata.get("subtasks")
            if not isinstance(subtasks, list):
                subtasks = []
            for subtask in subtasks:
                if isinstance(subtask, dict):
                    relations.append(("jira_subtask_of", "subtask", subtask))
            links = metadata.get("issue_links")
            if not isinstance(links, list):
                links = []
            for link in links:
                if isinstance(link, dict):
                    link_name = link.get("link_type_name") or link.get("link_type_id") or "unknown"
                    relations.append(
                        (
                            f"jira_issue_link:{link_name}",
                            "link",
                            link,
                        )
                    )
            for relationship, _, target in relations:
                target_id = _id(target.get("issue_id"))
                if target_id is None or target_id in root_ids:
                    continue
                try:
                    target_projection = sanitize_metadata(self.provider.issue_metadata(target_id))
                except PermissionDenied:
                    unresolved[(target_id, relationship)] = {
                        "issue_id": target_id,
                        "relationship": relationship,
                        "status": "unavailable",
                    }
                    continue
                except (PermanentSourceError, ScopeViolation) as exc:
                    unresolved[(target_id, relationship)] = {
                        "issue_id": target_id,
                        "relationship": relationship,
                        "status": "unavailable",
                        "reason": type(exc).__name__,
                    }
                    continue
                item = {
                    "issue_id": target_id,
                    "issue_key": target_projection["issue_key"],
                    "project_id": target_projection["project_id"],
                    "project_key": target_projection["project_key"],
                    "role": "context",
                    "relationship": relationship,
                    "selection_reason": "one_hop_direct_context",
                    "assignment_status": "not_applicable",
                    "boundary_status": "not_applicable",
                    "attachments": target_projection["attachments"],
                    "metadata": target_projection,
                }
                context.setdefault((target_id, relationship), _public_issue(item))
        return [context[key] for key in sorted(context)], [
            unresolved[key] for key in sorted(unresolved)
        ]


def _public_issue(item: dict[str, object]) -> dict[str, object]:
    return {
        key: item[key]
        for key in (
            "issue_id",
            "issue_key",
            "project_id",
            "project_key",
            "role",
            "relationship",
            "selection_reason",
            "assignment_status",
            "boundary_status",
            "attachments",
        )
        if key in item
    }
