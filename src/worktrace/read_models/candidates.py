from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from worktrace.candidates.decisions import _build_decision_projection_context
from worktrace.config import WorkTraceConfig
from worktrace.errors import DatabaseError, NotFound, WorkTraceError

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 50
MAX_SCAN_BUDGET = 200

_ACTIVE_GENERATION_SQL = """
    SELECT generated_at, generator_version
    FROM candidate_groups INDEXED BY idx_candidates_app_time
    WHERE app_id=?
    ORDER BY generated_at DESC
    LIMIT 1
"""
_OLDER_GENERATION_SQL = """
    SELECT 1
    FROM candidate_groups INDEXED BY idx_candidates_app_time
    WHERE app_id=? AND generated_at < ?
    LIMIT 1
"""
_NEWER_GENERATION_SQL = """
    SELECT 1
    FROM candidate_groups INDEXED BY idx_candidates_app_time
    WHERE app_id=? AND generated_at > ?
    LIMIT 1
"""
_RAW_CANDIDATE_SQL = """
    SELECT id, generator_version
    FROM candidate_groups INDEXED BY sqlite_autoindex_candidate_groups_1
    WHERE id > ? AND app_id=? AND generated_at=?
    ORDER BY id
    LIMIT ?
"""


class CandidateGenerationChanged(WorkTraceError):
    """The caller's cursor belongs to a different candidate rebuild."""


class CandidateGenerationInconsistent(DatabaseError):
    """Candidate rows do not describe one transactional rebuild generation."""


@dataclass(frozen=True, slots=True)
class CandidateCursor:
    generation_token: str
    after_candidate_id: str


@dataclass(frozen=True, slots=True)
class CandidateListItem:
    candidate_id: str
    confirmed_contribution_id: str | None
    title: str | None
    source_text_is_untrusted: bool
    title_content_type: str | None
    title_authority: str
    title_status: str
    title_observation_types: tuple[str, ...]
    title_supporting_evidence_ids: tuple[str, ...]
    title_limitations: tuple[str, ...]
    contribution_type: str
    status: str
    period_from: str | None
    period_to: str | None
    source_coverage: tuple[str, ...]
    participation_indicators: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CandidatePage:
    generation_token: str | None
    items: tuple[CandidateListItem, ...]
    next_cursor: CandidateCursor | None


@dataclass(slots=True)
class _PageDiagnostics:
    scanned_candidate_ids: list[str] = field(default_factory=list)
    projected_candidate_ids: list[str] = field(default_factory=list)
    batch_limits: list[int] = field(default_factory=list)

    @property
    def raw_scan_count(self) -> int:
        return len(self.scanned_candidate_ids)

    @property
    def projection_count(self) -> int:
        return len(self.projected_candidate_ids)


@dataclass(frozen=True, slots=True)
class _Generation:
    generated_at: str
    generator_version: str
    token: str


def _generation_token(app_id: str, generated_at: str, generator_version: str) -> str:
    payload = json.dumps(
        [app_id, generated_at, generator_version],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _active_generation(
    connection: sqlite3.Connection,
    app_id: str,
) -> _Generation | None:
    row = connection.execute(
        _ACTIVE_GENERATION_SQL,
        (app_id,),
    ).fetchone()
    if row is None:
        return None
    generated_at = str(row["generated_at"])
    generator_version = str(row["generator_version"])

    # Transactional rebuilds replace every candidate for an application with one
    # generated_at value. Range probes use the existing app/time index and reject
    # reachable mixed-timestamp corruption without scanning the full generation.
    older = connection.execute(
        _OLDER_GENERATION_SQL,
        (app_id, generated_at),
    ).fetchone()
    newer = connection.execute(
        _NEWER_GENERATION_SQL,
        (app_id, generated_at),
    ).fetchone()
    if older is not None or newer is not None:
        raise CandidateGenerationInconsistent("candidate rows contain mixed rebuild timestamps")
    return _Generation(
        generated_at=generated_at,
        generator_version=generator_version,
        token=_generation_token(app_id, generated_at, generator_version),
    )


def _raw_candidate_batch(
    connection: sqlite3.Connection,
    *,
    app_id: str,
    generation: _Generation,
    after_candidate_id: str,
    limit: int,
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            _RAW_CANDIDATE_SQL,
            (after_candidate_id, app_id, generation.generated_at, limit),
        )
    )


def _validate_row_generation(row: sqlite3.Row, generation: _Generation) -> None:
    if str(row["generator_version"]) != generation.generator_version:
        raise CandidateGenerationInconsistent("candidate rows contain mixed generator versions")


