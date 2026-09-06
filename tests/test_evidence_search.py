from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import worktrace.read_models.evidence_search as evidence_search_module
from tests.public_workflow_support import PublicWorkflowFixture
from tests.test_packets_golden import _packet_state
from tests.tui_support import add_visible_candidate_page
from worktrace.candidates.decisions import append_decision
from worktrace.config import load_config
from worktrace.db.connection import connect, connect_read_only
from worktrace.packets.builder import PacketBuilder
from worktrace.read_models import evidence_context as evidence_context_module
from worktrace.read_models.evidence_search import (
    CandidateLink,
    CanonicalMembershipLocator,
    EvidenceSearchCursor,
    EvidenceSearchInvalidated,
    EvidenceSearchValidationError,
    evidence_search_page,
    normalize_evidence_search_filters,
)
from worktrace.read_workspace import ReadOnlyWorkspace


def _locator(key: str) -> CanonicalMembershipLocator:
    return CanonicalMembershipLocator(
        key=key,
        identifier=f"candidate:{key}",
        contribution_id=None,
        candidate_id=f"candidate:{key}",
        confirmed=False,
    )


def _link(locator: CanonicalMembershipLocator) -> CandidateLink:
    return CandidateLink(
        candidate_id=locator.candidate_id,
        contribution_id=None,
        status="suggestion",
        role="material",
        confirmation_basis="suggestion",
        evidence_state="authoritative_current",
        limitations=(),
    )


def _enrich_with(
    monkeypatch: pytest.MonkeyPatch,
    locator_map: dict[str, tuple[CanonicalMembershipLocator, ...]],
    resolver: Any,
) -> tuple[list[tuple[CandidateLink, ...]], list[bool], list[str | None], int]:
    monkeypatch.setattr(evidence_search_module, "_active_generation", lambda *_: None)
    monkeypatch.setattr(
        evidence_search_module,
        "canonical_membership_locators",
        lambda _builder, _app_id, object_id: locator_map[object_id],
    )
    monkeypatch.setattr(evidence_search_module, "_resolve_canonical_membership", resolver)
    return evidence_search_module._enrich(
        SimpleNamespace(connection=None),
        "sample",
        [{"object_id": object_id} for object_id in locator_map],
    )


def test_normalize_evidence_search_filters_trims_optionals_and_is_frozen() -> None:
    filters = normalize_evidence_search_filters(
        "  literal * and ?  ",
        source=" git ",
        module_text="  src/checkout/*?  ",
        date_from=" 2026-01-01 ",
        date_to="2026-01-31 ",
    )

    assert filters.query == "literal * and ?"
    assert filters.source == "git"
    assert filters.module_text == "src/checkout/*?"
    assert filters.date_from == "2026-01-01"
    assert filters.date_to == "2026-01-31"
    with pytest.raises(FrozenInstanceError):
        filters.query = "changed"  # type: ignore[misc]

    blank_optionals = normalize_evidence_search_filters(
        "literal", source=" ", module_text="\t", date_from="", date_to="  "
    )
    assert blank_optionals.source is None
    assert blank_optionals.module_text is None
    assert blank_optionals.date_from is None
    assert blank_optionals.date_to is None


def test_evidence_search_reuses_prepared_page_context_without_changing_page_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, _, config, _ = _packet_state(tmp_path)
    add_visible_candidate_page(connection, count=30)
    connection.close()
    workspace = ReadOnlyWorkspace(config)
    filters = normalize_evidence_search_filters("Synthetic")
    baseline = workspace.search_evidence("sample_store", filters)

    original = PacketBuilder.page_projection_builder
    calls = 0

    def counted(self: PacketBuilder, app_id: str) -> PacketBuilder:
        nonlocal calls
        calls += 1
        return original(self, app_id)

    monkeypatch.setattr(PacketBuilder, "page_projection_builder", counted)
    actual = workspace.search_evidence("sample_store", filters)

    assert calls == 1
    assert actual.items == baseline.items
    assert [item.links for item in actual.items] == [item.links for item in baseline.items]
    assert [item.link_completeness for item in actual.items] == [
        item.link_completeness for item in baseline.items
    ]
    assert [item.period_limitations for item in actual.items] == [
        item.period_limitations for item in baseline.items
    ]
    assert [[link.limitations for link in item.links] for item in actual.items] == [
        [link.limitations for link in item.links] for item in baseline.items
    ]
    assert actual.diagnostics == baseline.diagnostics
    assert actual.diagnostics.returned_results <= 20
    assert actual.diagnostics.scanned_rows <= 200
    assert actual.diagnostics.projection_attempts <= 50
    assert all(len(item.links) <= 5 for item in actual.items)


