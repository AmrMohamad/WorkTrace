"""Provider-neutral contracts emitted by every source adapter.

Adapters do not write the evidence ledger. They emit already-redacted, bounded
records so the repository layer can persist a page atomically.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

type JSONScalar = str | int | float | bool | None
type JSONValue = JSONScalar | list[JSONValue] | dict[str, JSONValue]


class ParticipationRole(StrEnum):
    """Observed roles; a role is a relationship, never an ownership claim."""

    AUTHOR = "author"
    COMMITTER = "committer"
    CO_AUTHOR = "co_author"
    REVIEWER = "reviewer"
    APPROVER = "approver"
    ASSIGNEE = "assignee"
    REPORTER = "reporter"
    CREATOR = "creator"
    MERGER = "merger"
    DEPLOYER = "deployer"
    RELEASE_AUTHOR = "release_author"


class ReferenceStrength(StrEnum):
    """How a source expressed a relationship."""

    STRUCTURED = "structured"
    EXACT_TEXT = "exact_text"


@dataclass(frozen=True, slots=True)
class SourceObjectIdentity:
    source_kind: str
    source_instance: str
    object_type: str
    external_id: str
    stable_id: str
    app_id: str


@dataclass(frozen=True, slots=True)
class ObservationMetadata:
    observed_at: str
    source_updated_at: str | None
    adapter_version: str
    normalization_version: str
    redaction_version: str


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """Source-scoped actor identity with no persistable raw email."""

    source_actor_id: str
    stable_id: str
    display_name: str | None = None
    username: str | None = None
    email_hash: str | None = None


@dataclass(frozen=True, slots=True)
class Participation:
    actor: ActorIdentity
    role: ParticipationRole
    effective_from: str | None = None
    effective_to: str | None = None


@dataclass(frozen=True, slots=True)
class Reference:
    reference_type: str
    target_external_id: str
    strength: ReferenceStrength
    target_source_kind: str | None = None
    target_object_type: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedRecord:
    identity: SourceObjectIdentity
    observation: ObservationMetadata
    payload: dict[str, JSONValue]
    participations: tuple[Participation, ...]
    references: tuple[Reference, ...]
    payload_hash: str
    untrusted_text_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UnavailableObjectDescriptor:
    """Exact stable object observed unavailable without implying source failure."""

    kind: str
    external_id: str
    reason: str = "not_found"


@dataclass(frozen=True, slots=True)
class NormalizedPage:
    """One repository transaction boundary from a full snapshot."""

    source_kind: str
    source_instance: str
    resource_type: str
    cursor: str | None
    next_cursor: str | None
    is_last: bool
    records: tuple[NormalizedRecord, ...]
    unavailable_objects: tuple[UnavailableObjectDescriptor, ...] = ()
    limitations: tuple[str, ...] = ()
    selection_events: tuple[dict[str, JSONValue], ...] = ()
    records_selection_biased: bool = False


class SnapshotAdapter(Protocol):
    """A read-only full-snapshot source adapter."""

    def iter_pages(self) -> Iterator[NormalizedPage]: ...