def _has_raw_candidate_after(
    connection: sqlite3.Connection,
    *,
    app_id: str,
    generation: _Generation,
    after_candidate_id: str,
) -> bool:
    rows = _raw_candidate_batch(
        connection,
        app_id=app_id,
        generation=generation,
        after_candidate_id=after_candidate_id,
        limit=1,
    )
    if not rows:
        return False
    _validate_row_generation(rows[0], generation)
    return True


def _tuple_of_strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def _item_from_projection(projection: dict[str, object]) -> CandidateListItem:
    return CandidateListItem(
        candidate_id=str(projection["candidate_id"]),
        confirmed_contribution_id=(
            str(projection["confirmed_contribution_id"])
            if projection.get("confirmed_contribution_id") is not None
            else None
        ),
        title=str(projection["title"]) if projection.get("title") is not None else None,
        source_text_is_untrusted=bool(projection["source_text_is_untrusted"]),
        title_content_type=(
            str(projection["title_content_type"])
            if projection.get("title_content_type") is not None
            else None
        ),
        title_authority=str(projection["title_authority"]),
        title_status=str(projection["title_status"]),
        title_observation_types=_tuple_of_strings(projection["title_observation_types"]),
        title_supporting_evidence_ids=_tuple_of_strings(
            projection["title_supporting_evidence_ids"]
        ),
        title_limitations=_tuple_of_strings(projection["title_limitations"]),
        contribution_type=str(projection["suggested_type"]),
        status=str(projection["status"]),
        period_from=(
            str(projection["period_from"]) if projection.get("period_from") is not None else None
        ),
        period_to=(
            str(projection["period_to"]) if projection.get("period_to") is not None else None
        ),
        source_coverage=_tuple_of_strings(projection["source_coverage"]),
        participation_indicators=_tuple_of_strings(projection["participation_indicators"]),
    )


def candidate_page(
    connection: sqlite3.Connection,
    config: WorkTraceConfig,
    app_id: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    cursor: CandidateCursor | None = None,
) -> CandidatePage:
    return _candidate_page(
        connection,
        config,
        app_id,
        page_size=page_size,
        cursor=cursor,
        diagnostics=None,
    )


def _candidate_page(
    connection: sqlite3.Connection,
    config: WorkTraceConfig,
    app_id: str,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    cursor: CandidateCursor | None = None,
    diagnostics: _PageDiagnostics | None,
) -> CandidatePage:
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")
    if connection.in_transaction:
        raise DatabaseError("candidate page requires a connection outside a transaction")

    config.app(app_id)
    batch_size = page_size * 2
    scan_budget = min(page_size * 4, MAX_SCAN_BUDGET)
    from worktrace.packets.builder import PacketBuilder

    connection.execute("BEGIN")
    try:
        generation = _active_generation(connection, app_id)
        if generation is None:
            if cursor is not None:
                raise CandidateGenerationChanged(
                    "candidate generation changed; restart from the first page"
                )
            result = CandidatePage(generation_token=None, items=(), next_cursor=None)
        else:
            if cursor is not None and cursor.generation_token != generation.token:
                raise CandidateGenerationChanged(
                    "candidate generation changed; restart from the first page"
                )
            after_candidate_id = cursor.after_candidate_id if cursor is not None else ""
            items: list[CandidateListItem] = []
            raw_scans = 0
            decision_context = _build_decision_projection_context(connection)
            builder = PacketBuilder(
                connection,
                config,
                decision_context=decision_context,
            )

            while len(items) < page_size and raw_scans < scan_budget:
                batch_limit = min(batch_size, scan_budget - raw_scans)
                if diagnostics is not None:
                    diagnostics.batch_limits.append(batch_limit)
                rows = _raw_candidate_batch(
                    connection,
                    app_id=app_id,
                    generation=generation,
                    after_candidate_id=after_candidate_id,
                    limit=batch_limit,
                )
                if not rows:
                    break
                for row in rows:
                    _validate_row_generation(row, generation)
                    candidate_id = str(row["id"])
                    after_candidate_id = candidate_id
                    raw_scans += 1
                    if diagnostics is not None:
                        diagnostics.scanned_candidate_ids.append(candidate_id)
                        diagnostics.projected_candidate_ids.append(candidate_id)
                    try:
                        projection = builder.candidate_list_item(app_id, candidate_id)
                    except NotFound:
                        continue
                    items.append(_item_from_projection(projection))
                    if len(items) == page_size or raw_scans == scan_budget:
                        break

            has_more = bool(after_candidate_id) and _has_raw_candidate_after(
                connection,
                app_id=app_id,
                generation=generation,
                after_candidate_id=after_candidate_id,
            )
            next_cursor = (
                CandidateCursor(
                    generation_token=generation.token,
                    after_candidate_id=after_candidate_id,
                )
                if has_more
                else None
            )
            result = CandidatePage(
                generation_token=generation.token,
                items=tuple(items),
                next_cursor=next_cursor,
            )
        connection.execute("COMMIT")
        return result
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
