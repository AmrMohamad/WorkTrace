from __future__ import annotations

import copy
import json
import re
from typing import cast

from worktrace.constants import MAX_RESPONSE_CHARS

type JsonScalar = str | int | float | bool | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]

_OUTPUT_REDACTIONS = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(
            r"(?i)\b(authorization\s*:\s*(?:bearer|basic)|bearer)\s+"
            r"[A-Za-z0-9._~+/=-]+"
        ),
        "[REDACTED_AUTHORIZATION]",
    ),
    (
        re.compile(
            r"(?i)\b(x[_-]api[_-]key|client[_-]?secret|api[_-]?key|api[_-]?token|"
            r"access[_-]?token|private[_-]?token|refresh[_-]?token|session[_-]?id|"
            r"session[_-]?token|jsessionid|password|secret|token|cookie)\s*[:=]\s*"
            r"[^\s,;]+"
        ),
        "[REDACTED_SECRET]",
    ),
    (
        re.compile(
            r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w-])",
            re.IGNORECASE,
        ),
        "[REDACTED_EMAIL]",
    ),
    (re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{8,}\d)(?!\w)"), "[REDACTED_PHONE]"),
)

_SENSITIVE_OUTPUT_KEYS = {
    "access_token",
    "api_key",
    "api_token",
    "authorization",
    "client_secret",
    "cookie",
    "cookies",
    "password",
    "private_token",
    "refresh_token",
    "secret",
    "session_id",
    "session_token",
    "token",
    "x-api-key",
    "x_api_key",
}


def _redact_string(value: str) -> str:
    result = value
    for pattern, replacement in _OUTPUT_REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


def redact_output(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in _SENSITIVE_OUTPUT_KEYS
                else redact_output(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_output(item) for item in value]
    return _redact_string(str(value))


def _serialized_size(value: JsonValue) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _longest_string(value: JsonValue) -> tuple[object, object, str] | None:
    best: tuple[object, object, str] | None = None

    def visit(current: JsonValue) -> None:
        nonlocal best
        if isinstance(current, dict):
            for key, item in current.items():
                if isinstance(item, str):
                    if best is None or len(item) > len(best[2]):
                        best = (current, key, item)
                else:
                    visit(item)
        elif isinstance(current, list):
            for index, item in enumerate(current):
                if isinstance(item, str):
                    if best is None or len(item) > len(best[2]):
                        best = (current, index, item)
                else:
                    visit(item)

    visit(value)
    return best


def _longest_list(value: JsonValue) -> list[JsonValue] | None:
    best: list[JsonValue] | None = None

    def visit(current: JsonValue) -> None:
        nonlocal best
        if isinstance(current, dict):
            for item in current.values():
                visit(item)
        elif isinstance(current, list):
            if best is None or len(current) > len(best):
                best = current
            for item in current:
                visit(item)

    visit(value)
    return best


def enforce_total_limit(payload: dict[str, object]) -> dict[str, object]:
    sanitized = redact_output(payload)
    if not isinstance(sanitized, dict):
        raise TypeError("MCP response must be an object")
    result = copy.deepcopy(sanitized)
    if _serialized_size(result) <= MAX_RESPONSE_CHARS:
        return cast(dict[str, object], result)

    result["response_truncated"] = True
    limitations = result.get("limitations")
    if not isinstance(limitations, list):
        limitations = []
        result["limitations"] = limitations
    limitations.append("Response was truncated to the server-wide text budget.")

    while _serialized_size(result) > MAX_RESPONSE_CHARS:
        longest = _longest_string(result)
        if longest is not None and len(longest[2]) > 64:
            parent, key, text = longest
            shortened = text[: max(32, len(text) // 2)] + "…[truncated]"
            if isinstance(parent, dict):
                parent[str(key)] = shortened
            elif isinstance(parent, list) and isinstance(key, int):
                parent[key] = shortened
            continue
        longest_list = _longest_list(result)
        if longest_list is not None and len(longest_list) > 1:
            del longest_list[max(1, len(longest_list) // 2) :]
            continue
        break

    if _serialized_size(result) > MAX_RESPONSE_CHARS:
        minimal: dict[str, JsonValue] = {
            "as_of": result.get("as_of"),
            "source_text_trust": result.get("source_text_trust", "untrusted"),
            "source_text_is_untrusted": True,
            "response_truncated": True,
            "limitations": ["Response exceeded the server-wide text budget."],
        }
        return cast(dict[str, object], minimal)
    return cast(dict[str, object], result)
