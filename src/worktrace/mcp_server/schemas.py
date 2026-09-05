from __future__ import annotations

import re
from datetime import date

from worktrace.constants import MAX_EXCERPT_CHARS, MAX_RECORDS
from worktrace.errors import ScopeViolation

_STABLE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,127}(?::[A-Za-z0-9._-]{1,128})*$")
_APP_ID = re.compile(r"^[a-z0-9][a-z0-9_]{0,127}$")
_ALLOWED_SOURCES = frozenset({"git", "jira", "gitlab", "manual"})


def stable_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ScopeViolation(f"{field} must be a stable WorkTrace ID")
    return value


def app_id(value: str) -> str:
    if not isinstance(value, str) or not _APP_ID.fullmatch(value):
        raise ScopeViolation("app_id is invalid")
    return value


def bounded_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1 or value > MAX_RECORDS:
        raise ScopeViolation(f"limit must be between 1 and {MAX_RECORDS}")
    return value


def excerpt_limit(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < 1
        or value > MAX_EXCERPT_CHARS
    ):
        raise ScopeViolation(f"max_chars must be between 1 and {MAX_EXCERPT_CHARS}")
    return value


def iso_date(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise ScopeViolation(f"{field} must be an ISO date") from exc


def query_text(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScopeViolation("query must not be empty")
    normalized = value.strip()
    if len(normalized) > 500:
        raise ScopeViolation("query is limited to 500 characters")
    if "\x00" in normalized:
        raise ScopeViolation("query contains an invalid character")
    return normalized


def source_types(values: list[str] | None) -> tuple[str, ...]:
    if values is None:
        return ()
    normalized = tuple(dict.fromkeys(value.casefold() for value in values))
    if any(value not in _ALLOWED_SOURCES for value in normalized):
        raise ScopeViolation("source_types contains an unsupported source")
    return normalized


def optional_filter(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 200 or "\x00" in normalized:
        raise ScopeViolation(f"{field} is invalid")
    return normalized
