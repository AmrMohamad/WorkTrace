from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable

from worktrace.constants import MAX_RESPONSE_CHARS
from worktrace.mcp_server.responses import admit_page, bounded_response, shape_packet
from worktrace.packets.schema import PHASE4_QUESTIONS, PHASE4_SCHEMA_VERSION


def _cursor(position: dict[str, str]) -> str:
    return "wtc1:" + json.dumps(position, sort_keys=True, separators=(",", ":")).encode().hex()


def _envelope() -> dict[str, object]:
    return {
        "app_id": "app:budget",
        "view_token": "view:1:" + "a" * 64,
        "read_model_version": 1,
        "source_status": {},
    }


def _packet(*, large: bool) -> dict[str, object]:
    sections: dict[str, list[dict[str, object]]] = defaultdict(list)
    for index, spec in enumerate(PHASE4_QUESTIONS):
        answer = f"Answer for {spec.question_id}."
        limitations = [f"Limitation for {spec.question_id}."]
        missing = [f"Missing detail for {spec.question_id}."]
        if large:
            answer = "Unicode answer 🚀 " * 400
            limitations = ["Unicode limitation ✨ " * 400]
            missing = ["Unicode missing 🧭 " * 400]
        sections[spec.section].append(
            {
                "question_id": spec.question_id,
                "question": spec.text,
                "answer_draft": answer,
                "status": "supported" if index % 2 == 0 else "unknown",
                "supporting_evidence_ids": [f"obs:support-{index}", f"obs:extra-{index}"],
                "contradicting_evidence_ids": [f"obs:against-{index}"] if index % 3 == 0 else [],
                "limitations": limitations,
                "missing_information": missing,
            }
        )
    return {
        **_envelope(),
        "schema_version": PHASE4_SCHEMA_VERSION,
        "contribution": {
            "id": "contribution:budget",
            "app_id": "app:budget",
            "candidate_id": "candidate:budget",
            "type": "feature",
            "period_status": "known",
            "title_status": "supported",
            "title_authority": "provider_observed",
        },
        "identity_policy": {"valid": True, "warnings": [], "requires_rereview": False},
        "release_ladder": {},
        "contradictions": [{"evidence_ids": ["obs:packet-contradiction"]}],
        "sections": dict(sections),
    }


def _rows(
    *items: tuple[dict[str, str], dict[str, object] | None, bool],
) -> Iterable[tuple[dict[str, str], dict[str, object] | None, bool]]:
    return items


def test_admission_never_skips_an_oversized_unicode_eligible_row_on_retry() -> None:
    huge_indicator = "🚀" * (MAX_RESPONSE_CHARS + 1)
    rows = _rows(
        ({"candidate_id": "candidate:accepted"}, {"candidate_id": "candidate:accepted"}, True),
        (
            {"candidate_id": "candidate:oversized"},
            {
                "candidate_id": "candidate:oversized",
                "participation_indicators": [huge_indicator],
            },
            True,
        ),
        ({"candidate_id": "candidate:later"}, {"candidate_id": "candidate:later"}, False),
    )

    first = admit_page(_envelope(), rows, item_key="candidates", limit=20, make_cursor=_cursor)
    assert [item["candidate_id"] for item in first["candidates"]] == ["candidate:accepted"]
    assert first["next_cursor"] == _cursor({"candidate_id": "candidate:accepted"})
    assert first["response_truncated"] is True

    retry = admit_page(
        _envelope(),
        _rows(
            (
                {"candidate_id": "candidate:oversized"},
                {
                    "candidate_id": "candidate:oversized",
                    "participation_indicators": [huge_indicator],
                },
                True,
            ),
        ),
        item_key="candidates",
        limit=20,
        make_cursor=_cursor,
        initial_position={"candidate_id": "candidate:accepted"},
    )
    assert retry["error"]["code"] == "response_too_large"
    assert "candidate:later" not in json.dumps(retry, ensure_ascii=True)


