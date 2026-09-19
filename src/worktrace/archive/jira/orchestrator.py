"""Write-owned Jira archive preview, collection, resume, and status orchestration."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any, BinaryIO, cast

from worktrace.archive.jira.provider import JiraArchiveProvider
from worktrace.archive.jira.repository import JiraArchiveRepository, site_id_for_origin
from worktrace.archive.jira.selector import (
    JiraSelector,
    PreviewSelection,
    sanitize_metadata,
    token_hash,
)
from worktrace.config import WorkTraceConfig
from worktrace.errors import (
    ConfigurationError,
    InvalidCredentials,
    PermanentSourceError,
    PermissionDenied,
    RecoveryError,
    RetryExhausted,
    SourceObjectUnavailable,
    VaultIntegrityError,
)
from worktrace.vault.format import VaultDescriptor, canonical_json, write_vault_object

APP_MAPPING_STATUS = "not_configured"
NOT_IMPLEMENTED_STATUS = "not_implemented"
COMPLETE_OUTCOMES = frozenset({"complete", "complete_with_unavailable_resources"})
ACTION_OUTCOMES = frozenset({"paused", "partial", "unstable_partial"})
RESOURCE_TERMINAL = frozenset({"complete", "unavailable", "unsupported", "failed", "paused"})
TRANSFER_BUDGET_BYTES = 20 * 1024 * 1024 * 1024
FREE_SPACE_FLOOR_BYTES = 2 * 1024 * 1024 * 1024


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json(cast(Mapping[str, object], value))).hexdigest()


def _fingerprint(configuration: WorkTraceConfig) -> str:
    try:
        return "sha256:" + hashlib.sha256(configuration.config_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ConfigurationError("Jira archive configuration cannot be fingerprinted") from exc


def _object_path(
    root: Path, site_id: str, collection_id: str, revision_id: str, object_id: str
) -> Path:
    safe = hashlib.sha256(object_id.encode("utf-8")).hexdigest()
    return root / site_id / collection_id / revision_id / f"{safe}.wtva"


class _ResponseReader:
    def __init__(self, response: Any, *, collector: JiraCollector) -> None:
        self._chunks = iter(response.iter_bytes(chunk_size=1024 * 1024))
        self._buffer = bytearray()
        self._done = False
        self._collector = collector

    def read(self, size: int = -1) -> bytes:
        if size == 0:
            return b""
        while not self._done and (size < 0 or len(self._buffer) < size):
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._done = True
                break
            if not isinstance(chunk, bytes):
                raise VaultIntegrityError("Jira attachment stream returned non-bytes")
            self._collector._account_transfer(len(chunk))
            self._buffer.extend(chunk)
        if size < 0:
            result = bytes(self._buffer)
            self._buffer.clear()
            return result
        result = bytes(self._buffer[:size])
        del self._buffer[:size]
        return result


@dataclass(frozen=True, slots=True)
class CollectorLimits:
    transfer_budget_bytes: int = TRANSFER_BUDGET_BYTES
    free_space_floor_bytes: int = FREE_SPACE_FLOOR_BYTES
    max_downloads: int = 2


DEFAULT_LIMITS = CollectorLimits()


class CollectionPaused(Exception):
    """Durable pause requested by a collection budget or free-space guard."""


def outcome_exit_code(outcome: str) -> int:
    if outcome in COMPLETE_OUTCOMES:
        return 0
    if outcome in ACTION_OUTCOMES:
        return 2
    if outcome in {"failed", "preflight_failed"}:
        return 1
    if outcome in {"invalid", "incompatible"}:
        return 3
    return 1


class AttachmentIntegrityError(Exception):
    """The two authenticated attachment passes did not describe one object."""


class ScopeExpansionRequired(Exception):
    """Current issue hydration discovered an unapproved direct context target."""


class JiraCollector:
    """Archive collector owning all Jira-specific SQLite writes."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        configuration: WorkTraceConfig,
        provider: JiraArchiveProvider,
        *,
        vault_root: Path | None = None,
        vault_key: bytes | None = None,
        key_version: int = 1,
        limits: CollectorLimits = DEFAULT_LIMITS,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.connection = connection
        self.configuration = configuration
        self.provider = provider
        self.vault_root = (
            (vault_root or configuration.data_directory / "jira-vault").expanduser().resolve()
        )
        self.vault_key = vault_key
        self.key_version = key_version
        self.limits = limits
        self.clock = clock
        self._transferred = 0
        self._download_count = 0
        self._site_id = site_id_for_origin(provider.origin)
        self._config_fingerprint = _fingerprint(configuration)

    def preview(self) -> dict[str, object]:
        selection = self._fresh_selection()
        self._persist_preview(selection)
        return selection.as_dict()

    def collect(self, approval_token: str) -> dict[str, object]:
        if not self.vault_key or len(self.vault_key) != 32:
            raise ConfigurationError("Jira collection requires a verified 32-byte vault key")
        # Re-run the metadata-only preview before consuming anything. This is
        # the provider-view freshness gate; the raw token never crosses it.
        fresh = self._fresh_selection()
        collection_id, revision_id = self._consume_preview(approval_token, fresh)
        return self._run_collection(collection_id, revision_id, fresh)

    def resume(self, collection_id: str | None = None) -> dict[str, object]:
        row = self._latest_collection(collection_id)
        if row is None:
            raise ConfigurationError("no resumable Jira collection exists")
        if str(row["run_status"]) not in {"paused", "partial", "unstable_partial", "running"}:
            raise ConfigurationError("Jira collection is not resumable")
        scope = json.loads(str(row["scope_json"]))
        fresh_selection = self._fresh_selection()
        if not self._resume_scope_matches(row, scope, fresh_selection):
            with self.connection:
                self.connection.execute(
                    "UPDATE jira_collection_runs SET status='paused', error_json=? WHERE id=?",
                    (_json({"reason": "new_preview_required"}), row["run_id"]),
                )
            return self.status(collection_id)
        roots = tuple(cast(list[dict[str, object]], scope.get("roots", [])))
        context = tuple(cast(list[dict[str, object]], scope.get("context", [])))
        fresh = PreviewSelection(
            site_id=str(row["site_id"]),
            account_id=str(scope["account_id"]),
            interval_from=self.configuration.employment_from,
            interval_to=self.configuration.employment_to,
            timezone=self.configuration.employment_timezone,
            policy_version=int(row["policy_version"]),
            roots=roots,
            context=context,
            unresolved_refs=tuple(cast(list[dict[str, object]], scope.get("unresolved_refs", []))),
            attachment_estimate=cast(dict[str, object], scope.get("attachment_estimate", {})),
            limitations=tuple(cast(list[str], scope.get("limitations", []))),
            scope_hash=str(row["scope_hash"]),
            provider_view_hash=str(scope["provider_view_hash"]),
            preview_id="resume",
            approval_token="",
            config_fingerprint=str(row["config_fingerprint"]),
            provider_view=cast(dict[str, object], scope.get("provider_view", {})),
        )
        new_run, new_revision = self._new_revision(str(row["collection_id"]), fresh)
        return self._run_collection(str(row["collection_id"]), new_revision, fresh, run_id=new_run)

    def status(self, collection_id: str | None = None) -> dict[str, object]:
        row = self._latest_collection(collection_id)
        if row is None:
            raise ConfigurationError("Jira collection was not found")
        lineage = self._lineage_revisions(str(row["revision_id"]))
        placeholders = ",".join("?" for _ in lineage)
        resources = self.connection.execute(
            f"SELECT id, logical_resource_id, state, completeness FROM jira_resource_states "
            f"WHERE revision_id IN ({placeholders}) ORDER BY revision_id",
            tuple(lineage),
        ).fetchall()
        attachments = self.connection.execute(
            f"SELECT id, logical_resource_id, original_state AS state, "
            f"original_state AS completeness FROM jira_attachment_objects "
            f"WHERE revision_id IN ({placeholders}) ORDER BY revision_id",
            tuple(lineage),
        ).fetchall()
        latest_resources: dict[str, tuple[str, str]] = {}
        for resource in [*resources, *attachments]:
            logical = str(resource["logical_resource_id"] or resource["id"])
            latest_resources[logical] = (
                str(resource["state"]),
                str(resource["completeness"]),
            )
        counts: dict[str, int] = {
            key: 0 for key in ("complete", "partial", "unavailable", "unstable")
        }
        for state, completeness in latest_resources.values():
            if state in counts:
                counts[state] += 1
            elif completeness == "partial":
                counts["partial"] += 1
        outcome = str(row["run_status"])
        if outcome == "complete" and counts["unavailable"]:
            outcome = "complete_with_unavailable_resources"
        error_document = json.loads(str(row["error_json"])) if row["error_json"] else {}
        next_action = (
            "new_preview_required"
            if error_document.get("reason")
            in {
                "new_preview_required",
                "scope_expansion_required",
            }
            else ("resume" if outcome in {"paused", "partial", "unstable_partial"} else None)
        )
        return {
            "schema_version": 1,
            "collection_id": str(row["collection_id"]),
            "run_id": str(row["run_id"]),
            "revision_id": str(row["revision_id"]),
            "collection_outcome": outcome,
            "status": outcome,
            "dimensions": {
                "selection": "complete",
                "enumeration": "complete"
                if outcome not in {"failed", "preflight_failed"}
                else "failed",
                "original_availability": "complete_with_unavailable_resources"
                if counts["unavailable"]
                else "complete",
                "download_integrity": "complete_with_unavailable_resources"
                if counts["unavailable"]
                else "complete",
                "extraction": NOT_IMPLEMENTED_STATUS,
                "search_readiness": NOT_IMPLEMENTED_STATUS,
                "app_mapping": APP_MAPPING_STATUS,
            },
            "resource_counts": counts,
            "as_of": _iso(self.clock()),
            "next_action": next_action,
            "limitations": list(
                cast(list[str], json.loads(str(row["scope_json"])).get("limitations", []))
            ),
        }

    def _fresh_selection(self) -> PreviewSelection:
        account_id = self.configuration.identity.jira_account_id
        if not account_id:
            raise ConfigurationError("Jira archive requires identity.jira_account_id")
        return JiraSelector(
            self.provider,
            origin=self.provider.origin,
            account_id=account_id,
            date_from=self.configuration.employment_from,
            date_to=self.configuration.employment_to,
            timezone=self.configuration.employment_timezone,
            config_fingerprint=self._config_fingerprint,
        ).preview()

    def _resume_scope_matches(
        self,
        row: sqlite3.Row,
        scope: Mapping[str, object],
        fresh: PreviewSelection,
    ) -> bool:
        interval = cast(dict[str, object], scope.get("interval", {}))
        stored = {
            "site_id": str(row["site_id"]),
            "account_id": scope.get("account_id"),
            "config_fingerprint": str(row["config_fingerprint"]),
            "interval": interval,
            "policy_version": int(row["policy_version"]),
            "scope_hash": str(row["scope_hash"]),
            "provider_view_hash": scope.get("provider_view_hash"),
            "roots": scope.get("roots", []),
            "context": scope.get("context", []),
        }
        current = {
            "site_id": fresh.site_id,
            "account_id": fresh.account_id,
            "config_fingerprint": fresh.config_fingerprint,
            "interval": {
                "from": fresh.interval_from.isoformat(),
                "to": fresh.interval_to.isoformat(),
                "timezone": fresh.timezone,
            },
            "policy_version": fresh.policy_version,
            "scope_hash": fresh.scope_hash,
            "provider_view_hash": fresh.provider_view_hash,
            "roots": list(fresh.roots),
            "context": list(fresh.context),
        }
        return stored == current

    def _persist_preview(self, selection: PreviewSelection) -> None:
        repository = JiraArchiveRepository(self.connection)
        repository.ensure_site(self.provider.origin)
        now = self.clock()
        with self.connection:
            self.connection.execute(
                "INSERT INTO jira_scope_previews "
                "(preview_id, token_hash, scope_hash, provider_view_hash, config_fingerprint, "
                "site_id, verified_account_id, created_at, expires_at, consumed_at, "
                "preview_status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'pending')",
                (
                    selection.preview_id,
                    token_hash(selection.approval_token),
                    selection.scope_hash,
                    selection.provider_view_hash,
                    selection.config_fingerprint,
                    selection.site_id,
                    selection.account_id,
                    _iso(now),
                    _iso(now + timedelta(seconds=900)),
                ),
            )

    def _consume_preview(self, approval_token: str, fresh: PreviewSelection) -> tuple[str, str]:
        digest = token_hash(approval_token)
        now = self.clock()
        self.connection.commit()
        self.connection.rollback()
        self.connection.autocommit = True
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            "SELECT * FROM jira_scope_previews WHERE token_hash=? AND preview_status='pending' "
            "AND consumed_at IS NULL",
            (digest,),
        ).fetchone()
        if row is None:
            self.connection.rollback()
            self.connection.autocommit = False
            raise PermissionDenied("approval token is forged, stale, expired, or already used")
        try:
            expires = datetime.fromisoformat(str(row["expires_at"]))
        except ValueError:
            self.connection.rollback()
            self.connection.autocommit = False
            raise RecoveryError("stored Jira preview expiry is invalid") from None
        if expires <= now or any(
            str(row[field]) != expected
            for field, expected in (
                ("scope_hash", fresh.scope_hash),
                ("provider_view_hash", fresh.provider_view_hash),
                ("config_fingerprint", fresh.config_fingerprint),
                ("site_id", fresh.site_id),
                ("verified_account_id", fresh.account_id),
            )
        ):
            self.connection.rollback()
            self.connection.autocommit = False
            raise PermissionDenied("approval token no longer matches the fresh Jira scope")
        now_text = _iso(now)
        self.connection.execute(
            "UPDATE jira_scope_previews SET consumed_at=?, preview_status='consumed' "
            "WHERE preview_id=? AND consumed_at IS NULL",
            (now_text, row["preview_id"]),
        )
        collection_id = f"jcol:{uuid.uuid4()}"
        run_id = f"jrun:{uuid.uuid4()}"
        revision_number = int(
            self.connection.execute(
                "SELECT COALESCE(MAX(revision_number), 0) + 1 FROM jira_archive_revisions "
                "WHERE collection_id=?",
                (collection_id,),
            ).fetchone()[0]
        )
        revision_id = f"jrev:{collection_id}:{revision_number}"
        scope = {
            "account_id": fresh.account_id,
            "interval": {
                "from": fresh.interval_from.isoformat(),
                "to": fresh.interval_to.isoformat(),
                "timezone": fresh.timezone,
            },
            "roots": list(fresh.roots),
            "context": list(fresh.context),
            "unresolved_refs": list(fresh.unresolved_refs),
            "attachment_estimate": fresh.attachment_estimate,
            "limitations": list(fresh.limitations),
            "provider_view_hash": fresh.provider_view_hash,
            "provider_view": fresh.provider_view,
        }
        vault_id = "vault:" + hashlib.sha256(fresh.site_id.encode()).hexdigest()[:32]
        self.connection.execute(
            "INSERT INTO jira_collections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (
                collection_id,
                fresh.site_id,
                _json(scope),
                fresh.scope_hash,
                digest,
                fresh.policy_version,
                fresh.config_fingerprint,
                vault_id,
                _iso(now),
            ),
        )
        self.connection.execute(
            "INSERT INTO jira_collection_runs VALUES (?, ?, 'running', ?, NULL, '{}', NULL)",
            (run_id, collection_id, _iso(now)),
        )
        self.connection.execute(
            "INSERT INTO jira_archive_revisions "
            "(id, collection_id, run_id, revision_number, status, manifest_hash) "
            "VALUES (?, ?, ?, ?, 'pending', 'pending')",
            (revision_id, collection_id, run_id, revision_number),
        )
        self.connection.commit()
        self.connection.autocommit = False
        return collection_id, revision_id

    def _run_collection(
        self,
        collection_id: str,
        revision_id: str,
        selection: PreviewSelection,
        *,
        run_id: str | None = None,
    ) -> dict[str, object]:
        if self.vault_key is None:
            raise ConfigurationError("Jira collection requires a vault key")
        if run_id is None:
            run = self.connection.execute(
                "SELECT run_id FROM jira_archive_revisions WHERE id=?", (revision_id,)
            ).fetchone()
            if run is None:
                raise RecoveryError("Jira collection revision is missing its run")
            run_id = str(run["run_id"])
        issues = [*selection.roots, *selection.context]
        self._persist_collection_issues(collection_id, revision_id, run_id, issues)
        unstable = False
        unavailable = False
        partial = False
        for issue in issues:
            if self._issue_is_complete_in_lineage(revision_id, str(issue["issue_id"])):
                continue
            try:
                first_signature = self._collect_issue(collection_id, revision_id, run_id, issue)
                current_signature = self._issue_signature(str(issue["issue_id"]))
                if current_signature != first_signature:
                    unstable = True
                    self._mark_issue_unstable(revision_id, str(issue["issue_id"]))
            except CollectionPaused:
                self._finish_run(run_id, "paused")
                return self.status(collection_id)
            except AttachmentIntegrityError:
                unstable = True
                self._mark_issue_unstable(revision_id, str(issue["issue_id"]))
            except ScopeExpansionRequired:
                self._finish_run(run_id, "paused", error={"reason": "scope_expansion_required"})
                return self.status(collection_id)
            except PermissionDenied:
                unavailable = True
            except InvalidCredentials:
                self._finish_run(run_id, "failed")
                raise
            except (PermanentSourceError, RetryExhausted, OSError, VaultIntegrityError) as exc:
                self._record_issue_error(revision_id, str(issue["issue_id"]), str(exc))
                unavailable = True
                partial = True
        outcome = (
            "unstable_partial"
            if unstable
            else (
                "partial"
                if partial
                else ("complete_with_unavailable_resources" if unavailable else "complete")
            )
        )
        self._finish_run(run_id, outcome)
        with self.connection:
            self.connection.execute(
                "UPDATE jira_archive_revisions SET status=?, activated_at=? WHERE id=?",
                (
                    "pending" if unstable else "active",
                    None if unstable else _iso(self.clock()),
                    revision_id,
                ),
            )
        return self.status(collection_id)

    def _persist_collection_issues(
        self, collection_id: str, revision_id: str, run_id: str, issues: Iterable[dict[str, object]]
    ) -> None:
        with self.connection:
            for issue in issues:
                issue_id = str(issue["issue_id"])
                object_id = f"jira:{self._site_id}:issue:{issue_id}"
                self.connection.execute(
                    "INSERT OR IGNORE INTO jira_collection_issues "
                    "(collection_id, revision_id, run_id, issue_id, object_id, issue_key, role, "
                    "selection_reason_json, assignment_status, boundary_status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        collection_id,
                        revision_id,
                        run_id,
                        issue_id,
                        object_id,
                        str(issue.get("issue_key", "")),
                        str(issue.get("role", "context")),
                        _json({"reason": issue.get("selection_reason", "one_hop_direct_context")}),
                        str(issue.get("assignment_status", "not_applicable")),
                        str(issue.get("boundary_status", "not_applicable")),
                    ),
                )

    def _lineage_revisions(self, revision_id: str) -> list[str]:
        result: list[str] = []
        current: str | None = revision_id
        while current is not None and current not in result:
            result.append(current)
            row = self.connection.execute(
                "SELECT base_revision_id FROM jira_archive_revisions WHERE id=?", (current,)
            ).fetchone()
            current = str(row[0]) if row is not None and row[0] else None
        return list(reversed(result))

    def _issue_is_complete_in_lineage(self, revision_id: str, issue_id: str) -> bool:
        lineage = self._lineage_revisions(revision_id)
        placeholders = ",".join("?" for _ in lineage)
        rows = self.connection.execute(
            f"SELECT id, logical_resource_id, state FROM jira_resource_states "
            f"WHERE revision_id IN ({placeholders}) AND issue_id=? ORDER BY revision_id",
            (*lineage, issue_id),
        ).fetchall()
        if not rows:
            return False
        latest: dict[str, str] = {}
        for resource in rows:
            logical = str(resource["logical_resource_id"] or resource["id"])
            latest[logical] = str(resource["state"])
        attachment_rows = self.connection.execute(
            f"SELECT logical_resource_id, original_state, id FROM jira_attachment_objects "
            f"WHERE revision_id IN ({placeholders}) AND issue_id=? ORDER BY revision_id",
            (*lineage, issue_id),
        ).fetchall()
        for attachment in attachment_rows:
            logical = str(attachment["logical_resource_id"] or attachment["id"])
            latest[logical] = str(attachment["original_state"])
        return bool(latest) and all(state == "complete" for state in latest.values())

    def _logical_resource_complete(self, revision_id: str, logical_id: str) -> bool:
        lineage = self._lineage_revisions(revision_id)
        placeholders = ",".join("?" for _ in lineage)
        row = self.connection.execute(
            f"SELECT state FROM jira_resource_states WHERE logical_resource_id=? "
            f"AND revision_id IN ({placeholders}) ORDER BY revision_id DESC LIMIT 1",
            (logical_id, *lineage),
        ).fetchone()
        return row is not None and str(row[0]) == "complete"

    def _collect_issue(
        self,
        collection_id: str,
        revision_id: str,
        run_id: str,
        issue: dict[str, object],
        *,
        force: bool = False,
    ) -> str:
        issue_id = str(issue["issue_id"])
        full = self.provider.issue_full(issue_id)
        initial_signature = self._signature_from_issue(full)
        self._check_hydration_scope(revision_id, issue_id, full)
        self._store_json_resource(
            collection_id,
            revision_id,
            run_id,
            issue,
            "issue_fields",
            {"issue_id": issue_id, "api_revision": 3},
            full,
        )
        for kind in (
            "comments",
            "changelog",
            "worklogs",
        ):
            self._collect_paged_resource(collection_id, revision_id, run_id, issue, kind)
        self._collect_issue_properties(collection_id, revision_id, run_id, issue)
        self._collect_remote_links(collection_id, revision_id, run_id, issue)
        self._collect_watchers_votes(collection_id, revision_id, run_id, issue)
        attachments = self._attachments(full)
        self._store_json_resource(
            collection_id,
            revision_id,
            run_id,
            issue,
            "attachments_manifest",
            {"issue_id": issue_id, "manifest": True},
            {"issue_id": issue_id, "attachments": attachments},
        )
        self._store_json_resource(
            collection_id,
            revision_id,
            run_id,
            issue,
            "embedded_media",
            {"issue_id": issue_id, "media": True},
            {"issue_id": issue_id, "media": self._embedded_media_payload(full, attachments)},
        )
        for attachment in attachments:
            self._collect_attachment(collection_id, revision_id, issue, attachment, force=force)
        return initial_signature

    def _check_hydration_scope(
        self, revision_id: str, issue_id: str, full: dict[str, object]
    ) -> None:
        approved = {
            str(row[0])
            for row in self.connection.execute(
                "SELECT issue_id FROM jira_collection_issues WHERE revision_id=?",
                (revision_id,),
            ).fetchall()
        }
        scope_row = self.connection.execute(
            "SELECT c.scope_json FROM jira_collections c "
            "JOIN jira_archive_revisions r ON r.collection_id=c.id WHERE r.id=?",
            (revision_id,),
        ).fetchone()
        unresolved = set()
        if scope_row is not None:
            scope = json.loads(str(scope_row[0]))
            unresolved = {
                str(item["issue_id"])
                for item in scope.get("unresolved_refs", [])
                if isinstance(item, dict) and item.get("issue_id") is not None
            }
        projection = sanitize_metadata(full)
        targets: set[str] = set()
        parent = projection.get("parent")
        if isinstance(parent, dict) and parent.get("issue_id") is not None:
            targets.add(str(parent["issue_id"]))
        for key in ("subtasks", "issue_links"):
            values = projection.get(key)
            if isinstance(values, list):
                for value in values:
                    if isinstance(value, dict) and value.get("issue_id") is not None:
                        targets.add(str(value["issue_id"]))
        unexpected = sorted(targets - approved - unresolved - {issue_id})
        if unexpected:
            raise ScopeExpansionRequired(
                f"Jira issue {issue_id} revealed unapproved targets: {','.join(unexpected)}"
            )

    def _collect_paged_resource(
        self, collection_id: str, revision_id: str, run_id: str, issue: dict[str, object], kind: str
    ) -> None:
        issue_id = str(issue["issue_id"])
        start_at = 0
        seen_starts: set[int] = set()
        while True:
            if start_at in seen_starts:
                raise PermanentSourceError(f"Jira {kind} pagination repeated startAt")
            seen_starts.add(start_at)
            try:
                page = self.provider.resource_page(issue_id, kind, start_at=start_at)
            except PermissionDenied:
                self._store_resource_state(
                    collection_id,
                    revision_id,
                    run_id,
                    issue,
                    kind,
                    {"start_at": start_at},
                    "unavailable",
                    "unavailable",
                    "unavailable",
                    0,
                    {"reason": "permission_denied"},
                )
                return
            if kind == "watchers_votes":
                values: list[object] = [page]
                total = 1
            else:
                list_key = {
                    "comments": "comments",
                    "issue_properties": "keys",
                }.get(kind, "values")
                raw_values = page.get(list_key)
                start_value = page.get("startAt")
                max_value = page.get("maxResults")
                raw_total = page.get("total")
                if (
                    not isinstance(raw_values, list)
                    or not isinstance(start_value, int)
                    or start_value != start_at
                    or not isinstance(max_value, int)
                    or max_value < 1
                    or not isinstance(raw_total, int)
                    or raw_total < 0
                ):
                    raise PermanentSourceError(f"Jira {kind} pagination metadata is invalid")
                values = raw_values
                total = raw_total
            self._store_json_resource(
                collection_id,
                revision_id,
                run_id,
                issue,
                kind,
                {"start_at": start_at},
                page,
            )
            if start_at + len(values) >= total:
                return
            if not values:
                raise PermanentSourceError(f"Jira {kind} pagination made no progress")
            start_at += len(values)

    def _collect_issue_properties(
        self, collection_id: str, revision_id: str, run_id: str, issue: dict[str, object]
    ) -> None:
        issue_id = str(issue["issue_id"])
        start_at = 0
        seen_starts: set[int] = set()
        keys_seen: set[str] = set()
        while True:
            if start_at in seen_starts:
                raise PermanentSourceError("Jira property pagination repeated startAt")
            seen_starts.add(start_at)
            page = self.provider.resource_page(issue_id, "issue_properties", start_at=start_at)
            keys = page.get("keys")
            start_value = page.get("startAt")
            max_value = page.get("maxResults")
            total = page.get("total")
            if (
                not isinstance(keys, list)
                or not isinstance(start_value, int)
                or start_value != start_at
                or not isinstance(max_value, int)
                or max_value < 1
                or not isinstance(total, int)
                or total < 0
            ):
                raise PermanentSourceError("Jira property pagination metadata is invalid")
            for raw_key in keys:
                if (
                    not isinstance(raw_key, str)
                    or not 1 <= len(raw_key) <= 256
                    or any(ord(char) < 32 for char in raw_key)
                    or "/" in raw_key
                    or ".." in raw_key
                    or raw_key in keys_seen
                ):
                    raise PermanentSourceError("Jira property key listing is invalid")
                keys_seen.add(raw_key)
                locator = {"issue_id": issue_id, "property_key": raw_key}
                if self._logical_resource_complete(
                    revision_id,
                    self._logical_resource_id(collection_id, issue_id, "issue_property", locator),
                ):
                    continue
                try:
                    document = self.provider.issue_property(issue_id, raw_key)
                except (PermissionDenied, SourceObjectUnavailable) as exc:
                    self._store_resource_state(
                        collection_id,
                        revision_id,
                        run_id,
                        issue,
                        "issue_property",
                        locator,
                        "unavailable",
                        "unavailable",
                        "unavailable",
                        0,
                        {"reason": type(exc).__name__},
                    )
                    continue
                self._store_json_resource(
                    collection_id,
                    revision_id,
                    run_id,
                    issue,
                    "issue_property",
                    locator,
                    document,
                )
            if start_at + len(keys) >= total:
                return
            if not keys:
                raise PermanentSourceError("Jira property pagination made no progress")
            start_at += len(keys)

    def _collect_remote_links(
        self, collection_id: str, revision_id: str, run_id: str, issue: dict[str, object]
    ) -> None:
        issue_id = str(issue["issue_id"])
        locator = {"issue_id": issue_id, "remote_links": True}
        try:
            links = self.provider.remote_links(issue_id)
        except (PermissionDenied, SourceObjectUnavailable) as exc:
            self._store_resource_state(
                collection_id,
                revision_id,
                run_id,
                issue,
                "remote_links",
                locator,
                "unavailable",
                "unavailable",
                "unavailable",
                0,
                {"reason": type(exc).__name__},
            )
            return
        self._store_json_resource(
            collection_id,
            revision_id,
            run_id,
            issue,
            "remote_links",
            locator,
            {"issue_id": issue_id, "links": links},
        )

    def _collect_watchers_votes(
        self, collection_id: str, revision_id: str, run_id: str, issue: dict[str, object]
    ) -> None:
        issue_id = str(issue["issue_id"])
        for kind, fetch in (("watchers", self.provider.watchers), ("votes", self.provider.votes)):
            locator = {"issue_id": issue_id, "endpoint": kind}
            if self._logical_resource_complete(
                revision_id, self._logical_resource_id(collection_id, issue_id, kind, locator)
            ):
                continue
            try:
                document = fetch(issue_id)
            except (PermissionDenied, SourceObjectUnavailable) as exc:
                self._store_resource_state(
                    collection_id,
                    revision_id,
                    run_id,
                    issue,
                    kind,
                    locator,
                    "unavailable",
                    "unavailable",
                    "unavailable",
                    0,
                    {"reason": type(exc).__name__},
                )
                continue
            self._store_json_resource(
                collection_id,
                revision_id,
                run_id,
                issue,
                kind,
                locator,
                document,
            )

    def _collect_attachment(
        self,
        collection_id: str,
        revision_id: str,
        issue: dict[str, object],
        attachment: dict[str, object],
        *,
        force: bool = False,
    ) -> None:
        attachment_id = str(attachment["attachment_id"])
        logical_object_id = f"jatt-family:{collection_id}:{attachment_id}"
        object_id = f"jatt:{collection_id}:{revision_id}:{attachment_id}"
        existing = self.connection.execute(
            "SELECT original_state FROM jira_attachment_objects WHERE id=?",
            (object_id,),
        ).fetchone()
        if not force and existing is not None and str(existing["original_state"]) == "complete":
            return
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO jira_attachment_objects "
                "(id, collection_id, revision_id, issue_id, attachment_id, archive_evidence_id, "
                "filename, mime_type, declared_size, manifest_sha256, original_state, "
                "extracted_state, logical_resource_id, source_locator) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'planned', 'not_requested', ?, ?)",
                (
                    object_id,
                    collection_id,
                    revision_id,
                    str(issue["issue_id"]),
                    attachment_id,
                    f"jare:{self._site_id}:{collection_id}:{revision_id}:attachment:{attachment_id}",
                    f"attachment-{attachment_id}",
                    str(attachment.get("mime_type") or ""),
                    attachment.get("declared_size"),
                    _hash(attachment),
                    logical_object_id,
                    _json({"attachment_id": attachment_id}),
                ),
            )
        try:
            self._check_free_space()
            with self.provider.attachment_content(attachment_id) as first_response:
                first_validator = self._attachment_validator(first_response)
                first_length, first_hash = self._hash_attachment_pass(first_response)
            declared_size = attachment.get("declared_size")
            if isinstance(declared_size, int) and first_length != declared_size:
                raise AttachmentIntegrityError("Jira attachment declared size changed")
            self._check_free_space()
            with self.provider.attachment_content(attachment_id) as second_response:
                if first_validator != self._attachment_validator(second_response):
                    raise AttachmentIntegrityError(
                        "Jira attachment validator changed between passes"
                    )
                reader = _ResponseReader(second_response, collector=self)
                destination = _object_path(
                    self.vault_root, self._site_id, collection_id, revision_id, object_id
                )
                descriptor = VaultDescriptor(
                    site_id=self._site_id,
                    collection_id=collection_id,
                    revision_id=revision_id,
                    object_id=object_id,
                    kind="attachment_original",
                    content_length=first_length,
                    content_sha256=first_hash,
                    key_version=self.key_version,
                )
                result = write_vault_object(
                    cast(BinaryIO, reader),
                    destination,
                    descriptor,
                    self.vault_key or b"",
                    vault_root=self.vault_root,
                )
                with self.connection:
                    self.connection.execute(
                        "UPDATE jira_attachment_objects SET original_state='complete', "
                        "vault_object_id=?, "
                        "vault_object_path=?, vault_key_version=?, ciphertext_sha256=? WHERE id=?",
                        (
                            object_id,
                            str(destination.relative_to(self.vault_root)),
                            self.key_version,
                            result.ciphertext_sha256,
                            object_id,
                        ),
                    )
                if result.plaintext_length != first_length:
                    raise AttachmentIntegrityError("Jira attachment length changed between passes")
        except CollectionPaused:
            raise
        except AttachmentIntegrityError:
            self._mark_attachment_unavailable(
                collection_id, revision_id, object_id, "integrity_changed"
            )
            raise
        except PermissionDenied:
            self._mark_attachment_unavailable(
                collection_id, revision_id, object_id, "permission_denied"
            )
        except (PermanentSourceError, RetryExhausted, OSError, VaultIntegrityError) as exc:
            self._mark_attachment_unavailable(collection_id, revision_id, object_id, str(exc))

    @staticmethod
    def _attachment_validator(response: Any) -> tuple[str | None, str | None, str | None]:
        return (
            response.headers.get("ETag"),
            response.headers.get("Last-Modified"),
            response.headers.get("Content-Length"),
        )

    def _hash_attachment_pass(self, response: Any) -> tuple[int, str]:
        digest = hashlib.sha256()
        length = 0
        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
            if not isinstance(chunk, bytes):
                raise AttachmentIntegrityError("Jira attachment stream returned non-bytes")
            self._account_transfer(len(chunk))
            length += len(chunk)
            digest.update(chunk)
        return length, digest.hexdigest()

    def _store_json_resource(
        self,
        collection_id: str,
        revision_id: str,
        run_id: str,
        issue: dict[str, object],
        kind: str,
        locator: Mapping[str, object],
        payload: dict[str, object],
    ) -> None:
        resource_id = self._resource_id(
            collection_id, revision_id, str(issue["issue_id"]), kind, locator
        )
        self._store_resource_state(
            collection_id,
            revision_id,
            run_id,
            issue,
            kind,
            locator,
            "complete",
            "complete",
            "available",
            1,
            None,
        )
        object_id = f"jres:{resource_id}:attempt:{uuid.uuid4()}"
        raw = canonical_json(payload)
        destination = _object_path(
            self.vault_root, self._site_id, collection_id, revision_id, object_id
        )
        descriptor = VaultDescriptor(
            site_id=self._site_id,
            collection_id=collection_id,
            revision_id=revision_id,
            object_id=object_id,
            kind=kind,
            content_length=len(raw),
            content_sha256=hashlib.sha256(raw).hexdigest(),
            key_version=self.key_version,
        )
        self._check_free_space()
        result = write_vault_object(
            BytesIO(raw), destination, descriptor, self.vault_key or b"", vault_root=self.vault_root
        )
        with self.connection:
            self.connection.execute(
                "UPDATE jira_resource_states SET raw_vault_object_id=?, raw_vault_object_path=?, "
                "raw_vault_ciphertext_sha256=?, raw_vault_key_version=? WHERE id=?",
                (
                    object_id,
                    str(destination.relative_to(self.vault_root)),
                    result.ciphertext_sha256,
                    self.key_version,
                    resource_id,
                ),
            )

    def _store_resource_state(
        self,
        collection_id: str,
        revision_id: str,
        run_id: str,
        issue: dict[str, object],
        kind: str,
        locator: Mapping[str, object],
        state: str,
        completeness: str,
        availability: str,
        seen_count: int,
        error: Mapping[str, object] | None,
    ) -> None:
        logical_resource_id = self._logical_resource_id(
            collection_id, str(issue["issue_id"]), kind, locator
        )
        resource_id = self._resource_id(
            collection_id, revision_id, str(issue["issue_id"]), kind, locator
        )
        role = "root" if issue.get("role") in {"root", "explicit_root"} else "context"
        repository = JiraArchiveRepository(self.connection)
        with suppress(sqlite3.IntegrityError):
            repository.create_resource_checkpoint(
                resource_id=resource_id,
                collection_id=collection_id,
                revision_id=revision_id,
                run_id=run_id,
                issue_id=str(issue["issue_id"]),
                kind=kind,
                locator=locator,
                role=role,
                redaction_version="1",
                logical_resource_id=logical_resource_id,
                state=state,
            )
        repository.checkpoint_resource(
            resource_id=resource_id,
            state=state,
            completeness=completeness,
            availability=availability,
            seen_count=seen_count,
            page_cursor=None,
            attempt=1,
            logical_resource_id=logical_resource_id,
            fetched_at=_iso(self.clock()),
            error=error,
        )

    @staticmethod
    def _resource_id(
        collection_id: str,
        revision_id: str,
        issue_id: str,
        kind: str,
        locator: Mapping[str, object],
    ) -> str:
        digest = hashlib.sha256(canonical_json(dict(locator))).hexdigest()[:24]
        return f"jres:{collection_id}:{revision_id}:{issue_id}:{kind}:{digest}"

    @staticmethod
    def _logical_resource_id(
        collection_id: str, issue_id: str, kind: str, locator: Mapping[str, object]
    ) -> str:
        digest = hashlib.sha256(canonical_json(dict(locator))).hexdigest()[:24]
        return f"jres-family:{collection_id}:{issue_id}:{kind}:{digest}"

    @staticmethod
    def _attachments(issue: Mapping[str, object]) -> list[dict[str, object]]:
        fields = issue.get("fields") if isinstance(issue, dict) else None
        attachments = fields.get("attachment") if isinstance(fields, dict) else None
        result: list[dict[str, object]] = []
        if not isinstance(attachments, list):
            return result
        for item in attachments:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                continue
            result.append(
                {
                    "attachment_id": item["id"],
                    "declared_size": item.get("size")
                    if isinstance(item.get("size"), int)
                    else None,
                    "mime_type": item.get("mimeType")
                    if isinstance(item.get("mimeType"), str)
                    else None,
                }
            )
        return result

    @staticmethod
    def _embedded_media_payload(
        issue: Mapping[str, object], attachments: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        attachment_ids = {str(item["attachment_id"]) for item in attachments}
        found: list[dict[str, object]] = []
        stack: list[tuple[object, str, int]] = [(issue.get("fields", {}), "fields", 0)]
        while stack and len(found) < 1000:
            value, locator, depth = stack.pop()
            if depth > 32:
                continue
            if isinstance(value, dict):
                attrs: dict[str, object] = (
                    cast(dict[str, object], value["attrs"])
                    if isinstance(value.get("attrs"), dict)
                    else {}
                )
                media_id = value.get("id") or attrs.get("id") or value.get("mediaId")
                if value.get("type") == "media" or (media_id is not None and "media" in value):
                    identifier = str(media_id) if media_id is not None else None
                    found.append(
                        {
                            "media_id": identifier,
                            "locator": locator,
                            "disposition": (
                                "resolved" if identifier in attachment_ids else "unavailable"
                            ),
                        }
                    )
                for key, child in value.items():
                    if key in {"url", "contentUrl", "href"} and isinstance(child, str):
                        found.append(
                            {
                                "media_id": None,
                                "locator": f"{locator}.{key}",
                                "disposition": "external_unavailable",
                            }
                        )
                    elif key not in {"url", "contentUrl", "href"}:
                        stack.append((child, f"{locator}.{key}", depth + 1))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    stack.append((child, f"{locator}[{index}]", depth + 1))
        return found[:1000]

    def _issue_signature(self, issue_id: str) -> str:
        return self._signature_from_issue(self.provider.issue_full(issue_id))

    @staticmethod
    def _signature_from_issue(issue: Mapping[str, object]) -> str:
        fields = issue.get("fields")
        fields_map = fields if isinstance(fields, dict) else {}
        value = {
            "updated": fields_map.get("updated"),
            "attachment_manifest": [
                {
                    "id": item.get("id"),
                    "size": item.get("size"),
                    "mimeType": item.get("mimeType"),
                }
                for item in fields_map.get("attachment", [])
                if isinstance(item, dict)
            ],
        }
        return _hash(value)

    def _check_free_space(self) -> None:
        self.vault_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if shutil.disk_usage(self.vault_root).free < self.limits.free_space_floor_bytes:
            raise CollectionPaused("Jira vault free-space floor reached")

    def _account_transfer(self, amount: int) -> None:
        self._transferred += amount
        if self._transferred > self.limits.transfer_budget_bytes:
            raise CollectionPaused("Jira collection transfer budget exceeded")

    def _mark_attachment_unavailable(
        self, collection_id: str, revision_id: str, object_id: str, reason: str
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jira_attachment_objects SET original_state='unavailable', "
                "source_locator=? WHERE id=? "
                "AND collection_id=? AND revision_id=?",
                (reason[:500], object_id, collection_id, revision_id),
            )

    def _record_issue_error(self, revision_id: str, issue_id: str, error: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jira_resource_states SET state='unavailable', completeness='partial', "
                "availability='unavailable', error_json=? WHERE revision_id=? AND issue_id=? "
                "AND state NOT IN ('complete', 'unavailable')",
                (_json({"error": error[:500]}), revision_id, issue_id),
            )

    def _mark_issue_unstable(self, revision_id: str, issue_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jira_resource_states SET state='unstable', completeness='partial' "
                "WHERE revision_id=? AND issue_id=? AND state='complete'",
                (revision_id, issue_id),
            )

    def _finish_run(
        self, run_id: str, status: str, *, error: Mapping[str, object] | None = None
    ) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE jira_collection_runs SET status=?, completed_at=?, error_json=? WHERE id=?",
                (status, _iso(self.clock()), _json(error) if error else None, run_id),
            )

    def _latest_collection(self, collection_id: str | None) -> sqlite3.Row | None:
        query = (
            "SELECT c.id AS collection_id, c.scope_json, c.scope_hash, c.config_fingerprint, "
            "c.policy_version, "
            "c.site_id, r.id AS revision_id, r.run_id, run.status AS run_status, run.error_json "
            "FROM jira_collections c JOIN jira_archive_revisions r ON r.collection_id=c.id "
            "JOIN jira_collection_runs run ON run.id=r.run_id "
            "WHERE (? IS NULL OR c.id=?) ORDER BY r.revision_number DESC LIMIT 1"
        )
        return cast(
            sqlite3.Row | None,
            self.connection.execute(query, (collection_id, collection_id)).fetchone(),
        )

    def _new_revision(self, collection_id: str, selection: PreviewSelection) -> tuple[str, str]:
        base_row = self.connection.execute(
            "SELECT id FROM jira_archive_revisions WHERE collection_id=? "
            "ORDER BY revision_number DESC LIMIT 1",
            (collection_id,),
        ).fetchone()
        with self.connection:
            repository = JiraArchiveRepository(self.connection)
            run_id = repository.start_run(collection_id)
            revision_id = repository.create_revision(
                collection_id,
                run_id,
                "pending",
                base_revision_id=str(base_row[0]) if base_row is not None else None,
            )
        return run_id, revision_id
