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

# These keys are emitted by WorkTrace itself and carry opaque ledger identifiers,
# not provider prose.  Preserve only values that satisfy the public stable-ID
# grammar; provider-controlled fields such as ``external_id`` and ``source_instance``
# deliberately remain subject to output redaction.
_STABLE_IDENTIFIER_KEYS = frozenset(
    {
        "actor_id",
        "availability_evidence_id",
        "candidate_id",
        "confirmed_contribution_id",
        "contribution_id",
        "decision_id",
        "evidence_id",
        "from_object_id",
        "import_session_id",
        "metadata_source_object_id",
        "object_id",
        "observation_evidence_id",
        "participation_evidence_id",
        "reference_id",
        "replaces_decision_id",
        "run_id",
        "seed_object_id",
        "source_object_id",
        "supporting_observation_id",
        "sync_run_id",
        "target_id",
        "to_object_id",
        "undo_target_id",
        "unsupported_seed_object_id",
    }
)
_STABLE_IDENTIFIER_LIST_KEYS = frozenset(
    {
        "candidate_ids",
        "claim_supporting_evidence_ids",
        "compensated_by_decision_ids",
        "contradicting_evidence_ids",
        "contribution_ids",
        "decision_ids",
        "evidence_ids",
        "module_evidence_ids",
        "period_evidence_ids",
        "supporting_evidence_ids",
        "title_supporting_evidence_ids",
        "unsupported_member_ids",
    }
)
_STABLE_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{1,127}(?::[A-Za-z0-9._-]{1,128})*$")
_INTERNAL_ID_PREFIXES = frozenset(
    {
        "actor",
        "availability",
        "candidate",
        "contribution",
        "decision",
        "obj",
        "obs",
        "part",
        "participation",
        "ref",
        "run",
        "session",
        "source",
    }
)


def _redact_string(value: str) -> str:
    result = value
    for pattern, replacement in _OUTPUT_REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


def _is_structured_identifier(value: str, field_name: str | None) -> bool:
    if field_name in {"view_token", "expected_view_token"}:
        return re.fullmatch(r"view:1:[0-9a-f]{64}", value) is not None
    if field_name in {"cursor", "next_cursor", "detail_cursor"}:
        return len(value) <= 2048 and re.fullmatch(r"wtc1:[A-Za-z0-9_-]+", value) is not None
    if field_name is None or _STABLE_IDENTIFIER.fullmatch(value) is None:
        return False
    key = field_name.casefold()
    if key in _STABLE_IDENTIFIER_KEYS or key in _STABLE_IDENTIFIER_LIST_KEYS:
        return True
    if key == "id":
        return value.partition(":")[0].casefold() in _INTERNAL_ID_PREFIXES
    return False


def redact_output(value: object, *, field_name: str | None = None) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _is_structured_identifier(value, field_name):
            return value
        return _redact_string(value)
    if isinstance(value, dict):
        return {
            str(key): (
                "[REDACTED]"
                if str(key).casefold() in _SENSITIVE_OUTPUT_KEYS
                else redact_output(item, field_name=str(key))
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_output(item, field_name=field_name) for item in value]
    return _redact_string(str(value))


def _serialized_size(value: JsonValue) -> int:
    # Count the conservative escaped representation too: truncation markers and
    # provider Unicode must not put an otherwise bounded response over the cap.
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _longest_string(
    value: JsonValue,
    *,
    protected_string_keys: frozenset[str] = frozenset(),
) -> tuple[object, object, str] | None:
    best: tuple[object, object, str] | None = None

    def visit(current: JsonValue, field_name: str | None = None) -> None:
        nonlocal best
        if isinstance(current, dict):
            for key, item in current.items():
                if isinstance(item, str):
                    if (
                        key not in protected_string_keys
                        and not _is_structured_identifier(item, key)
                        and (best is None or len(item) > len(best[2]))
                    ):
                        best = (current, key, item)
                else:
                    visit(item, key)
        elif isinstance(current, list):
            for index, item in enumerate(current):
                if isinstance(item, str):
                    if not _is_structured_identifier(item, field_name) and (
                        best is None or len(item) > len(best[2])
                    ):
                        best = (current, index, item)
                else:
                    visit(item, field_name)

    visit(value)
    return best


def _longest_list(
    value: JsonValue,
    *,
    protected_list_keys: frozenset[str] = frozenset(),
) -> list[JsonValue] | None:
    best: list[JsonValue] | None = None

    def visit(current: JsonValue, field_name: str | None = None) -> None:
        nonlocal best
        if isinstance(current, dict):
            for key, item in current.items():
                visit(item, key)
        elif isinstance(current, list):
            if field_name not in protected_list_keys and (best is None or len(current) > len(best)):
                best = current
            for item in current:
                visit(item)

    visit(value)
    return best


def enforce_total_limit(
    payload: dict[str, object],
    *,
    protected_list_keys: frozenset[str] = frozenset(),
    protected_string_keys: frozenset[str] = frozenset(),
) -> dict[str, object]:
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
        longest = _longest_string(result, protected_string_keys=protected_string_keys)
        if longest is not None and len(longest[2]) > 64:
            parent, key, text = longest
            shortened = text[: max(32, len(text) // 2)] + "…[truncated]"
            if isinstance(parent, dict):
                parent[str(key)] = shortened
            elif isinstance(parent, list) and isinstance(key, int):
                parent[key] = shortened
            continue
        longest_list = _longest_list(result, protected_list_keys=protected_list_keys)
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