def test_filtered_empty_pages_advance_to_the_last_excluded_position() -> None:
    result = admit_page(
        _envelope(),
        _rows(
            ({"observation_id": "obs:one"}, None, True),
            ({"observation_id": "obs:two"}, None, True),
        ),
        item_key="results",
        limit=20,
        make_cursor=_cursor,
        initial_position={"observation_id": "obs:previous"},
    )
    assert result["results"] == []
    assert result["next_cursor"] == _cursor({"observation_id": "obs:two"})


def test_response_redaction_preserves_opaque_cursor_token_and_stable_ids() -> None:
    result = bounded_response(
        {
            **_envelope(),
            "next_cursor": "wtc1:opaque_cursor_123",
            "detail_cursor": "wtc1:opaque_detail_456",
            "candidate_id": "candidate:budget",
            "supporting_evidence_ids": ["obs:support-1", "obs:support-2"],
            "provider_text": "token=provider-secret",
        }
    )
    assert result["view_token"] == _envelope()["view_token"]
    assert result["next_cursor"] == "wtc1:opaque_cursor_123"
    assert result["detail_cursor"] == "wtc1:opaque_detail_456"
    assert result["candidate_id"] == "candidate:budget"
    assert result["supporting_evidence_ids"] == ["obs:support-1", "obs:support-2"]
    assert result["provider_text"] == "[REDACTED_SECRET]"


def test_small_packet_stays_full_and_large_packet_keeps_all_question_headers() -> None:
    small = _packet(large=False)
    assert shape_packet(small) == bounded_response(small)

    compact = shape_packet(_packet(large=True))
    headers = [header for values in compact["sections"].values() for header in values]
    assert compact["packet_compacted"] is True
    assert compact["response_truncated"] is True
    assert len(headers) == len(PHASE4_QUESTIONS) == 30
    assert [header["question_id"] for header in headers] == [
        spec.question_id for spec in PHASE4_QUESTIONS
    ]
    assert [header["status"] for header in headers] == [
        "supported" if index % 2 == 0 else "unknown" for index, _ in enumerate(PHASE4_QUESTIONS)
    ]
    assert all(header["has_support"] for header in headers)
    assert [header["has_contradictions"] for header in headers] == [
        index % 3 == 0 for index, _ in enumerate(PHASE4_QUESTIONS)
    ]


def test_packet_detail_pages_reconstruct_every_lossless_answer_and_detail_field() -> None:
    packet = _packet(large=True)
    section = "contribution_identity"
    expected = {
        question["question_id"]: {
            "answer_draft": [question["answer_draft"]],
            "supporting_evidence_ids": question["supporting_evidence_ids"],
            "contradicting_evidence_ids": question["contradicting_evidence_ids"],
            "limitations": question["limitations"],
            "missing_information": question["missing_information"],
        }
        for question in packet["sections"][section]
    }
    after: dict[str, str] | None = None
    recovered: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    while True:
        page = shape_packet(
            packet,
            section=section,
            after=after,
            limit=2,
            make_cursor=_cursor,
        )
        assert page["response_mode"] == "question_details"
        for detail in page["details"]:
            identifier = str(detail["question_id"])
            kind = str(detail["detail_kind"])
            value = str(detail.get("evidence_id", detail.get("text")))
            recovered[identifier][kind].append(value)
        cursor = page.get("detail_cursor")
        if cursor is None:
            break
        after = json.loads(bytes.fromhex(str(cursor).removeprefix("wtc1:")).decode())

    for question_id, fields in expected.items():
        assert "".join(recovered[question_id]["answer_draft"]) == fields["answer_draft"][0]
        assert (
            recovered[question_id]["supporting_evidence_ids"] == fields["supporting_evidence_ids"]
        )
        assert (
            recovered[question_id]["contradicting_evidence_ids"]
            == fields["contradicting_evidence_ids"]
        )
        assert "".join(recovered[question_id]["limitations"]) == fields["limitations"][0]
        assert (
            "".join(recovered[question_id]["missing_information"])
            == fields["missing_information"][0]
        )
