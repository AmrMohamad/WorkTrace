"""Stable, source-scoped identifiers."""

from __future__ import annotations

import hashlib


def _stable_digest(*parts: str) -> str:
    canonical = "\x1f".join(parts).encode("utf-8", errors="strict")
    return hashlib.sha256(canonical).hexdigest()[:32]


def stable_source_object_id(
    source_kind: str,
    source_instance: str,
    object_type: str,
    external_id: str,
) -> str:
    """Return an idempotent identity without exposing provider identifiers."""

    return f"src_{_stable_digest(source_kind, source_instance, object_type, external_id)}"


def stable_actor_id(source_kind: str, source_instance: str, source_actor_id: str) -> str:
    """Actors remain source-specific until an explicit human alias maps them."""

    return f"actor_{_stable_digest(source_kind, source_instance, source_actor_id)}"
