from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from worktrace.domain.enums import Completeness

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]


@dataclass(frozen=True)
class SourceIdentity:
    source: str
    source_instance: str
    kind: str
    external_id: str
    canonical_url: str | None = None


@dataclass(frozen=True)
class AvailabilityObservation:
    source: str
    source_instance: str
    kind: str
    external_id: str
    reason: str = "not_found"


@dataclass(frozen=True)
class ActorObservation:
    source: str
    source_instance: str
    external_actor_id: str
    display_name: str
    email_hash: str | None = None
    is_self: bool = False


@dataclass(frozen=True)
class ParticipationObservation:
    actor_external_id: str
    role: str
    effective_from: datetime | None = None
    effective_to: datetime | None = None
    details: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class PendingReference:
    target_source: str
    target_kind: str
    target_external_id: str
    relationship_type: str
    extraction_method: str
    exact_value: str | None = None


@dataclass(frozen=True)
class ChangedPath:
    old_path: str | None
    new_path: str
    additions: int | None = None
    deletions: int | None = None
    new_file: bool = False
    renamed_file: bool = False
    deleted_file: bool = False
    generated_file: bool | None = None
    too_large: bool | None = None


@dataclass(frozen=True)
class NormalizedObject:
    identity: SourceIdentity
    app_id: str
    title: str | None
    body_text: str | None
    source_updated_at: datetime | None
    actors: tuple[ActorObservation, ...]
    participations: tuple[ParticipationObservation, ...]
    pending_references: tuple[PendingReference, ...]
    data: dict[str, JsonValue]
    completeness: Completeness


@dataclass(frozen=True)
class RepositoryInfo:
    root: Path
    remotes: tuple[str, ...]
