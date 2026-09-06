from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from worktrace.candidates.decisions import CREATION_ACTIONS
from worktrace.candidates.projector import project_candidate
from worktrace.config import WorkTraceConfig
from worktrace.constants import MAX_EXCERPT_CHARS
from worktrace.db.connection import connect_read_only
from worktrace.db.readiness import DatabaseReadinessStatus, database_readiness
from worktrace.errors import DatabaseError, NotFound, ScopeViolation
from worktrace.packets.builder import PacketBuilder
from worktrace.packets.gaps import build_gap_report
from worktrace.read_models.candidates import CandidateCursor, CandidatePage, candidate_page
from worktrace.read_models.evidence_search import (
    EvidenceSearchCursor,
    EvidenceSearchFilters,
    EvidenceSearchPage,
    evidence_search_page,
    normalize_evidence_search_filters,
)

TUI_BUSY_TIMEOUT_MS = 500


class DatabaseBusy(DatabaseError):
    """The ledger could not be read within the TUI contention budget."""


class DatabaseUpgradeRequired(DatabaseError):
    """The ledger is older than the packaged read model."""


class DatabaseVersionUnsupported(DatabaseError):
    """The ledger is newer than the packaged read model."""


@dataclass(frozen=True, slots=True)
class ApplicationSummary:
    app_id: str
    name: str
    market: str
    business_type: str
    sources: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UnsupportedMember:
    object_id: str
    source: str
    kind: str
    external_id: str


@dataclass(frozen=True, slots=True)
class ContributionReview:
    app_id: str
    candidate_id: str
    resolved_contribution_id: str
    status: str
    packet: dict[str, object]
    gaps: dict[str, object]
    unsupported_members: tuple[UnsupportedMember, ...]


