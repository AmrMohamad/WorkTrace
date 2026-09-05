from __future__ import annotations

import json

from worktrace.constants import MAX_RESPONSE_CHARS
from worktrace.mcp_server.responses import admit_context


def _rows(name: str, count: int, *, excluded: bool = False, has_more_last: bool = False):
    for index in range(count):
        key = f"{name}:{index + 10}"
        item = None if excluded else {"reference_id" if name == "ref" else "candidate_id": key}
        yield ({"phase": "after", "key": key}, item, index + 1 < count or has_more_last)


def _cursor(prefix: str):
    return lambda position: f"wtc1:{prefix}:{position['key'] if position else '-'}"


def _admit(relations, memberships, limit: int = 10):
    return admit_context(
        {"app_id": "sample_store", "view_token": "view:1:" + "a" * 64},
        relation_rows=relations,
        membership_rows=memberships,
        limit=limit,
        relation_initial=None,
        membership_initial=None,
        relation_cursor=_cursor("r"),
        membership_cursor=_cursor("m"),
    )


def test_empty_requested_streams_are_successful_and_complete() -> None:
    result = _admit([], [])
    assert result["relations"] == {
        "requested": True,
        "items": [],
        "next_cursor": None,
        "complete": True,
    }
    assert result["memberships"] == {
        "requested": True,
        "items": [],
        "next_cursor": None,
        "complete": True,
    }


def test_initial_streams_get_start_cursors_and_each_limit_is_bounded() -> None:
    result = _admit(_rows("ref", 20), _rows("candidate", 20), limit=1)
    assert len(result["relations"]["items"]) == len(result["memberships"]["items"]) == 1
    assert result["relations"]["next_cursor"].endswith("ref:10")
    assert result["memberships"]["next_cursor"].endswith("candidate:10")


def test_excluded_scan_rows_preserve_continuation_after_two_hundred_rows() -> None:
    result = _admit(_rows("ref", 200, excluded=True, has_more_last=True), [], limit=20)
    assert result["relations"]["items"] == []
    assert result["relations"]["complete"] is False
    assert result["relations"]["next_cursor"].endswith("ref:209")


def test_combined_cap_is_twenty_even_when_each_stream_allows_twenty() -> None:
    result = _admit(_rows("ref", 30), _rows("candidate", 30), limit=20)
    assert len(result["relations"]["items"]) + len(result["memberships"]["items"]) == 20


def test_context_compaction_preserves_required_relation_and_membership_fields() -> None:
    relation = {
        "reference_id": "ref:one",
        "direction": "outgoing",
        "from_object_id": "obj:a",
        "to_object_id": "obj:b",
        "relationship_type": "relates_to",
        "exact_value": "x" * 40_000,
        "from_endpoint": {"object_id": "obj:a"},
        "to_endpoint": {"object_id": "obj:b"},
    }
    membership = {
        "object_id": "obj:a",
        "candidate_id": "candidate:one",
        "role": "material",
        "basis": "confirmed",
        "status": "confirmed",
        "citations": ["obs:one"],
    }
    result = _admit(
        iter([({"phase": "after", "key": "ref:one"}, relation, False)]),
        iter([({"phase": "after", "key": "candidate:one"}, membership, False)]),
    )
    assert {
        "reference_id",
        "from_object_id",
        "to_object_id",
        "relationship_type",
        "from_endpoint",
        "to_endpoint",
    } <= set(result["relations"]["items"][0])
    assert {"object_id", "candidate_id", "role", "basis", "status"} <= set(
        result["memberships"]["items"][0]
    )


def test_unicode_budget_never_skips_eligible_row() -> None:
    huge = {
        "reference_id": "ref:unicode",
        "exact_value": "é" * 19_000,
        "relationship_type": "exact",
    }
    result = _admit(iter([({"phase": "after", "key": "ref:unicode"}, huge, True)]), [])
    assert result["relations"]["items"][0]["reference_id"] == "ref:unicode"
    assert result["relations"]["next_cursor"].endswith("ref:unicode")
    assert len(json.dumps(result, ensure_ascii=True, separators=(",", ":"))) <= MAX_RESPONSE_CHARS
