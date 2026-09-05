"""Bounded consistency markers; these are not authentication capabilities."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime

from worktrace.config import WorkTraceConfig
from worktrace.db.read_state import READ_MODEL_VERSION, READ_PROTOCOL_VERSION
from worktrace.errors import ScopeViolation
from worktrace.packets.builder import PacketBuilder

_TOKEN = re.compile(r"^view:1:[0-9a-f]{64}$")
_CURSOR_PREFIX = "wtc1:"
_POSITIONS = {
    "candidates": {"candidate_id"},
    "evidence": {"sort_time", "observation_id"},
    "packet_details": {"question_id", "kind", "ordinal"},
    "context_relations": {"phase", "key"},
    "context_memberships": {"phase", "key"},
}


class ProtocolError(ScopeViolation):
    def __init__(self, code: str, message: str, *, view_token: str | None = None) -> None:
        super().__init__(message)
        self.code, self.view_token = code, view_token

    def response(self) -> dict[str, object]:
        result: dict[str, object] = {
            "error": {"code": self.code, "message": str(self), "restart_required": True}
        }
        if self.view_token is not None:
            result["view_token"] = self.view_token
        return result


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def view_token(builder: PacketBuilder, app_id: str, epoch: str) -> str:
    config: WorkTraceConfig = builder.config
    app = config.app(app_id)
    connection = builder.connection
    revision = connection.execute("SELECT read_revision FROM apps WHERE id=?", (app_id,)).fetchone()
    protocol = connection.execute(
        "SELECT version FROM agent_read_protocol WHERE singleton=1"
    ).fetchone()
    if revision is None or protocol is None or protocol[0] != READ_PROTOCOL_VERSION:
        raise ProtocolError(
            "read_protocol_unavailable", "Use the matching CLI to upgrade this ledger."
        )
    binding = connection.execute(
        "SELECT ledger_id FROM identity_key_binding WHERE singleton=1"
    ).fetchone()
    database = connection.execute("PRAGMA database_list").fetchone()
    digest = fingerprint(
        {
            "app_id": app_id,
            "revision": int(revision[0]),
            "protocol": READ_PROTOCOL_VERSION,
            "model": READ_MODEL_VERSION,
            "epoch": epoch,
            "ledger": str(binding[0]) if binding else None,
            "database": str(database[2]) if database else None,
            "config": {
                "app": asdict(app),
                "identity": asdict(config.identity),
                "from": config.employment_from,
                "to": config.employment_to,
                "timezone": config.employment_timezone,
            },
        }
    )
    return "view:1:" + digest


def check_expected(expected: str | None, actual: str) -> None:
    if expected is None:
        return
    if not isinstance(expected, str) or _TOKEN.fullmatch(expected) is None:
        raise ProtocolError(
            "invalid_view_token", "expected_view_token is not a supported view token."
        )
    if expected != actual:
        raise ProtocolError(
            "evidence_changed",
            "Evidence changed; refresh before combining results.",
            view_token=actual,
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate cursor key")
        result[key] = value
    return result


def decode_cursor(
    value: str | None,
    *,
    collection: str,
    app_id: str,
    view: str,
    filters: str,
    object_fingerprint: str | None = None,
) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("offset:"):
        raise ProtocolError(
            "cursor_upgrade_required",
            "Legacy offset cursors are unsupported; restart without a cursor.",
        )
    if not isinstance(value, str) or len(value) > 2048 or not value.startswith(_CURSOR_PREFIX):
        raise ProtocolError("invalid_cursor", "Cursor is malformed or unsupported.")
    try:
        encoded = value[len(_CURSOR_PREFIX) :]
        if re.fullmatch(r"[A-Za-z0-9_-]+", encoded) is None:
            raise ValueError("bad alphabet")
        raw = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw, object_pairs_hook=_unique_object)
        base_fields = {
            "v",
            "collection",
            "app",
            "view",
            "filters",
            "generation",
            "position",
        }
        if not isinstance(payload, dict) or (
            set(payload) != base_fields and set(payload) != base_fields | {"object_fingerprint"}
        ):
            raise ValueError("bad fields")
        if type(payload["v"]) is not int or payload["v"] != 1:
            raise ValueError("bad version")
        if not all(isinstance(payload[k], str) for k in ("collection", "app", "view", "filters")):
            raise ValueError("bad types")
        context_fingerprint = payload.get("object_fingerprint")
        if context_fingerprint is not None and (
            not isinstance(context_fingerprint, str)
            or re.fullmatch(r"[0-9a-f]{64}", context_fingerprint) is None
        ):
            raise ValueError("bad object fingerprint")
        if (
            _TOKEN.fullmatch(payload["view"]) is None
            or re.fullmatch(r"[0-9a-f]{64}", payload["filters"]) is None
        ):
            raise ValueError("bad fingerprints")
        position = payload["position"]
        if not isinstance(position, dict) or set(position) != _POSITIONS.get(
            payload["collection"], set()
        ):
            raise ValueError("bad position")
        if not all(
            isinstance(item, str) and 0 < len(item) <= 256 and "\x00" not in item
            for item in position.values()
        ):
            raise ValueError("bad position value")
        generation = payload["generation"]
        if generation is not None and (
            not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{64}", generation) is None
        ):
            raise ValueError("bad generation")
        if (
            payload["collection"] not in {"candidates", "context_memberships"}
            and generation is not None
        ):
            raise ValueError("unexpected generation")
        if payload["collection"] in {"context_relations", "context_memberships"}:
            if context_fingerprint is None:
                raise ValueError("missing object fingerprint")
            if position["phase"] not in {"start", "after"}:
                raise ValueError("bad context phase")
            if position["phase"] == "start" and position["key"] != "-":
                raise ValueError("bad context start")
            if position["phase"] == "after" and not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]{1,127}(?::[A-Za-z0-9._-]{1,128})*", position["key"]
            ):
                raise ValueError("bad context key")
            if (
                position["phase"] == "after"
                and payload["collection"] == "context_relations"
                and not position["key"].startswith("ref:")
            ):
                raise ValueError("bad relation key")
            if (
                position["phase"] == "after"
                and payload["collection"] == "context_memberships"
                and not position["key"].startswith(("candidate:", "contribution:"))
            ):
                raise ValueError("bad membership key")
        if (
            payload["collection"] == "evidence"
            and datetime.fromisoformat(position["sort_time"].replace("Z", "+00:00")).tzinfo is None
        ):
            raise ValueError("missing cursor timezone")
        if payload["collection"] == "packet_details":
            if re.fullmatch(r"[0-9]{1,9}", position["ordinal"]) is None:
                raise ValueError("bad detail ordinal")
            if position["kind"] not in {
                "answer_draft",
                "supporting_evidence_ids",
                "contradicting_evidence_ids",
                "limitations",
                "missing_information",
            }:
                raise ValueError("bad detail kind")
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise ProtocolError("invalid_cursor", "Cursor is malformed or unsupported.") from exc
    if payload["collection"] != collection or payload["app"] != app_id:
        raise ProtocolError(
            "cursor_scope_mismatch", "Cursor belongs to a different app or collection."
        )
    if payload["filters"] != filters:
        raise ProtocolError("cursor_filter_mismatch", "Filters changed; restart without a cursor.")
    if context_fingerprint != object_fingerprint:
        raise ProtocolError(
            "cursor_scope_mismatch", "Cursor belongs to a different context object."
        )
    check_expected(payload["view"], view)
    return payload


def encode_cursor(
    *,
    collection: str,
    app_id: str,
    view: str,
    filters: str,
    position: dict[str, str],
    generation: str | None = None,
    object_fingerprint: str | None = None,
) -> str:
    payload: dict[str, object] = {
        "v": 1,
        "collection": collection,
        "app": app_id,
        "view": view,
        "filters": filters,
        "generation": generation,
        "position": position,
    }
    if object_fingerprint is not None:
        payload["object_fingerprint"] = object_fingerprint
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    result = _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(result) > 2048:
        raise ProtocolError("response_too_large", "Continuation cannot fit the cursor budget.")
    return result
