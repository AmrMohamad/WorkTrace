"""Lazy, scan-bounded canonical rows for agent-facing paging.

This module deliberately stops before cursor/token serialization and response
budget admission.  Consumers receive every scanned position, including rows
excluded by canonical projection or an activity-date filter, so they can
advance only over an excluded or delivered row.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from worktrace.constants import DEFAULT_EXCERPT_CHARS
from worktrace.errors import NotFound
from worktrace.packets.builder import (
    PacketBuilder,
    _activity_metadata_json,
    _calendar_date,
    _evidence_record_from_row,
)
from worktrace.read_models.candidates import (
    MAX_SCAN_BUDGET,
    _active_generation,
    _has_raw_candidate_after,
    _raw_candidate_batch,
    _validate_row_generation,
)

ScannedRow = tuple[dict[str, str], dict[str, object] | None, bool]


@dataclass(slots=True)
class PagingDiagnostics:
    """Per-scan diagnostics; these are measurements, not cost guarantees."""

    raw_scans: int = 0
    projections: int = 0
    authority_rows: int = 0
    decision_rows: int = 0
    hydrated_body_bytes: int = 0
    started_at: float = 0.0

    @property
    def runtime_seconds(self) -> float:
        return time.perf_counter() - self.started_at


class _LazyScannedRows(Iterator[ScannedRow]):
    def __init__(
        self,
        rows: Sequence[sqlite3.Row],
        *,
        has_more_after_last: bool,
        position: Callable[[sqlite3.Row], dict[str, str]],
        project: Callable[[sqlite3.Row], dict[str, object] | None],
        diagnostics: PagingDiagnostics,
    ) -> None:
        self._rows = rows
        self._has_more_after_last = has_more_after_last
        self._position = position
        self._project = project
        self._diagnostics = diagnostics
        self._index = 0

    @property
    def diagnostics(self) -> PagingDiagnostics:
        return self._diagnostics

    def __next__(self) -> ScannedRow:
        if self._index >= len(self._rows):
            raise StopIteration
        row = self._rows[self._index]
        self._index += 1
        self._diagnostics.raw_scans += 1
        projected = self._project(row)
        self._diagnostics.projections += 1
        return (
            self._position(row),
            projected,
            self._index < len(self._rows) or self._has_more_after_last,
        )


def _matches_date_filter(
    item: dict[str, object],
    *,
    zone: str,
    date_from: str | None,
    date_to: str | None,
) -> bool:
    if not (date_from or date_to):
        return True
    raw_period_from = item.get("period_from")
    raw_period_to = item.get("period_to")
    period_from = _calendar_date(
        raw_period_from if isinstance(raw_period_from, str) else None,
        zone,
    )
    period_to = _calendar_date(
        raw_period_to if isinstance(raw_period_to, str) else None,
        zone,
    )
    if not (period_from and period_to):
        return False
    return not (
        (date_from is not None and period_to < date_from)
        or (date_to is not None and period_from > date_to)
    )


def _page_builder(
    builder: PacketBuilder,
    app_id: str,
    diagnostics: PagingDiagnostics,
) -> PacketBuilder:
    page_builder = builder.page_projection_builder(app_id)
    context = page_builder._authority_context
    decision_context = page_builder._decision_projection
    diagnostics.authority_rows = len(context.current_observations) if context is not None else 0
    diagnostics.decision_rows = (
        len(decision_context.active_decisions) if decision_context is not None else 0
    )
    return page_builder


def scan_candidates(
    builder: PacketBuilder,
    app_id: str,
    *,
    after: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[str | None, Iterator[ScannedRow]]:
    """Yield at most 200 generation-bound candidate rows in stable ID order."""

    builder.config.app(app_id)
    diagnostics = PagingDiagnostics(started_at=time.perf_counter())
    generation = _active_generation(builder.connection, app_id)
    if generation is None:
        return (
            None,
            _LazyScannedRows(
                (),
                has_more_after_last=False,
                position=lambda _: {},
                project=lambda _: None,
                diagnostics=diagnostics,
            ),
        )

    rows = _raw_candidate_batch(
        builder.connection,
        app_id=app_id,
        generation=generation,
        after_candidate_id=after or "",
        limit=MAX_SCAN_BUDGET,
    )
    has_more = (
        bool(rows)
        and len(rows) == MAX_SCAN_BUDGET
        and _has_raw_candidate_after(
            builder.connection,
            app_id=app_id,
            generation=generation,
            after_candidate_id=str(rows[-1]["id"]),
        )
    )
    page_builder: PacketBuilder | None = None

    def project(row: sqlite3.Row) -> dict[str, object] | None:
        nonlocal page_builder
        _validate_row_generation(row, generation)
        if page_builder is None:
            page_builder = _page_builder(builder, app_id, diagnostics)
        try:
            item = page_builder.candidate_list_item(app_id, str(row["id"]))
        except NotFound:
            return None
        return (
            item
            if _matches_date_filter(
                item,
                zone=builder.config.employment_timezone,
                date_from=date_from,
                date_to=date_to,
            )
            else None
        )

    return (
        generation.token,
        _LazyScannedRows(
            rows,
            has_more_after_last=has_more,
            position=lambda row: {"candidate_id": str(row["id"])},
            project=project,
            diagnostics=diagnostics,
        ),
    )


def _search_clauses(
    query: str,
    app_id: str,
    *,
    source_types: Sequence[str],
    actor_id: str | None,
    module: str | None,
    after: tuple[str, str] | None,
) -> tuple[list[str], list[object]]:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    clauses = [
        "latest.position=1",
        "so.app_id=?",
        "(lower(COALESCE(latest.title,'')) LIKE lower(?) ESCAPE '\\' "
        "OR lower(COALESCE(latest.body_text,'')) LIKE lower(?) ESCAPE '\\' "
        "OR lower(latest.data_json) LIKE lower(?) ESCAPE '\\')",
    ]
    parameters: list[object] = [app_id, f"%{escaped}%", f"%{escaped}%", f"%{escaped}%"]
    if source_types:
        clauses.append(f"so.source IN ({','.join('?' for _ in source_types)})")
        parameters.extend(source_types)
    if actor_id:
        clauses.append(
            "EXISTS (SELECT 1 FROM authoritative_current_participations p "
            "WHERE p.observation_id=latest.id AND p.actor_id=?)"
        )
        parameters.append(actor_id)
    if module:
        escaped_module = module.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("lower(latest.data_json) LIKE lower(?) ESCAPE '\\'")
        parameters.append(f"%{escaped_module}%")
    if after is not None:
        clauses.append(
            "(COALESCE(latest.source_updated_at, latest.fetched_at) < ? OR "
            "(COALESCE(latest.source_updated_at, latest.fetched_at) = ? AND latest.id > ?))"
        )
        parameters.extend((after[0], after[0], after[1]))
    return clauses, parameters


def _search_rows(
    builder: PacketBuilder,
    query: str,
    app_id: str,
    *,
    source_types: Sequence[str],
    actor_id: str | None,
    module: str | None,
    after: tuple[str, str] | None,
    limit: int,
) -> list[sqlite3.Row]:
    from worktrace.db.authority import authoritative_current_participation_ctes

    clauses, parameters = _search_clauses(
        query,
        app_id,
        source_types=source_types,
        actor_id=actor_id,
        module=module,
        after=after,
    )
    return list(
        builder.connection.execute(
            f"""
            WITH {authoritative_current_participation_ctes()}
            SELECT latest.id, latest.source_object_id, latest.source_updated_at,
                   latest.fetched_at, substr(latest.title, 1, 4000) AS title,
                   NULL AS body_text, {_activity_metadata_json("latest")} AS data_json,
                   latest.completeness, so.app_id, so.source, so.source_instance,
                   so.kind, so.external_id, so.availability, so.availability_reason,
                   so.availability_observed_at, NULL AS availability_evidence_id,
                   COALESCE(latest.source_updated_at, latest.fetched_at) AS sort_time,
                   substr(COALESCE(latest.body_text, latest.title, ''), 1, ?) AS page_text
            FROM authoritative_current_observations latest
            JOIN source_objects so ON so.id=latest.source_object_id
            WHERE {" AND ".join(clauses)}
            ORDER BY COALESCE(latest.source_updated_at, latest.fetched_at) DESC, latest.id
            LIMIT ?
            """,
            [DEFAULT_EXCERPT_CHARS, *parameters, limit],
        )
    )


def scan_evidence(
    builder: PacketBuilder,
    query: str,
    app_id: str,
    *,
    source_types: Sequence[str],
    actor_id: str | None,
    module: str | None,
    date_from: str | None,
    date_to: str | None,
    after: tuple[str, str] | None = None,
    page_builder: PacketBuilder | None = None,
) -> Iterator[ScannedRow]:
    """Yield at most 200 evidence-search rows in freshness/id keyset order."""

    builder.config.app(app_id)
    diagnostics = PagingDiagnostics(started_at=time.perf_counter())
    rows = _search_rows(
        builder,
        query,
        app_id,
        source_types=source_types,
        actor_id=actor_id,
        module=module,
        after=after,
        limit=MAX_SCAN_BUDGET,
    )
    has_more = False
    if rows and len(rows) == MAX_SCAN_BUDGET:
        last = rows[-1]
        has_more = bool(
            _search_rows(
                builder,
                query,
                app_id,
                source_types=source_types,
                actor_id=actor_id,
                module=module,
                after=(str(last["sort_time"]), str(last["id"])),
                limit=1,
            )
        )
    projection_builder = page_builder

    def project(row: sqlite3.Row) -> dict[str, object] | None:
        nonlocal projection_builder
        if projection_builder is None:
            projection_builder = _page_builder(builder, app_id, diagnostics)
        diagnostics.hydrated_body_bytes += len(str(row["page_text"]).encode("utf-8"))
        record = _evidence_record_from_row(row, context_only=False)
        period = projection_builder._activity_period([record])
        if not period.matches(date_from, date_to, builder.config.employment_timezone):
            return None
        return {
            "evidence_id": str(row["id"]),
            **period.fields(),
            "object_id": str(row["source_object_id"]),
            "source": str(row["source"]),
            "source_instance": str(row["source_instance"]),
            "kind": str(row["kind"]),
            "external_id": str(row["external_id"]),
            "title": row["title"],
            "content_type": "untrusted_source_excerpt",
            "source_text_is_untrusted": True,
            "text": str(row["page_text"]),
            "completeness": str(row["completeness"]),
            "observed_at": str(row["fetched_at"]),
        }

    return _LazyScannedRows(
        rows,
        has_more_after_last=has_more,
        position=lambda row: {
            "sort_time": str(row["sort_time"]),
            "observation_id": str(row["id"]),
        },
        project=project,
        diagnostics=diagnostics,
    )
