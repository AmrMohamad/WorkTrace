from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_mcp_security import _mcp_state
from worktrace.errors import ScopeViolation
from worktrace.mcp_server import tools as tool_module


def _rows(prefix: str, count: int, *, excluded: bool = False):
    for index in range(count):
        key = f"{prefix}:{index + 10}"
        item = None if excluded else {"reference_id" if prefix == "ref" else "candidate_id": key}
        yield {"phase": "after", "key": key}, item, index + 1 < count


def _install_streams(
    monkeypatch: pytest.MonkeyPatch, *, relations, memberships, generation: str = "a" * 64
) -> None:
    monkeypatch.setattr(tool_module, "scan_relations", lambda *args, **kwargs: iter(relations))
    monkeypatch.setattr(
        tool_module, "scan_memberships", lambda *args, **kwargs: (generation, iter(memberships))
    )


def test_context_uses_source_object_ids_and_starts_both_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, tools = _mcp_state(tmp_path)
    _install_streams(
        monkeypatch, relations=list(_rows("ref", 1)), memberships=list(_rows("candidate", 1))
    )
    result = tools.get_evidence_context(app_id="sample_store", object_id="obj:manual_1")
    assert result["object"]["object_id"] == "obj:manual_1"
    assert result["relations"]["requested"] is result["memberships"]["requested"] is True
    with pytest.raises(ScopeViolation):
        tools.get_evidence_context(app_id="sample_store", object_id="obs:manual_1")
    with pytest.raises(ScopeViolation):
        tools.get_evidence_context(app_id="other_app", object_id="obj:manual_1")


def test_context_continuations_are_independent_and_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, tools = _mcp_state(tmp_path)
    _install_streams(
        monkeypatch, relations=list(_rows("ref", 2)), memberships=list(_rows("candidate", 2))
    )
    first = tools.get_evidence_context(app_id="sample_store", object_id="obj:manual_1", limit=1)
    continued = tools.get_evidence_context(
        app_id="sample_store",
        object_id="obj:manual_1",
        relation_cursor=first["relations"]["next_cursor"],
        expected_view_token=first["view_token"],
    )
    assert continued["relations"]["requested"] is True
    assert continued["memberships"] == {
        "requested": False,
        "items": [],
        "next_cursor": None,
        "complete": None,
    }
    for cursor in (
        first["relation_cursor"]
        if "relation_cursor" in first
        else first["relations"]["next_cursor"],
        "offset:0",
    ):
        bad = tools.get_evidence_context(
            app_id="sample_store", object_id="obj:manual_2", relation_cursor=cursor
        )
        assert "error" in bad


def test_context_excluded_scan_and_confirmed_namespace_keep_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, tools = _mcp_state(tmp_path)
    excluded = list(_rows("ref", 200, excluded=True))
    excluded[-1] = (excluded[-1][0], excluded[-1][1], True)
    _install_streams(
        monkeypatch,
        relations=excluded,
        memberships=[
            (
                {"phase": "after", "key": "contribution:confirmed_1"},
                {"contribution_id": "contribution:confirmed_1", "role": "material"},
                True,
            )
        ],
    )
    result = tools.get_evidence_context(app_id="sample_store", object_id="obj:manual_1")
    assert result["relations"]["items"] == []
    assert result["relations"]["next_cursor"]
    assert result["memberships"]["next_cursor"]


def test_context_stale_and_collection_cursors_fail_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, tools = _mcp_state(tmp_path)
    _install_streams(monkeypatch, relations=list(_rows("ref", 2)), memberships=[])
    first = tools.get_evidence_context(app_id="sample_store", object_id="obj:manual_1", limit=1)
    stale = tools.get_evidence_context(
        app_id="sample_store", object_id="obj:manual_1", expected_view_token="view:1:" + "b" * 64
    )
    assert stale["error"]["code"] == "evidence_changed"
    wrong_collection = tools.get_evidence_context(
        app_id="sample_store",
        object_id="obj:manual_1",
        membership_cursor=first["relations"]["next_cursor"],
    )
    assert wrong_collection["error"]["code"] == "cursor_scope_mismatch"
