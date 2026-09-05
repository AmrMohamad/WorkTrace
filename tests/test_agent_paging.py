from __future__ import annotations

from pathlib import Path

from tests.test_candidate_paging import _APP_ID, _build_scale_database, _config
from worktrace.db.connection import connect_read_only
from worktrace.packets.builder import PacketBuilder
from worktrace.read_models.agent_pages import scan_candidates, scan_evidence


def test_agent_scans_are_lazy_bounded_and_keep_stable_raw_positions(tmp_path: Path) -> None:
    database_path = tmp_path / "agent-pages.sqlite3"
    _build_scale_database(database_path)
    connection = connect_read_only(database_path)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    try:
        builder = PacketBuilder(connection, _config(tmp_path))
        generation, candidates = scan_candidates(builder, _APP_ID)
        assert generation is not None
        candidate_diagnostics = candidates.diagnostics  # type: ignore[attr-defined]
        assert candidate_diagnostics.raw_scans == 0

        first_position, first_item, first_has_more = next(candidates)
        assert first_position == {"candidate_id": "candidate:scale-0000"}
        assert first_item is None  # The deterministic first fixture row is ignored.
        assert first_has_more is True
        assert candidate_diagnostics.raw_scans == 1
        assert candidate_diagnostics.projections == 1
        assert candidate_diagnostics.authority_rows == 3_000
        assert candidate_diagnostics.decision_rows == 1_100
        assert candidate_diagnostics.hydrated_body_bytes == 0
        authority_statement = next(
            statement
            for statement in statements
            if "worktrace_page_authority_evidence_context" in statement
        )
        assert "NULL AS body_text" in authority_statement
        assert "SELECT current.*" not in authority_statement

        last = first_position
        for _ in range(199):
            last, item, has_more = next(candidates)
            assert item is None
            assert has_more is True
        assert last == {"candidate_id": "candidate:scale-0199"}
        assert candidate_diagnostics.raw_scans == 200

        evidence = scan_evidence(
            builder,
            "Synthetic body",
            _APP_ID,
            source_types=(),
            actor_id=None,
            module=None,
            date_from=None,
            date_to=None,
        )
        evidence_diagnostics = evidence.diagnostics  # type: ignore[attr-defined]
        evidence_position, evidence_item, evidence_has_more = next(evidence)
        assert evidence_position == {
            "sort_time": "2026-08-30T11:00:00+00:00",
            "observation_id": "obs:scale-0000",
        }
        assert evidence_item is not None
        assert evidence_item["text"] == "Synthetic body"
        assert evidence_has_more is True
        assert evidence_diagnostics.raw_scans == 1
        assert evidence_diagnostics.projections == 1
        assert evidence_diagnostics.authority_rows == 3_000
        assert evidence_diagnostics.hydrated_body_bytes == len("Synthetic body")
    finally:
        connection.close()


def test_agent_scans_expose_date_exclusions_without_advancing_past_them(tmp_path: Path) -> None:
    database_path = tmp_path / "agent-pages-dates.sqlite3"
    _build_scale_database(database_path)
    connection = connect_read_only(database_path)
    try:
        builder = PacketBuilder(connection, _config(tmp_path))
        _, candidates = scan_candidates(
            builder,
            _APP_ID,
            date_from="2026-08-30",
            date_to="2026-08-30",
        )
        candidate_rows = list(candidates)
        assert [position["candidate_id"] for position, _, _ in candidate_rows] == [
            f"candidate:scale-{index:04d}" for index in range(200)
        ]
        assert all(item is None for _, item, _ in candidate_rows)
        assert candidate_rows[-1][2] is True

        evidence_rows = list(
            scan_evidence(
                builder,
                "Synthetic body",
                _APP_ID,
                source_types=(),
                actor_id=None,
                module=None,
                date_from="2026-08-30",
                date_to="2026-08-30",
            )
        )
        assert [position["observation_id"] for position, _, _ in evidence_rows] == [
            f"obs:scale-{index:04d}" for index in range(200)
        ]
        assert all(item is None for _, item, _ in evidence_rows)
        assert evidence_rows[-1][2] is True
    finally:
        connection.close()