def test_page_context_builder_falls_back_when_context_is_not_prepared(tmp_path: Path) -> None:
    connection, _, config, _ = _packet_state(tmp_path)
    try:
        builder = PacketBuilder(connection, config)
        prepared = evidence_context_module._page_context_builder(builder, "sample_store")
    finally:
        connection.close()

    assert prepared is not builder


@pytest.mark.parametrize(
    ("query", "kwargs"),
    [
        ("", {}),
        ("\x00", {}),
        ("x" * 501, {}),
        ("valid", {"source": "github"}),
        ("valid", {"module_text": "\x00"}),
        ("valid", {"module_text": "x" * 201}),
        ("valid", {"date_from": "2026-02-30"}),
        ("valid", {"date_to": "not-a-date"}),
        ("valid", {"date_from": "2026-02-01", "date_to": "2026-01-31"}),
    ],
)
def test_normalize_evidence_search_filters_rejects_invalid_inputs(
    query: str, kwargs: dict[str, str]
) -> None:
    with pytest.raises(EvidenceSearchValidationError):
        normalize_evidence_search_filters(query, **kwargs)


def test_imported_synthetic_evidence_is_searchable_before_grouping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery is proved from actual public imports, not fixture membership rows."""
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch)
    assert fixture.invoke("init").exit_code == 0
    imported = fixture.invoke("import", "all", "sample")
    assert imported.exit_code == 0, imported.output

    config = load_config(fixture.config_path)
    connection = connect_read_only(config.database_path)
    try:
        assert connection.execute("SELECT COUNT(*) FROM human_decisions").fetchone()[0] == 0
    finally:
        connection.close()

    workspace = ReadOnlyWorkspace(config)
    page = workspace.search_evidence(
        "sample",
        normalize_evidence_search_filters("DEMO-1 context"),
    )

    assert page.items
    assert any(item.source == "git" for item in page.items)
    assert page.diagnostics.returned_results == len(page.items)

    literal_wildcard = workspace.search_evidence(
        "sample",
        normalize_evidence_search_filters("DEMO-*"),
    )
    assert literal_wildcard.items == ()

    rebuilt = fixture.invoke("rebuild", "all", "sample")
    assert rebuilt.exit_code == 0, rebuilt.output
    linked = workspace.search_evidence(
        "sample",
        normalize_evidence_search_filters("DEMO-1 context"),
    )
    evidence = next(item for item in linked.items if item.source == "git")
    link = next(item for item in evidence.links if item.role == "material")
    assert link.status == "suggestion"
    assert link.confirmation_basis == "suggestion"
    assert link.contribution_id is None
    assert link.candidate_id.startswith("candidate:")

    review = workspace.contribution_review("sample", link.identifier)
    assert review.status == "candidate"
    summary = review.packet["evidence_summary"]
    assert isinstance(summary, dict)
    assert any(
        isinstance(member, dict) and member.get("object_id") == evidence.object_id
        for member in summary["members"]
    )


def test_unconfigured_current_row_is_excluded_without_skipping_configured_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch)
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    config = load_config(fixture.config_path)
    writer = connect(config.database_path)
    try:
        run_id = str(
            writer.execute(
                "SELECT id FROM sync_runs WHERE app_id='sample' AND source='git' "
                "ORDER BY id LIMIT 1"
            ).fetchone()[0]
        )
        writer.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES (
                'obj:outside-scope', 'sample', 'git', 'unconfigured-repository', 'git_commit',
                'outside', ?, ?
            )
            """,
            (run_id, run_id),
        )
        writer.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, body_text, data_json, completeness,
                adapter_version, normalization_version, redaction_version
            ) VALUES (
                'obs:outside-scope', 'obj:outside-scope', ?, '2026-04-01T12:00:00+00:00',
                '2026-04-01T12:00:00+00:00', 'outside-hash',
                'DEMO-1 context outside configured repository', 'outside', '{}', 'complete',
                'fixture', '1', '1'
            )
            """,
            (run_id,),
        )
        writer.commit()
    finally:
        writer.close()

    page = ReadOnlyWorkspace(config).search_evidence(
        "sample", normalize_evidence_search_filters("DEMO-1 context")
    )
    assert any(item.source == "git" for item in page.items)
    assert all(item.evidence_id != "obs:outside-scope" for item in page.items)


def test_revision_change_invalidates_a_previously_valid_tui_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = PublicWorkflowFixture.create(tmp_path, monkeypatch)
    assert fixture.invoke("init").exit_code == 0
    assert fixture.invoke("import", "all", "sample").exit_code == 0
    config = load_config(fixture.config_path)
    workspace = ReadOnlyWorkspace(config)
    filters = normalize_evidence_search_filters("DEMO-1 context")
    first = workspace.search_evidence("sample", filters)
    cursor = EvidenceSearchCursor(
        app_id="sample",
        filters=filters,
        revision=first.revision,
        read_model_version=first.read_model_version,
        after_sort_time="2026-01-01T00:00:00Z",
        after_observation_id="obs:cursor-boundary",
    )
    writer = connect(config.database_path)
    try:
        writer.execute("UPDATE apps SET read_revision=read_revision+1 WHERE id='sample'")
        writer.commit()
    finally:
        writer.close()

    with pytest.raises(EvidenceSearchInvalidated, match="restart"):
        workspace.search_evidence("sample", filters, cursor=cursor)
    with pytest.raises(EvidenceSearchInvalidated, match="restart"):
        workspace.search_evidence("sample", filters, expected_revision=first.revision)


def test_canonical_review_opens_confirmed_history_after_generated_candidate_disappears(
    tmp_path: Path,
) -> None:
    connection, _, config, candidate_id = _packet_state(tmp_path)
    append_decision(
        connection,
        "confirm_candidate",
        candidate_id,
        {
            "app_id": "sample_store",
            "contribution_id": "contribution:rowless-history",
            "title": "Surviving confirmed history",
            "members": ["obj:commit"],
        },
    )
    connection.execute("DELETE FROM candidate_members WHERE candidate_id=?", (candidate_id,))
    connection.execute("DELETE FROM candidate_groups WHERE id=?", (candidate_id,))
    connection.commit()
    connection.close()

    review = ReadOnlyWorkspace(config).contribution_review(
        "sample_store", "contribution:rowless-history"
    )
    assert review.status == "confirmed"
    assert review.resolved_contribution_id == "contribution:rowless-history"
    contribution = review.packet["contribution"]
    assert isinstance(contribution, dict)
    assert contribution["id"] == "contribution:rowless-history"


def test_page_wide_projection_budget_counts_failures_and_round_robins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-layer budget fixture: locators are synthetic, not discovery evidence."""
    first = tuple(_locator(f"first-{index:02d}") for index in range(100))
    second = (_locator("second"),)
    calls: list[tuple[str, str]] = []

    def resolve(
        _builder: object,
        _app_id: str,
        object_id: str,
        locator: CanonicalMembershipLocator,
        *,
        legacy_generated: bool,
    ) -> CandidateLink | None:
        assert legacy_generated is False
        calls.append((object_id, locator.key))
        return None

    links, complete, limits, attempts = _enrich_with(
        monkeypatch,
        {"obj:first": first, "obj:second": second},
        resolve,
    )

    assert attempts == 50
    assert calls[:4] == [
        ("obj:first", "first-00"),
        ("obj:second", "second"),
        ("obj:first", "first-01"),
        ("obj:first", "first-02"),
    ]
    assert all(not values for values in links)
    assert complete == [False, True]
    assert limits == ["projection_budget", None]


