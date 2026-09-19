from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResourceCheckpoint:
    """The durable boundary at which one archive resource may safely resume."""

    id: str
    collection_id: str
    revision_id: str
    run_id: str
    issue_id: str
    kind: str
    locator_json: str
    state: str
    completeness: str
    availability: str
    expected_count: int | None
    seen_count: int
    page_cursor: str | None
    attempt: int
    raw_vault_object_id: str | None
    redaction_version: str
    source_updated_at: str | None
    fetched_at: str | None
    error_json: str | None
