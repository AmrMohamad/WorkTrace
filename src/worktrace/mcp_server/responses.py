"""Budget admission before continuation: no eligible unsent row is skipped."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Iterator
from typing import cast

from worktrace.constants import MAX_RESPONSE_CHARS
from worktrace.mcp_server.limits import redact_output
from worktrace.packets.schema import PHASE4_QUESTIONS

type ScannedRow = tuple[dict[str, str], dict[str, object] | None, bool]
type CursorFactory = Callable[[dict[str, str]], str]


def serialized_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _clean(value: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], redact_output(value))


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _error(envelope: dict[str, object]) -> dict[str, object]:
    return {
        **{k: envelope[k] for k in ("app_id", "view_token", "read_model_version") if k in envelope},
        "error": {
            "code": "response_too_large",
            "message": "Response cannot fit safely; continuation was not advanced.",
        },
        "response_truncated": True,
    }


def _small_envelope(value: dict[str, object]) -> dict[str, object]:
    result = dict(value)
    sources = _mapping(value.get("source_status"))
    if sources:
        result["source_status"] = {
            name: {
                "complete": info.get("complete", False),
                "stale": info.get("stale", True),
                "has_warning": bool(info.get("warning"))
                or any(
                    _mapping(instance).get("limitations") or _mapping(instance).get("error_summary")
                    for instance in _list(info.get("instances"))
                ),
                "warning": str(info.get("warning") or "")[:192],
                "instance_count": len(_list(info.get("instances"))),
                "preflight_count": len(_list(info.get("preflight"))),
                "details_compacted": True,
            }
            for name, raw in sources.items()
            for info in [_mapping(raw)]
        }
    result["response_truncated"] = True
    result["envelope_compacted"] = True
    result["has_limitations"] = bool(value.get("limitations")) or bool(sources)
    result["limitation_count"] = len(_list(value.get("limitations")))
    result["limitations"] = [
        "Source-status detail compacted; absence of a listed warning is not proof "
        "of complete coverage."
    ]
    return result


def bounded_response(payload: dict[str, object]) -> dict[str, object]:
    result = _clean(payload)
    if serialized_size(result) <= MAX_RESPONSE_CHARS:
        return result
    result = _small_envelope(result)
    return result if serialized_size(result) <= MAX_RESPONSE_CHARS else _error(result)


def _minimal_item(item: dict[str, object]) -> dict[str, object]:
    retained = {
        "candidate_id",
        "confirmed_contribution_id",
        "contribution_id",
        "object_id",
        "evidence_id",
        "source",
        "kind",
        "status",
        "period_status",
        "period_from",
        "period_to",
        "date_from",
        "date_to",
        "title_authority",
        "title_status",
        "title_content_type",
        "source_text_is_untrusted",
        "participation_indicators",
        "source_coverage",
        "suggested_type",
        "content_type",
        "completeness",
        "question_id",
        "detail_kind",
        "ordinal",
        "text",
        "scope",
        "chunk_index",
        "chunk_count",
    }
    result = {key: value for key, value in item.items() if key in retained}
    for key in ("title", "text"):
        if isinstance(item.get(key), str):
            value = str(item[key])
            # Detail chunks must stay lossless. They are pre-sized, never shortened.
            result[key] = value if "detail_kind" in item else value[:192]
            if len(str(result[key])) < len(value):
                result[key + "_truncated"] = True
    result["has_contradictions"] = bool(
        item.get("has_contradictions") or item.get("contradictions")
    )
    result["has_limitations"] = bool(
        item.get("limitations") or item.get("warnings") or item.get("title_limitations")
    )
    result["item_compacted"] = True
    return result


def admit_page(
    envelope: dict[str, object],
    rows: Iterable[ScannedRow],
    *,
    item_key: str,
    limit: int,
    make_cursor: CursorFactory,
    initial_position: dict[str, str] | None = None,
) -> dict[str, object]:
    result = _clean(envelope)
    result[item_key], result["next_cursor"] = [], None
    if serialized_size(result) + 2100 > MAX_RESPONSE_CHARS:
        result = _small_envelope(result)
    if serialized_size(result) + 2100 > MAX_RESPONSE_CHARS:
        return _error(result)
    items: list[dict[str, object]] = []
    accepted_position = initial_position
    for position, raw_item, has_more in rows:
        if raw_item is None:
            accepted_position = position
            result["next_cursor"] = make_cursor(position) if has_more else None
            continue
        item = _clean(raw_item)
        next_cursor = make_cursor(position) if has_more else None
        trial = {**result, item_key: [*items, item], "next_cursor": next_cursor}
        if serialized_size(trial) > MAX_RESPONSE_CHARS:
            item = _minimal_item(item)
            trial = {
                **result,
                item_key: [*items, item],
                "next_cursor": next_cursor,
                "response_truncated": True,
            }
        if serialized_size(trial) > MAX_RESPONSE_CHARS:
            if not items:
                return _error(result)
            result["next_cursor"] = make_cursor(accepted_position) if accepted_position else None
            result["response_truncated"] = True
            break
        items.append(item)
        accepted_position = position
        result = trial
        if len(items) == limit:
            break
    return result if serialized_size(result) <= MAX_RESPONSE_CHARS else _error(result)


def _questions(packet: dict[str, object]) -> list[dict[str, object]]:
    by_id = {
        str(q.get("question_id")): q
        for group in _mapping(packet.get("sections")).values()
        for raw in _list(group)
        for q in [_mapping(raw)]
    }
    return [by_id[spec.question_id] for spec in PHASE4_QUESTIONS if spec.question_id in by_id]


def _header(question: dict[str, object]) -> dict[str, object]:
    support = _list(question.get("supporting_evidence_ids"))
    against = _list(question.get("contradicting_evidence_ids"))
    return {
        "question_id": question["question_id"],
        "question": question["question"],
        "status": question["status"],
        "answer_draft": None,
        "support_count": len(support),
        "contradiction_count": len(against),
        "has_support": bool(support),
        "has_contradictions": bool(against),
        "supporting_evidence_ids": support[:1],
        "contradicting_evidence_ids": against[:1],
        "limitation_count": len(_list(question.get("limitations"))),
        "missing_information_count": len(_list(question.get("missing_information"))),
        "details_available": True,
    }


def _compact_packet(
    packet: dict[str, object], questions: list[dict[str, object]]
) -> dict[str, object]:
    identity = _mapping(packet.get("identity_policy"))
    contribution = _mapping(packet.get("contribution"))
    result = _small_envelope(
        {
            key: packet[key]
            for key in (
                "schema_version",
                "app_id",
                "view_token",
                "read_model_version",
                "as_of",
                "source_status",
                "source_text_trust",
                "source_text_is_untrusted",
            )
            if key in packet
        }
    )
    result["contribution"] = {
        key: contribution[key]
        for key in (
            "id",
            "app_id",
            "candidate_id",
            "type",
            "date_from",
            "date_to",
            "period_status",
            "title_status",
            "title_authority",
            "title_supporting_evidence_ids",
            "title_observation_types",
            "title_content_type",
            "source_text_is_untrusted",
        )
        if key in contribution
    }
    compact_contribution = _mapping(result["contribution"])
    title = contribution.get("title")
    if isinstance(title, str):
        compact_contribution["title"] = title[:192]
        compact_contribution["title_truncated"] = len(title) > 192
    result["identity_policy"] = {
        "valid": identity.get("valid", False),
        "has_warnings": bool(identity.get("warnings")),
        "requires_rereview": bool(identity.get("requires_rereview")),
        "warnings": [str(warning)[:160] for warning in _list(identity.get("warnings"))[:8]],
        "warning_count": len(_list(identity.get("warnings"))),
        "warning_details_compacted": True,
    }
    result["release_ladder"] = {
        name: {
            "status": info.get("status", "unknown"),
            "has_support": bool(info.get("supporting_evidence_ids")),
            "has_contradictions": bool(info.get("contradicting_evidence_ids")),
        }
        for name, raw in _mapping(packet.get("release_ladder")).items()
        for info in [_mapping(raw)]
    }
    result["has_contradictions"] = bool(packet.get("contradictions")) or any(
        q.get("contradicting_evidence_ids") or q.get("status") == "contradicted" for q in questions
    )
    result["contradiction_count"] = len(_list(packet.get("contradictions")))
    sections: dict[str, list[dict[str, object]]] = {}
    section_by_id = {s.question_id: s.section for s in PHASE4_QUESTIONS}
    for question in questions:
        sections.setdefault(section_by_id[str(question["question_id"])], []).append(
            _header(question)
        )
    result["sections"] = sections
    evidence_summary = _mapping(packet.get("evidence_summary"))
    members = _list(evidence_summary.get("members"))
    unsupported = _list(evidence_summary.get("unsupported_member_ids"))
    result["evidence_summary"] = {
        "members": [
            {
                key: item[key]
                for key in (
                    "object_id",
                    "evidence_id",
                    "source",
                    "kind",
                    "context_only",
                    "current_complete_evidence",
                )
                if key in item
            }
            for raw in members
            for item in [_mapping(raw)]
        ],
        "member_count": len(members),
        "unsupported_member_ids": unsupported,
        "unsupported_member_count": len(unsupported),
        "has_contradictions": result["has_contradictions"],
    }
    result["packet_compacted"] = True
    result["detail_retrieval"] = {
        "tool": "build_phase4_packet",
        "selectors": ["section", "question_id"],
        "continuation_parameter": "detail_cursor",
    }
    if serialized_size(result) > MAX_RESPONSE_CHARS:
        summary = _mapping(result["evidence_summary"])
        summary.pop("members", None)
        summary.pop("unsupported_member_ids", None)
        summary["membership_details_compacted"] = True
        for group in sections.values():
            for question in group:
                question["supporting_evidence_ids"] = []
                question["contradicting_evidence_ids"] = []
                question["citation_details_omitted"] = True
    return result


_DETAIL_FIELDS = (
    "answer_draft",
    "supporting_evidence_ids",
    "contradicting_evidence_ids",
    "limitations",
    "missing_information",
)


def _details(
    questions: list[dict[str, object]],
) -> Iterator[tuple[dict[str, str], dict[str, object]]]:
    for question in questions:
        identifier = str(question["question_id"])
        for kind in _DETAIL_FIELDS:
            values = (
                [question[kind]]
                if kind == "answer_draft" and question.get(kind)
                else _list(question.get(kind))
            )
            ordinal = 0
            for value in values:
                text = str(value)
                chunks = (
                    [text]
                    if kind.endswith("evidence_ids")
                    else [text[i : i + 1200] for i in range(0, len(text), 1200)] or [""]
                )
                for index, chunk in enumerate(chunks):
                    position = {"question_id": identifier, "kind": kind, "ordinal": str(ordinal)}
                    item: dict[str, object] = {
                        "question_id": identifier,
                        "detail_kind": kind,
                        "ordinal": ordinal,
                    }
                    item["evidence_id" if kind.endswith("evidence_ids") else "text"] = chunk
                    if len(chunks) > 1:
                        item.update(chunk_index=index, chunk_count=len(chunks))
                    yield position, item
                    ordinal += 1


def shape_packet(
    packet: dict[str, object],
    *,
    section: str | None = None,
    question_id: str | None = None,
    after: dict[str, str] | None = None,
    limit: int = 20,
    make_cursor: CursorFactory | None = None,
) -> dict[str, object]:
    clean = _clean(packet)
    questions = _questions(clean)
    if len(questions) != len(PHASE4_QUESTIONS):
        return _error(clean)
    if section is None and question_id is None:
        if serialized_size(clean) <= MAX_RESPONSE_CHARS:
            return clean
        result = _compact_packet(clean, questions)
        return result if serialized_size(result) <= MAX_RESPONSE_CHARS else _error(result)
    ids = {
        s.question_id
        for s in PHASE4_QUESTIONS
        if (section is None or s.section == section)
        and (question_id is None or s.question_id == question_id)
    }
    selected = [q for q in questions if q["question_id"] in ids]
    envelope = _compact_packet(clean, selected)
    envelope["response_mode"] = "question_details"
    assert make_cursor is not None

    def scan() -> Iterator[ScannedRow]:
        found = after is None
        iterator = iter(_details(selected))
        previous = next(iterator, None)
        while previous is not None:
            following = next(iterator, None)
            position, item = previous
            if found:
                yield position, item, following is not None
            elif position == after:
                found = True
            previous = following
        if not found:
            from worktrace.mcp_server.protocol import ProtocolError

            raise ProtocolError(
                "invalid_cursor", "Detail continuation does not exist in this view."
            )

    result = admit_page(
        envelope,
        scan(),
        item_key="details",
        limit=limit,
        make_cursor=make_cursor,
        initial_position=after,
    )
    if "next_cursor" in result:
        result["detail_cursor"] = result.pop("next_cursor")
    return result