def test_cached_canonical_group_is_reused_after_projection_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locator_map = {
        "obj:cached": (_locator("shared"),),
        "obj:target": (
            *(_locator(f"unique-{index:02d}") for index in range(49)),
            _locator("uncached-after-budget"),
            _locator("shared"),
        ),
    }
    calls: list[str] = []

    def resolve(
        _builder: object,
        _app_id: str,
        _object_id: str,
        locator: CanonicalMembershipLocator,
        *,
        legacy_generated: bool,
    ) -> CandidateLink | None:
        assert legacy_generated is False
        calls.append(locator.key)
        return None if locator.key.startswith("unique-") else _link(locator)

    links, complete, limits, attempts = _enrich_with(monkeypatch, locator_map, resolve)

    assert attempts == 50
    assert calls == ["shared", *[f"unique-{index:02d}" for index in range(49)]]
    assert links[1] == (_link(_locator("shared")),)
    assert complete[1] is False
    assert limits[1] == "projection_budget"


def test_link_display_cap_and_incomplete_empty_coverage_are_truthful(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    display_locators = tuple(_locator(f"display-{index}") for index in range(6))

    def resolve_link(
        _builder: object,
        _app_id: str,
        _object_id: str,
        locator: CanonicalMembershipLocator,
        *,
        legacy_generated: bool,
    ) -> CandidateLink:
        assert legacy_generated is False
        return _link(locator)

    links, complete, limits, attempts = _enrich_with(
        monkeypatch, {"obj:display": display_locators}, resolve_link
    )
    assert attempts == 5
    assert len(links[0]) == 5
    assert complete == [False]
    assert limits == ["display_cap"]

    failed = tuple(_locator(f"failed-{index:02d}") for index in range(51))
    links, complete, limits, attempts = _enrich_with(
        monkeypatch,
        {"obj:failed": failed},
        lambda *_args, **_kwargs: None,
    )
    assert attempts == 50
    assert links == [()]
    assert complete == [False]
    assert limits == ["projection_budget"]


def test_scanner_bounds_continue_across_empty_pages_without_skipping_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unit-layer scanner fixture: positions model the existing bounded scanner stream."""
    from tests.test_packets_golden import _packet_state

    connection, _, config, _ = _packet_state(tmp_path)
    filters = normalize_evidence_search_filters("literal")
    rows = [
        (
            {"sort_time": f"2026-01-01T00:00:{index:03d}Z", "observation_id": f"obs:{index:03d}"},
            (
                None
                if index < 200
                else {
                    "evidence_id": f"obs:{index:03d}",
                    "object_id": f"obj:{index:03d}",
                    "source": "git",
                    "kind": "git_commit",
                    "title": f"literal {index:03d}",
                    "date_from": "2026-01-01",
                    "date_to": "2026-01-01",
                    "period_status": "known",
                }
            ),
        )
        for index in range(221)
    ]

    def scan(
        _builder: object,
        _query: str,
        _app_id: str,
        *,
        after: tuple[str, str] | None,
        **_kwargs: object,
    ) -> Any:
        start = 0
        if after is not None:
            start = next(
                index + 1
                for index, (position, _) in enumerate(rows)
                if position == {"sort_time": after[0], "observation_id": after[1]}
            )
        stop = min(start + 200, len(rows))
        has_more = stop < len(rows)
        return iter(
            (position, projected, index < stop - 1 or has_more)
            for index, (position, projected) in enumerate(rows[start:stop], start=start)
        )

    monkeypatch.setattr(evidence_search_module, "scan_evidence", scan)
    monkeypatch.setattr(
        evidence_search_module,
        "_is_admitted_search_object",
        lambda _builder, _app_id, _object_id: True,
    )
    monkeypatch.setattr(
        evidence_search_module,
        "_enrich",
        lambda _builder, _app_id, raw: ([()] * len(raw), [True] * len(raw), [None] * len(raw), 0),
    )
    try:
        first = evidence_search_page(
            connection, PacketBuilder(connection, config), "sample_store", filters, cursor=None
        )
        assert first.items == ()
        assert first.diagnostics.scanned_rows == 200
        assert first.next_cursor is not None
        assert first.next_cursor.after_observation_id == "obs:199"

        second = evidence_search_page(
            connection,
            PacketBuilder(connection, config),
            "sample_store",
            filters,
            cursor=first.next_cursor,
        )
        assert second.next_cursor is not None
        third = evidence_search_page(
            connection,
            PacketBuilder(connection, config),
            "sample_store",
            filters,
            cursor=second.next_cursor,
        )
    finally:
        connection.close()

    assert second.diagnostics.scanned_rows == 20
    assert [item.evidence_id for item in second.items] == [
        f"obs:{index:03d}" for index in range(200, 220)
    ]
    assert [item.evidence_id for item in third.items] == ["obs:220"]
    assert third.next_cursor is None
