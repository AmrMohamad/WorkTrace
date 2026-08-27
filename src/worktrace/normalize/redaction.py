"""Pre-persistence redaction and bounded text extraction."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from worktrace.adapters.base import JSONValue
from worktrace.constants import MAX_STORED_TEXT

_EMAIL_PATTERN = re.compile(
    r"(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w-])",
    flags=re.IGNORECASE,
)
_INLINE_SECRET_PATTERN = re.compile(
    r"(?i)\b(authorization\s*:\s*(?:bearer|basic)|bearer|x[_-]api[_-]key\s*[=:]|"
    r"(?:token|password|client[_-]?secret|secret|api[_-]?(?:key|token)|"
    r"access[_-]?token|private[_-]?token|refresh[_-]?token)\s*[=:])"
    r"\s*[^\s,;&]+"
)
_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_PHONE_PATTERN = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)")
_SESSION_PATTERN = re.compile(
    r"(?i)\b(?:session[_-]?id|jsessionid|session_token)\s*[=:]\s*[^\s,;&]+"
)
_REMOVED_KEYS = frozenset(
    {
        "attachment",
        "attachments",
        "authorization",
        "access_token",
        "api_key",
        "api_token",
        "cookie",
        "cookies",
        "client_secret",
        "diff",
        "diffs",
        "password",
        "patch",
        "private_token",
        "refresh_token",
        "secret",
        "session_id",
        "session_token",
        "jsessionid",
        "token",
        "x-api-key",
        "x_api_key",
    }
)
_URL_KEYS = frozenset({"url", "web_url", "self", "avatar_url"})
_STABLE_ID_KEYS = frozenset(
    {
        "candidate_id",
        "candidate_ids",
        "compensates",
        "keep_source_object_ids",
        "source_object_id",
        "source_object_ids",
    }
)
_STABLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*:[A-Za-z0-9._:-]{1,256}$")


def _sanitize_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return "[REDACTED_URL]"
    host = parts.hostname
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


@dataclass(frozen=True, slots=True)
class Redactor:
    """Deterministic redaction configured with a private local email key."""

    email_key: bytes
    max_text_chars: int = MAX_STORED_TEXT
    max_collection_items: int = 1_000
    max_depth: int = 50

    def __post_init__(self) -> None:
        if not self.email_key:
            raise ValueError("email_key must not be empty")
        if self.max_text_chars < 1:
            raise ValueError("max_text_chars must be positive")
        if self.max_collection_items < 1 or self.max_depth < 1:
            raise ValueError("redaction bounds must be positive")

    def hash_email(self, email: str) -> str:
        normalized = email.strip().casefold().encode("utf-8", errors="strict")
        digest = hmac.new(self.email_key, normalized, hashlib.sha256).hexdigest()
        return f"email_hmac_sha256:{digest}"

    def redact_text(self, value: str) -> str:
        redacted = _EMAIL_PATTERN.sub(lambda match: self.hash_email(match.group(1)), value)
        redacted = _INLINE_SECRET_PATTERN.sub("[REDACTED_SECRET]", redacted)
        redacted = _PRIVATE_KEY_PATTERN.sub("[REDACTED_PRIVATE_KEY]", redacted)
        redacted = _PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
        redacted = _SESSION_PATTERN.sub("[REDACTED_SESSION]", redacted)
        if len(redacted) <= self.max_text_chars:
            return redacted
        return f"{redacted[: self.max_text_chars]}\n[TRUNCATED]"

    def redact_payload(
        self,
        value: Any,
        *,
        field_name: str | None = None,
        _depth: int = 0,
    ) -> JSONValue:
        """Return JSON-safe content with secret/diff/attachment fields removed."""

        key = field_name.casefold() if field_name is not None else None
        if key in _REMOVED_KEYS:
            return "[REDACTED]"
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, str):
            if key in _URL_KEYS:
                return _sanitize_url(value)
            if key is not None and (key == "email" or key.endswith("_email")):
                return self.hash_email(value)
            # WorkTrace-generated stable IDs are opaque structured values, not
            # source prose.  Preserve valid IDs so a UUID-like run of digits
            # cannot be mistaken for a phone number by the text redactor.
            if key in _STABLE_ID_KEYS and _STABLE_ID_PATTERN.fullmatch(value):
                return value
            return self.redact_text(value)
        if _depth >= self.max_depth:
            return "[TRUNCATED_DEPTH]"
        if isinstance(value, list | tuple):
            output_list = [
                self.redact_payload(item, field_name=field_name, _depth=_depth + 1)
                for item in value[: self.max_collection_items]
            ]
            if len(value) > self.max_collection_items:
                output_list.append("[TRUNCATED_COLLECTION]")
            return output_list
        if isinstance(value, dict):
            output: dict[str, JSONValue] = {}
            for index, (child_key, child_value) in enumerate(value.items()):
                if index >= self.max_collection_items:
                    output["_worktrace_truncated"] = True
                    break
                if not isinstance(child_key, str):
                    continue
                output[child_key] = self.redact_payload(
                    child_value,
                    field_name=child_key,
                    _depth=_depth + 1,
                )
            return output
        return self.redact_text(str(value))


def extract_jira_text(value: object, *, max_chars: int = MAX_STORED_TEXT) -> str | None:
    """Extract only text nodes from Jira ADF; links/media are never followed."""

    parts: list[str] = []
    total_chars = 0
    visited_nodes = 0
    stack = [value]
    while stack and total_chars < max_chars and visited_nodes < 10_000:
        node = stack.pop()
        visited_nodes += 1
        if isinstance(node, dict):
            if node.get("type") == "text" and isinstance(node.get("text"), str):
                text = str(node["text"])[: max_chars - total_chars]
                parts.append(text)
                total_chars += len(text)
            content = node.get("content")
            if isinstance(content, list):
                stack.extend(reversed(content))
        elif isinstance(node, list):
            stack.extend(reversed(node))

    if isinstance(value, str):
        return value[:max_chars]
    text = "\n".join(part for part in parts if part).strip()
    return text[:max_chars] or None
