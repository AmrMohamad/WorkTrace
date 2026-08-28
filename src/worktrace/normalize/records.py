"""Canonical record construction and payload hashing."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from worktrace.adapters.base import (
    JSONValue,
    NormalizedRecord,
    ObservationMetadata,
    Participation,
    Reference,
    SourceObjectIdentity,
)
from worktrace.constants import ADAPTER_VERSION, NORMALIZATION_VERSION, REDACTION_VERSION
from worktrace.normalize.identities import stable_source_object_id
from worktrace.normalize.redaction import Redactor


def normalize_timestamp(value: str | datetime | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip().replace("Z", "+00:00")
        parsed = datetime.fromisoformat(candidate)
    else:
        parsed = value
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def observed_now() -> str:
    value = normalize_timestamp(datetime.now(tz=UTC))
    assert value is not None
    return value


def build_record(
    *,
    source_kind: str,
    source_instance: str,
    object_type: str,
    external_id: str,
    app_id: str,
    observed_at: str,
    source_updated_at: str | None,
    payload: Mapping[str, object],
    redactor: Redactor,
    participations: Iterable[Participation] = (),
    references: Iterable[Reference] = (),
    untrusted_text_fields: Iterable[str] = (),
) -> NormalizedRecord:
    redacted = redactor.redact_payload(dict(payload))
    if not isinstance(redacted, dict):
        raise TypeError("record payload must normalize to an object")
    canonical_payload: dict[str, JSONValue] = redacted
    canonical = json.dumps(redacted, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return NormalizedRecord(
        identity=SourceObjectIdentity(
            source_kind=source_kind,
            source_instance=source_instance,
            object_type=object_type,
            external_id=external_id,
            stable_id=stable_source_object_id(
                source_kind,
                source_instance,
                object_type,
                external_id,
            ),
            app_id=app_id,
        ),
        observation=ObservationMetadata(
            observed_at=observed_at,
            source_updated_at=normalize_timestamp(source_updated_at),
            adapter_version=ADAPTER_VERSION,
            normalization_version=NORMALIZATION_VERSION,
            redaction_version=REDACTION_VERSION,
        ),
        payload=canonical_payload,
        participations=tuple(participations),
        references=tuple(references),
        payload_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        untrusted_text_fields=tuple(untrusted_text_fields),
    )