class ReadOnlyWorkspace:
    """Narrow, connection-per-call composition root for the human TUI."""

    def __init__(
        self,
        config: WorkTraceConfig,
        *,
        busy_timeout_ms: int = TUI_BUSY_TIMEOUT_MS,
    ) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must be non-negative")
        self._config = config
        self._database_path = config.database_path
        self._busy_timeout_ms = busy_timeout_ms

    @property
    def database_path(self) -> Path:
        return self._database_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection: sqlite3.Connection | None = None
        try:
            connection = connect_read_only(
                self._database_path,
                busy_timeout_ms=self._busy_timeout_ms,
            )
            query_only = connection.execute("PRAGMA query_only").fetchone()
            if query_only is None or int(query_only[0]) != 1:
                raise DatabaseError("read-only ledger connection did not enter query-only mode")
            readiness = database_readiness(connection)
            if readiness.status is DatabaseReadinessStatus.UPGRADE_REQUIRED:
                raise DatabaseUpgradeRequired(
                    "WorkTrace database is older than this version; run `worktrace init` "
                    "from the CLI, then retry `worktrace ui`."
                )
            if readiness.status is DatabaseReadinessStatus.UNSUPPORTED_NEWER:
                raise DatabaseVersionUnsupported(
                    "WorkTrace database is newer than this installed version; upgrade "
                    "WorkTrace before opening the UI."
                )
            yield connection
        except sqlite3.OperationalError as exc:
            message = str(exc).casefold()
            if "locked" in message or "busy" in message:
                raise DatabaseBusy(
                    "WorkTrace data is busy. Another process may be importing or rebuilding; "
                    "retry after it finishes."
                ) from exc
            raise DatabaseError(
                "WorkTrace could not read the configured database. Run `worktrace doctor` "
                "from the CLI and retry."
            ) from exc
        finally:
            if connection is not None:
                connection.close()

    @contextmanager
    def _read_snapshot(self) -> Iterator[sqlite3.Connection]:
        from worktrace.db.read_state import read_snapshot

        with self._connection() as connection, read_snapshot(connection):
            yield connection

    def applications(self) -> tuple[ApplicationSummary, ...]:
        with self._connection():
            return tuple(
                ApplicationSummary(
                    app_id=app.id,
                    name=app.name,
                    market=app.market,
                    business_type=app.business_type,
                    sources=tuple(
                        source
                        for source, enabled in (
                            ("git", bool(app.repo_paths)),
                            ("jira", bool(app.jira_project_keys)),
                            ("gitlab", bool(app.gitlab_project_ids)),
                        )
                        if enabled
                    ),
                )
                for app in self._config.apps
            )

    def source_status(self, app_id: str) -> dict[str, object]:
        self._config.app(app_id)
        with self._connection() as connection:
            return PacketBuilder(connection, self._config).source_status(app_id)

    def candidate_page(
        self,
        app_id: str,
        *,
        page_size: int = 25,
        cursor: CandidateCursor | None = None,
    ) -> CandidatePage:
        self._config.app(app_id)
        with self._connection() as connection:
            return candidate_page(
                connection,
                self._config,
                app_id,
                page_size=page_size,
                cursor=cursor,
            )

    def search_evidence(
        self,
        app_id: str,
        filters: EvidenceSearchFilters,
        *,
        cursor: EvidenceSearchCursor | None = None,
        expected_revision: int | None = None,
    ) -> EvidenceSearchPage:
        """Search current evidence with a TUI-only, revision-bound continuation."""

        self._config.app(app_id)
        normalized = normalize_evidence_search_filters(
            filters.query,
            source=filters.source,
            module_text=filters.module_text,
            date_from=filters.date_from,
            date_to=filters.date_to,
        )
        with self._read_snapshot() as connection:
            return evidence_search_page(
                connection,
                PacketBuilder(connection, self._config),
                app_id,
                normalized,
                cursor=cursor,
                expected_revision=expected_revision,
            )

    def contribution_review(self, app_id: str, candidate_id: str) -> ContributionReview:
        self._config.app(app_id)
        with self._read_snapshot() as connection:
            builder = PacketBuilder(connection, self._config)
            canonical = builder.resolve_contribution(candidate_id)
            if canonical.app_id != app_id:
                raise ScopeViolation("candidate belongs to another application")
            resolved_candidate_id = canonical.candidate_id or candidate_id
            try:
                projected = project_candidate(connection, resolved_candidate_id)
            except NotFound:
                projected = None
            if projected is not None and projected.app_id != app_id:
                raise ScopeViolation("candidate belongs to another application")
            packet = builder.build_packet(candidate_id)
            contribution = packet.get("contribution")
            if not isinstance(contribution, dict) or contribution.get("app_id") != app_id:
                raise ScopeViolation("candidate belongs to another application")
            contribution_id = contribution.get("id")
            if not isinstance(contribution_id, str) or not contribution_id:
                raise DatabaseError("contribution packet is missing its stable ID")
            evidence_summary = packet.get("evidence_summary")
            raw_unsupported = (
                evidence_summary.get("unsupported_member_ids", [])
                if isinstance(evidence_summary, dict)
                else []
            )
            unsupported_ids = tuple(
                value for value in raw_unsupported if isinstance(value, str) and value
            )
            unsupported: tuple[UnsupportedMember, ...] = ()
            if unsupported_ids:
                placeholders = ",".join("?" for _ in unsupported_ids)
                rows = connection.execute(
                    f"""
                    SELECT id, source, kind, external_id
                    FROM source_objects
                    WHERE app_id=? AND id IN ({placeholders})
                    ORDER BY id
                    """,
                    (app_id, *unsupported_ids),
                )
                unsupported = tuple(
                    UnsupportedMember(
                        object_id=str(row["id"]),
                        source=str(row["source"]),
                        kind=str(row["kind"]),
                        external_id=str(row["external_id"]),
                    )
                    for row in rows
                )
            lineage = builder._decision_projection
            if lineage is None:
                lineage = builder.page_projection_builder(app_id)._decision_projection
            canonical_lineage = (
                lineage.resolve_lineage(candidate_id, app_id=app_id)
                if lineage is not None
                else None
            )
            confirmed = bool(
                canonical_lineage
                and any(
                    decision.action in CREATION_ACTIONS for decision in canonical_lineage.decisions
                )
            )
        return ContributionReview(
            app_id=app_id,
            candidate_id=resolved_candidate_id,
            resolved_contribution_id=contribution_id,
            status=(
                projected.status
                if projected is not None
                else "confirmed"
                if confirmed
                else "suggestion"
            ),
            packet=packet,
            gaps=build_gap_report(packet),
            unsupported_members=unsupported,
        )

    def evidence_excerpt(
        self,
        app_id: str,
        evidence_id: str,
        *,
        max_chars: int,
    ) -> dict[str, object]:
        if not 1 <= max_chars <= MAX_EXCERPT_CHARS:
            raise ValueError(f"max_chars must be between 1 and {MAX_EXCERPT_CHARS}")
        self._config.app(app_id)
        with self._connection() as connection:
            excerpt = PacketBuilder(connection, self._config).evidence_excerpt(
                evidence_id,
                max_chars,
            )
            if excerpt.get("app_id") != app_id:
                raise ScopeViolation("evidence belongs to another application")
            return excerpt
