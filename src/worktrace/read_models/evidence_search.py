"""Bounded, TUI-only evidence discovery over one immutable read snapshot."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Literal, cast

from worktrace.candidates.builder import GENERATOR_VERSION
from worktrace.errors import DatabaseError, NotFound, ScopeViolation
from worktrace.packets.builder import PacketBuilder
from worktrace.packets.models import ContributionView
from worktrace.read_models.agent_pages import scan_evidence
from worktrace.read_models.candidates import _active_generation
from worktrace.read_models.evidence_context import (
    CanonicalMembershipLocator,
    canonical_membership_locators,
    describe_object,
    project_canonical_membership,
    resolve_canonical_group,
)

TUI_EVIDENCE_SEARCH_READ_MODEL_VERSION = 1
EVIDENCE_SEARCH_PAGE_SIZE = 20
EVIDENCE_SEARCH_SCAN_BUDGET = 200
EVIDENCE_SEARCH_PROJECTION_BUDGET = 50
EVIDENCE_SEARCH_LINK_CAP = 5
_SOURCES = frozenset({"git", "jira", "gitlab", "manual"})


class EvidenceSearchValidationError(ValueError):
    """A submitted TUI search filter is outside the frozen reader contract."""


class EvidenceSearchInvalidated(DatabaseError):
    """A continuation cannot safely describe the current application view."""


@dataclass(frozen=True, slots=True)
class EvidenceSearchFilters:
    query: str
    source: str | None
    module_text: str | None
    date_from: str | None
    date_to: str | None


@dataclass(frozen=True, slots=True)
class EvidenceSearchCursor:
    app_id: str
    filters: EvidenceSearchFilters
    revision: int
    read_model_version: int
    after_sort_time: str
    after_observation_id: str


@dataclass(frozen=True, slots=True)
class CandidateLink:
    candidate_id: str
    contribution_id: str | None
    status: str
    role: str
    confirmation_basis: str
    evidence_state: str
    limitations: tuple[str, ...]

    @property
    def identifier(self) -> str:
        """The only contribution-review identifier a link is permitted to open."""

        return self.contribution_id or self.candidate_id


@dataclass(frozen=True, slots=True)
class EvidenceSearchItem:
    evidence_id: str
    object_id: str
    source: str
    kind: str
    title: str | None
    period_from: str | None
    period_to: str | None
    period_status: str
    period_limitations: tuple[str, ...]
    links: tuple[CandidateLink, ...]
    link_completeness: bool
    link_limit_reason: Literal["projection_budget", "display_cap"] | None


@dataclass(frozen=True, slots=True)
class EvidenceSearchDiagnostics:
    scanned_rows: int
    eligible_results: int
    returned_results: int
    projection_attempts: int
    projection_budget: int
    display_link_count: int


@dataclass(frozen=True, slots=True)
class EvidenceSearchPage:
    app_id: str
    filters: EvidenceSearchFilters
    items: tuple[EvidenceSearchItem, ...]
    next_cursor: EvidenceSearchCursor | None
    revision: int
    read_model_version: int
    readiness: Mapping[str, object]
    limitations: tuple[str, ...]
    diagnostics: EvidenceSearchDiagnostics


def _optional_text(value: str | None, name: str, maximum: int | None = None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EvidenceSearchValidationError(f"{name} must be text")
    normalized = value.strip()
    if not normalized:
        return None
    if "\0" in normalized:
        raise EvidenceSearchValidationError(f"{name} must not contain NUL")
    if maximum is not None and len(normalized) > maximum:
        raise EvidenceSearchValidationError(f"{name} must be at most {maximum} characters")
    return normalized


def normalize_evidence_search_filters(
    query: str,
    *,
    source: str | None = None,
    module_text: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> EvidenceSearchFilters:
    """Normalize the one filter contract shared by screen and direct callers."""

    normalized_query = _optional_text(query, "query", 500)
    if normalized_query is None:
        raise EvidenceSearchValidationError("query must contain 1 to 500 characters")
    normalized_source = _optional_text(source, "source")
    if normalized_source is not None:
        normalized_source = normalized_source.casefold()
        if normalized_source not in _SOURCES:
            allowed = ", ".join(sorted(_SOURCES))
            raise EvidenceSearchValidationError(f"source must be one of: {allowed}")
    normalized_module = _optional_text(module_text, "module text", 200)
    normalized_from = _optional_text(date_from, "date_from")
    normalized_to = _optional_text(date_to, "date_to")
    for name, value in (("date_from", normalized_from), ("date_to", normalized_to)):
        if value is None:
            continue
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise EvidenceSearchValidationError(f"{name} must be an ISO calendar date") from exc
    if (
        normalized_from is not None
        and normalized_to is not None
        and normalized_from > normalized_to
    ):
        raise EvidenceSearchValidationError("date_from must not be after date_to")
    return EvidenceSearchFilters(
        query=normalized_query,
        source=normalized_source,
        module_text=normalized_module,
        date_from=normalized_from,
        date_to=normalized_to,
    )


def _freeze(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _read_revision(connection: sqlite3.Connection, app_id: str) -> int:
    row = connection.execute("SELECT read_revision FROM apps WHERE id=?", (app_id,)).fetchone()
    if row is None:
        # config.app() already checked scope; a missing app table row is ledger corruption.
        raise DatabaseError("configured application is missing from the read model")
    return int(row[0])


def _period_limitations(
    period_status: str, date_from: str | None, date_to: str | None
) -> tuple[str, ...]:
    if not (date_from or date_to) or period_status != "unknown":
        return ()
    return ("Undated evidence is excluded while an activity-date filter is supplied.",)


def _link_from_projection(value: dict[str, object]) -> CandidateLink:
    raw_limitations = value.get("limitations")
    return CandidateLink(
        candidate_id=str(value["candidate_id"]),
        contribution_id=(
            str(value["contribution_id"]) if value.get("contribution_id") is not None else None
        ),
        status=str(value["status"]),
        role=str(value["role"]),
        confirmation_basis=str(value["basis"]),
        evidence_state=str(value["evidence_state"]),
        limitations=(
            tuple(str(item) for item in raw_limitations if isinstance(item, str))
            if isinstance(raw_limitations, list)
            else ()
        ),
    )


def _is_admitted_search_object(
    builder: PacketBuilder, app_id: str, value: Mapping[str, object]
) -> bool:
    """Apply the canonical configured-source guard before page admission.

    Scanner order/positions remain authoritative: an out-of-config or vanished
    row is excluded, not an exception which can strand the continuation.  The
    public application and continuation checks occur before this per-row guard.
    """

    try:
        describe_object(builder, app_id, str(value["object_id"]))
    except (NotFound, ScopeViolation):
        return False
    return True


def _resolve_canonical_membership(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    locator: CanonicalMembershipLocator,
    *,
    legacy_generated: bool,
) -> ContributionView | None:
    """Resolve exactly one group; callers count every invocation as an attempt."""

    del object_id
    return resolve_canonical_group(
        builder,
        app_id,
        locator,
        legacy_generated=legacy_generated,
    )


def _link_for_object(
    builder: PacketBuilder,
    app_id: str,
    object_id: str,
    locator: CanonicalMembershipLocator,
    contribution: ContributionView,
) -> CandidateLink | None:
    projected = project_canonical_membership(builder, app_id, object_id, locator, contribution)
    return _link_from_projection(projected) if projected is not None else None


def _enrich(
    builder: PacketBuilder,
    app_id: str,
    raw_items: list[dict[str, object]],
) -> tuple[list[tuple[CandidateLink, ...]], list[bool], list[str | None], int]:
    """Allocate a single fair projection budget across all displayed results."""

    generation = _active_generation(builder.connection, app_id)
    legacy_generated = generation is not None and generation.generator_version != GENERATOR_VERSION
    locators = [
        list(canonical_membership_locators(builder, app_id, str(item["object_id"])))
        for item in raw_items
    ]
    positions = [0] * len(raw_items)
    links: list[list[CandidateLink]] = [[] for _ in raw_items]
    limits: list[str | None] = [None] * len(raw_items)
    cache: dict[str, ContributionView | None] = {}
    attempts = 0
    progressed = True
    while progressed:
        progressed = False
        for index, item in enumerate(raw_items):
            if limits[index] == "display_cap":
                continue
            while positions[index] < len(locators[index]):
                locator = locators[index][positions[index]]
                positions[index] += 1
                progressed = True
                if locator.key in cache:
                    contribution = cache[locator.key]
                elif attempts >= EVIDENCE_SEARCH_PROJECTION_BUDGET:
                    # This distinct group remains unprojected, but later
                    # locators can still reuse a group cached by another row.
                    # Consume this locator so an exhausted page cannot spin.
                    limits[index] = "projection_budget"
                    break
                else:
                    attempts += 1
                    contribution = _resolve_canonical_membership(
                        builder,
                        app_id,
                        str(item["object_id"]),
                        locator,
                        legacy_generated=legacy_generated,
                    )
                    cache[locator.key] = contribution
                # Test seams and the old single-object resolver return an
                # already-shaped link.  Production caches the group instead,
                # then projects its role independently for each evidence row.
                link = (
                    contribution
                    if isinstance(contribution, CandidateLink)
                    else _link_for_object(
                        builder, app_id, str(item["object_id"]), locator, contribution
                    )
                    if contribution is not None
                    else None
                )
                if link is not None:
                    links[index].append(link)
                    if len(links[index]) == EVIDENCE_SEARCH_LINK_CAP:
                        if positions[index] < len(locators[index]):
                            limits[index] = "display_cap"
                        break
                # Round robin gives each result one uncached/cached group per pass.
                break
    complete = [
        positions[index] == len(locators[index]) and limits[index] is None
        for index in range(len(raw_items))
    ]
    return [tuple(value) for value in links], complete, limits, attempts


def evidence_search_page(
    connection: sqlite3.Connection,
    builder: PacketBuilder,
    app_id: str,
    filters: EvidenceSearchFilters,
    *,
    cursor: EvidenceSearchCursor | None,
    expected_revision: int | None = None,
) -> EvidenceSearchPage:
    """Build one page without opening a connection or transaction of its own."""

    builder.config.app(app_id)
    revision = _read_revision(connection, app_id)
    if expected_revision is not None and expected_revision != revision:
        raise EvidenceSearchInvalidated("Evidence changed; restart the search from its first page.")
    if cursor is not None and (
        cursor.app_id != app_id
        or cursor.filters != filters
        or cursor.revision != revision
        or cursor.read_model_version != TUI_EVIDENCE_SEARCH_READ_MODEL_VERSION
    ):
        raise EvidenceSearchInvalidated("Evidence changed; restart the search from its first page.")
    page_builder = builder.page_projection_builder(app_id)
    rows = scan_evidence(
        page_builder,
        filters.query,
        app_id,
        source_types=(filters.source,) if filters.source else (),
        actor_id=None,
        module=filters.module_text,
        date_from=filters.date_from,
        date_to=filters.date_to,
        after=(cursor.after_sort_time, cursor.after_observation_id) if cursor else None,
        page_builder=page_builder,
    )
    raw_items: list[dict[str, object]] = []
    scanned_rows = 0
    eligible = 0
    last_position: dict[str, str] | None = None
    has_more = False
    for position, projected, more in rows:
        scanned_rows += 1
        last_position = position
        has_more = more
        if projected is None:
            if scanned_rows == EVIDENCE_SEARCH_SCAN_BUDGET:
                break
            continue
        if not _is_admitted_search_object(page_builder, app_id, projected):
            if scanned_rows == EVIDENCE_SEARCH_SCAN_BUDGET:
                break
            continue
        eligible += 1
        raw_items.append(projected)
        if len(raw_items) == EVIDENCE_SEARCH_PAGE_SIZE:
            break
        if scanned_rows == EVIDENCE_SEARCH_SCAN_BUDGET:
            break
    links, complete, limits, attempts = _enrich(page_builder, app_id, raw_items)
    items = tuple(
        EvidenceSearchItem(
            evidence_id=str(value["evidence_id"]),
            object_id=str(value["object_id"]),
            source=str(value["source"]),
            kind=str(value["kind"]),
            title=(str(value["title"])[:500] if value.get("title") is not None else None),
            period_from=str(value["date_from"]) if value.get("date_from") is not None else None,
            period_to=str(value["date_to"]) if value.get("date_to") is not None else None,
            period_status=str(value["period_status"]),
            period_limitations=_period_limitations(
                str(value["period_status"]), filters.date_from, filters.date_to
            ),
            links=links[index],
            link_completeness=complete[index],
            link_limit_reason=cast(
                Literal["projection_budget", "display_cap"] | None,
                limits[index] if limits[index] in {"projection_budget", "display_cap"} else None,
            ),
        )
        for index, value in enumerate(raw_items)
    )
    next_cursor = (
        EvidenceSearchCursor(
            app_id=app_id,
            filters=filters,
            revision=revision,
            read_model_version=TUI_EVIDENCE_SEARCH_READ_MODEL_VERSION,
            after_sort_time=last_position["sort_time"],
            after_observation_id=last_position["observation_id"],
        )
        if has_more and last_position is not None
        else None
    )
    readiness = _freeze(page_builder.source_status(app_id))
    assert isinstance(readiness, Mapping)
    return EvidenceSearchPage(
        app_id=app_id,
        filters=filters,
        items=items,
        next_cursor=next_cursor,
        revision=revision,
        read_model_version=TUI_EVIDENCE_SEARCH_READ_MODEL_VERSION,
        readiness=readiness,
        limitations=(
            "Undated evidence is excluded while an activity-date filter is supplied."
            if filters.date_from or filters.date_to
            else "Undated evidence is included when no activity-date filter is supplied.",
        ),
        diagnostics=EvidenceSearchDiagnostics(
            scanned_rows=scanned_rows,
            eligible_results=eligible,
            returned_results=len(items),
            projection_attempts=attempts,
            projection_budget=EVIDENCE_SEARCH_PROJECTION_BUDGET,
            display_link_count=sum(len(item.links) for item in items),
        ),
    )
