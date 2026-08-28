"""Actor normalization that preserves source roles without name-based merging."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from worktrace.adapters.base import ActorIdentity, ParticipationRole
from worktrace.normalize.identities import stable_actor_id
from worktrace.normalize.redaction import Redactor

_TRAILER_PATTERN = re.compile(
    r"^(Co-authored-by|Reviewed-by):\s*(.*?)\s*<([^<>]+)>\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class GitTrailerActor:
    role: ParticipationRole
    name: str
    email: str


def actor_identity(
    *,
    source_kind: str,
    source_instance: str,
    redactor: Redactor,
    provider_actor_id: str | None,
    display_name: str | None,
    username: str | None = None,
    email: str | None = None,
) -> ActorIdentity:
    email_hash = redactor.hash_email(email) if email else None
    if provider_actor_id:
        source_actor_id = provider_actor_id
    elif email_hash:
        source_actor_id = email_hash
    else:
        basis = (username or display_name or "unknown").strip().casefold()
        source_actor_id = f"anonymous:{hashlib.sha256(basis.encode()).hexdigest()[:24]}"
    return ActorIdentity(
        source_actor_id=source_actor_id,
        stable_id=stable_actor_id(source_kind, source_instance, source_actor_id),
        display_name=redactor.redact_text(display_name) if display_name else None,
        username=redactor.redact_text(username) if username else None,
        email_hash=email_hash,
    )


def parse_git_trailers(message: str) -> tuple[GitTrailerActor, ...]:
    """Parse only explicit co-author/reviewer trailers from commit text."""

    result: list[GitTrailerActor] = []
    for match in _TRAILER_PATTERN.finditer(message):
        label, name, email = match.groups()
        role = (
            ParticipationRole.CO_AUTHOR
            if label.casefold() == "co-authored-by"
            else ParticipationRole.REVIEWER
        )
        result.append(GitTrailerActor(role=role, name=name.strip(), email=email.strip()))
    return tuple(result)
