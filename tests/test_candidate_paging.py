from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
from datetime import date
from pathlib import Path
from types import MappingProxyType

import pytest

from worktrace.candidates.decisions import (
    _build_decision_projection_context,
    append_decision,
    undo_decision,
)
from worktrace.candidates.projector import project_candidate
from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect, connect_read_only
from worktrace.db.migrations import migrate
from worktrace.errors import ScopeViolation
from worktrace.identity import identity_fingerprint
from worktrace.packets.builder import PacketBuilder, _build_authority_evidence_context
from worktrace.read_models.candidates import (
    _ACTIVE_GENERATION_SQL,
    _NEWER_GENERATION_SQL,
    _OLDER_GENERATION_SQL,
    _RAW_CANDIDATE_SQL,
    CandidateCursor,
    CandidateGenerationChanged,
    CandidateGenerationInconsistent,
    CandidateListItem,
    _candidate_page,
    _PageDiagnostics,
    candidate_page,
)

_APP_ID = "sample_store"
_GENERATED_AT = "2026-08-30T12:00:00+00:00"
_GENERATOR_VERSION = "fixture-v1"
_SOURCES = (
    ("manual", "local", "manual_evidence", {}),
    ("git", "repo", "git_commit", {}),
    ("jira", "jira.example", "jira_issue", {"selection_policy_version": 2}),
    ("gitlab", "gitlab.example", "gitlab_mr", {"selection_policy_version": 2}),
)


def _config(data_directory: Path) -> WorkTraceConfig:
    return WorkTraceConfig(
        schema_version=1,
        data_directory=data_directory,
        employment_from=date(2020, 1, 1),
        employment_to=date(2026, 12, 31),
        identity=IdentityConfig(
            display_name="Fixture Engineer",
            git_author_emails=(),
            git_author_names=("Fixture Engineer",),
            jira_account_id="fixture-self",
            gitlab_user_id=7,
            gitlab_username="fixture-engineer",
        ),
        apps=(
            AppConfig(
                id=_APP_ID,
                name="Sample Store",
                market="XX",
                business_type="fixture",
                jira_project_keys=("DEMO",),
                gitlab_project_ids=(101,),
                repo_paths=(data_directory / "repo",),
                jira_key_patterns=(r"DEMO-[0-9]+",),
                production_environments=("production",),
                release_tag_patterns=(r"v[0-9]+.*",),
                ignored_paths=(),
            ),
            AppConfig(
                id="other_app",
                name="Other App",
                market="YY",
                business_type="fixture",
                jira_project_keys=(),
                gitlab_project_ids=(),
                repo_paths=(data_directory / "other-repo",),
                jira_key_patterns=(),
                production_environments=(),
                release_tag_patterns=(),
                ignored_paths=(),
            ),
        ),
        config_path=data_directory / "config.toml",
    )


def _initialize(database_path: Path) -> sqlite3.Connection:
    connection = connect(database_path)
    migrate(connection, database_path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, ?, ?)",
        (_APP_ID, "Sample Store", "XX", "fixture"),
    )
    connection.execute(
        "INSERT INTO app_identity_policy(app_id, version, fingerprint) VALUES (?, 1, ?)",
        (_APP_ID, identity_fingerprint(_config(database_path.parent))),
    )
    return connection


def _build_scale_database(database_path: Path, *, candidate_count: int = 3_000) -> None:
    connection = _initialize(database_path)
    try:
        run_rows: list[tuple[object, ...]] = []
        for source, source_instance, _, scope in _SOURCES:
            run_rows.extend(
                (
                    (
                        f"run:{source}:old",
                        source,
                        source_instance,
                        "2026-08-29T12:00:00+00:00",
                        json.dumps(scope),
                    ),
                    (
                        f"run:{source}:current",
                        source,
                        source_instance,
                        "2026-08-30T12:00:00+00:00",
                        json.dumps(scope),
                    ),
                )
            )
        connection.executemany(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES (?, 'sample_store', ?, ?, 'complete', ?, ?, 'fixture', ?,
                      'complete_for_scope')
            """,
            [(*row[:3], row[3], row[3], row[4]) for row in run_rows],
        )
        connection.execute(
            "INSERT INTO apps(id, name, market, business_type) "
            "VALUES ('other_app', 'Other App', 'YY', 'fixture')"
        )
        connection.execute(
            """
            INSERT INTO sync_runs(
                id, app_id, source, source_instance, status, started_at, completed_at,
                adapter_version, scope_json, completeness
            ) VALUES ('run:other:current', 'other_app', 'manual', 'other-local', 'complete',
                      '2026-08-30T12:00:00+00:00', '2026-08-30T12:00:00+00:00',
                      'fixture', '{}', 'complete_for_scope')
            """
        )

        object_rows: list[tuple[object, ...]] = []
        candidate_rows: list[tuple[object, ...]] = []
        member_rows: list[tuple[str, str]] = []
        for index in range(candidate_count):
            source, source_instance, kind, _ = _SOURCES[index % len(_SOURCES)]
            object_id = f"obj:scale-{index:04d}"
            candidate_id = f"candidate:scale-{index:04d}"
            object_rows.append(
                (
                    object_id,
                    source,
                    source_instance,
                    kind,
                    f"external-{index:04d}",
                    f"run:{source}:old",
                    f"run:{source}:current",
                )
            )
            candidate_rows.append(
                (
                    candidate_id,
                    object_id,
                    _GENERATOR_VERSION,
                    f"Candidate {index:04d}",
                    "feature",
                    _GENERATED_AT,
                )
            )
            member_rows.append((candidate_id, object_id))
        for index in range(1_000, 1_050):
            source, source_instance, kind, _ = _SOURCES[index % len(_SOURCES)]
            object_rows.append(
                (
                    f"obj:unsupported-{index:04d}",
                    source,
                    source_instance,
                    kind,
                    f"unsupported-{index:04d}",
                    f"run:{source}:old",
                    f"run:{source}:old",
                )
            )
        connection.executemany(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES (?, 'sample_store', ?, ?, ?, ?, ?, ?)
            """,
            object_rows,
        )
        connection.executemany(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES (?, 'sample_store', ?, ?, ?, ?, ?)
            """,
            candidate_rows,
        )
        connection.executemany(
            """
            INSERT INTO candidate_members(
                candidate_id, source_object_id, membership_reason, context_only
            ) VALUES (?, ?, 'fixture', 0)
            """,
            member_rows,
        )
        connection.execute(
            """
            INSERT INTO source_objects(
                id, app_id, source, source_instance, kind, external_id,
                first_seen_run_id, last_seen_run_id
            ) VALUES ('obj:other', 'other_app', 'manual', 'other-local',
                      'manual_evidence', 'other', 'run:other:current', 'run:other:current')
            """
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, body_text, data_json, completeness,
                adapter_version, normalization_version, redaction_version
            ) VALUES ('obs:other', 'obj:other', 'run:other:current',
                      '2026-08-30T11:00:00+00:00', '2026-08-30T12:00:00+00:00',
                      'hash-other', 'Other title', 'Other body', '{}', 'complete',
                      'fixture', '1', '1')
            """
        )
        connection.execute(
            """
            INSERT INTO candidate_groups(
                id, app_id, seed_object_id, generator_version, suggested_title,
                suggested_type, generated_at
            ) VALUES ('candidate:other', 'other_app', 'obj:other', 'fixture-v1',
                      'Other title', 'feature', ?)
            """,
            (_GENERATED_AT,),
        )
        connection.execute(
            "INSERT INTO candidate_members VALUES ('candidate:other', 'obj:other', 'fixture', 0)"
        )

        observation_rows: list[tuple[object, ...]] = []
        actor_rows: list[tuple[object, ...]] = []
        participation_rows: list[tuple[object, ...]] = []
        for source, source_instance, _, _ in _SOURCES:
            actor_rows.append(
                (
                    f"actor:self:{source}",
                    source,
                    source_instance,
                    f"self-{source}",
                    "Fixture Engineer",
                )
            )
        for index in range(candidate_count):
            source, _, _, _ = _SOURCES[index % len(_SOURCES)]
            object_id = f"obj:scale-{index:04d}"
            observation_id = f"obs:scale-{index:04d}"
            observation_rows.append(
                (
                    observation_id,
                    object_id,
                    f"run:{source}:current",
                    f"hash-{index:04d}",
                    f"Candidate {index:04d}",
                )
            )
            participation_rows.append(
                (
                    f"participation:scale-{index:04d}",
                    object_id,
                    observation_id,
                    f"actor:self:{source}",
                    "authored" if index % 2 == 0 else "reviewed",
                )
            )
        connection.executemany(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, body_text, data_json, completeness,
                adapter_version, normalization_version, redaction_version
            ) VALUES (?, ?, ?, '2026-08-30T11:00:00+00:00',
                      '2026-08-30T12:00:00+00:00', ?, ?, 'Synthetic body', '{}',
                      'complete', 'fixture', '1', '1')
            """,
            observation_rows,
        )
        connection.executemany(
            """
            INSERT INTO actors(
                id, source, source_instance, external_actor_id, display_name, is_self,
                identity_policy_version
            ) VALUES (?, ?, ?, ?, ?, 1, 1)
            """,
            actor_rows,
        )
        connection.executemany(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, details_json
            ) VALUES (?, ?, ?, ?, ?, '{}')
            """,
            participation_rows,
        )
        connection.execute(
            """
            INSERT INTO observations(
                id, source_object_id, sync_run_id, source_updated_at, fetched_at,
                payload_hash, title, body_text, data_json, completeness,
                adapter_version, normalization_version, redaction_version
            ) VALUES ('obs:scale-2950-old', 'obj:scale-2950', 'run:jira:old',
                      '2026-08-29T11:00:00+00:00', '2026-08-29T12:00:00+00:00',
                      'hash-2950-old', 'Stale title', 'Stale body', '{}', 'complete',
                      'fixture', '1', '1')
            """
        )
        connection.execute(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, details_json
            ) VALUES ('participation:scale-2950-old', 'obj:scale-2950',
                      'obs:scale-2950-old', 'actor:self:jira', 'assigned', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO participations(
                id, source_object_id, observation_id, actor_id, role, details_json
            ) VALUES ('participation:scale-2950-mismatch', 'obj:scale-2950',
                      'obs:scale-2950', 'actor:self:git', 'reviewed', '{}')
            """
        )
        connection.execute(
            """
            UPDATE source_objects
            SET availability='unavailable', availability_reason='fixture_unavailable',
                availability_observed_at='2026-08-30T12:00:00+00:00'
            WHERE id='obj:scale-2950'
            """
        )
        connection.execute(
            """
            INSERT INTO source_object_availability_events(
                id, source_object_id, sync_run_id, state, reason, observed_at
            ) VALUES ('availability:scale-2950', 'obj:scale-2950', 'run:jira:current',
                      'unavailable', 'fixture_unavailable',
                      '2026-08-30T12:00:00+00:00')
            """
        )

        ignored = [
            (
                f"decision:ignore-{index:04d}",
                f"candidate:scale-{index:04d}",
                f"2026-08-30T13:{index // 60:02d}:{index % 60:02d}+00:00",
            )
            for index in range(1_000)
        ]
        connection.executemany(
            """
            INSERT INTO human_decisions(
                id, action, target_id, payload_json, actor_label, created_at
            ) VALUES (?, 'ignore_candidate', ?, '{}', 'local-user', ?)
            """,
            ignored,
        )
        confirmed = [
            (
                f"decision:confirm-{index:04d}",
                f"candidate:scale-{index:04d}",
                json.dumps(
                    {
                        "app_id": _APP_ID,
                        "contribution_id": f"contribution:scale-{index:04d}",
                        "members": [
                            f"obj:scale-{index:04d}",
                            *([f"obj:unsupported-{index:04d}"] if index < 1_050 else []),
                        ],
                        "title": f"Confirmed {index:04d}",
                    },
                    separators=(",", ":"),
                ),
                f"2026-08-30T14:{(index - 1_000) // 60:02d}:{index % 60:02d}+00:00",
            )
            for index in range(1_000, 1_100)
        ]
        connection.executemany(
            """
            INSERT INTO human_decisions(
                id, action, target_id, payload_json, actor_label, created_at
            ) VALUES (?, 'confirm_candidate', ?, ?, 'local-user', ?)
            """,
            confirmed,
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture(scope="module")
def scale_state(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, WorkTraceConfig]:
    directory = tmp_path_factory.mktemp("candidate-scale")
    database_path = directory / "worktrace.sqlite3"
    _build_scale_database(database_path)
    return database_path, _config(directory)


def _copy_database(source: Path, destination: Path) -> Path:
    shutil.copy2(source, destination)
    return destination


def test_default_page_is_scan_bounded_and_advances_by_the_last_raw_row(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, config = scale_state
    connection = connect_read_only(database_path)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)
    diagnostics = _PageDiagnostics()
    try:
        page = _candidate_page(
            connection,
            config,
            _APP_ID,
            diagnostics=diagnostics,
        )
    finally:
        connection.close()

    assert page.items == ()
    assert diagnostics.raw_scan_count == 100
    assert diagnostics.projection_count == 100
    assert diagnostics.batch_limits == [50, 50]
    assert diagnostics.decision_context_builds == 1
    assert diagnostics.active_decision_count == 1_100
    assert diagnostics.decision_scope_count == 1_100
    assert diagnostics.decision_lineage_count == 100
    assert diagnostics.authority_context_builds == 1
    assert sum(statement.strip() == "BEGIN" for statement in statements) == 1
    assert sum(statement.strip() == "COMMIT" for statement in statements) == 1
    assert sum("worktrace_page_authority_evidence_context" in value for value in statements) == 1
    assert sum("authoritative_current_observations AS" in value for value in statements) == 1
    assert sum("authoritative_current_availability_events AS" in value for value in statements) == 1
    assert sum("worktrace_page_participation_context" in value for value in statements) == 1
    assert sum("authoritative_current_participations AS" in value for value in statements) == 0
    assert sum("WHERE current.source_object_id=" in value for value in statements) == 0
    assert all("COUNT(" not in statement.upper() for statement in statements)
    expected_token = hashlib.sha256(
        json.dumps(
            [_APP_ID, _GENERATED_AT, _GENERATOR_VERSION],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert page.generation_token == expected_token
    assert page.next_cursor is not None
    assert page.next_cursor.generation_token == expected_token
    assert page.next_cursor.after_candidate_id == diagnostics.scanned_candidate_ids[-1]
    assert page.next_cursor.after_candidate_id == "candidate:scale-0099"


def test_scale_fixture_contains_the_intended_decision_and_authority_mix(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, _ = scale_state
    connection = connect_read_only(database_path)
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM candidate_groups WHERE app_id='sample_store'"
            ).fetchone()[0]
            == 3_000
        )
        authority_context = _build_authority_evidence_context(connection, _APP_ID)
        assert len(authority_context.current_observations) == 3_000
        assert (
            sum(len(rows) for rows in authority_context.participation_rows_by_observation.values())
            == 3_000
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM human_decisions WHERE action='ignore_candidate'"
            ).fetchone()[0]
            == 1_000
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM human_decisions WHERE action='confirm_candidate'"
            ).fetchone()[0]
            == 100
        )
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT source FROM source_objects WHERE id >= 'obj:scale-2950'"
            )
        } == {"manual", "git", "jira", "gitlab"}
        unsupported = project_candidate(connection, "candidate:scale-1000")
    finally:
        connection.close()

    assert unsupported.status == "confirmed"
    assert tuple(member["source_object_id"] for member in unsupported.members) == (
        "obj:scale-1000",
    )
    assert unsupported.unsupported_member_ids == ("obj:unsupported-1000",)


def test_maximum_page_never_scans_or_projects_more_than_two_hundred_rows(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, config = scale_state
    connection = connect_read_only(database_path)
    diagnostics = _PageDiagnostics()
    try:
        page = _candidate_page(
            connection,
            config,
            _APP_ID,
            page_size=50,
            diagnostics=diagnostics,
        )
        with pytest.raises(ValueError, match="between 1 and 50"):
            candidate_page(connection, config, _APP_ID, page_size=51)
    finally:
        connection.close()

    assert page.items == ()
    assert diagnostics.raw_scan_count == 200
    assert diagnostics.projection_count == 200
    assert diagnostics.batch_limits == [100, 100]


def test_short_pages_continue_without_duplicate_or_omitted_raw_candidates(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, config = scale_state
    connection = connect_read_only(database_path)
    cursor: CandidateCursor | None = None
    scanned_ids: list[str] = []
    visible_ids: list[str] = []
    try:
        while True:
            diagnostics = _PageDiagnostics()
            page = _candidate_page(
                connection,
                config,
                _APP_ID,
                cursor=cursor,
                diagnostics=diagnostics,
            )
            scanned_ids.extend(diagnostics.scanned_candidate_ids)
            visible_ids.extend(item.candidate_id for item in page.items)
            if page.next_cursor is None:
                break
            assert page.next_cursor.after_candidate_id == diagnostics.scanned_candidate_ids[-1]
            cursor = page.next_cursor
    finally:
        connection.close()

    assert scanned_ids == [f"candidate:scale-{index:04d}" for index in range(3_000)]
    assert len(scanned_ids) == len(set(scanned_ids))
    assert visible_ids == [f"candidate:scale-{index:04d}" for index in range(1_000, 3_000)]


def test_visible_items_keep_canonical_title_source_and_participation_projection(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, config = scale_state
    connection = connect_read_only(database_path)
    cursor: CandidateCursor | None = None
    page = None
    try:
        while page is None or not page.items:
            page = candidate_page(connection, config, _APP_ID, cursor=cursor)
            cursor = page.next_cursor
            assert cursor is not None or page.items
    finally:
        connection.close()

    item = page.items[0]
    assert item.title == "Confirmed 1000"
    assert item.title_authority == "human_decision"
    assert item.title_supporting_evidence_ids == ("decision:confirm-1000",)
    assert item.source_coverage == ("manual",)
    assert item.participation_indicators == ("authored",)
    assert item.confirmed_contribution_id == "contribution:scale-1000"


def test_page_builds_the_global_decision_projection_once_not_once_per_row(
    tmp_path: Path,
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    source, config = scale_state
    high_decision_path = _copy_database(source, tmp_path / "high-decisions.sqlite3")
    low_decision_path = _copy_database(source, tmp_path / "low-decisions.sqlite3")
    writer = connect(low_decision_path)
    try:
        writer.execute("DELETE FROM human_decisions")
        writer.commit()
    finally:
        writer.close()

    token = hashlib.sha256(
        json.dumps(
            [_APP_ID, _GENERATED_AT, _GENERATOR_VERSION],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cursor = CandidateCursor(token, "candidate:scale-2949")

    def trace_page(database_path: Path) -> list[str]:
        statements: list[str] = []
        connection = connect_read_only(database_path)
        connection.set_trace_callback(statements.append)
        try:
            page = candidate_page(connection, config, _APP_ID, cursor=cursor)
            assert len(page.items) == 25
        finally:
            connection.close()
        return statements

    low_statements = trace_page(low_decision_path)
    high_statements = trace_page(high_decision_path)
    stream_sql = "SELECT * FROM human_decisions ORDER BY created_at, id"
    assert sum(stream_sql in statement for statement in low_statements) == 1
    assert sum(stream_sql in statement for statement in high_statements) == 1

    decision_count = 1_100
    assert len(high_statements) <= len(low_statements) + (2 * decision_count)


def test_page_builds_authority_evidence_context_once_for_low_and_dense_corpora(
    tmp_path: Path,
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    source, config = scale_state
    dense_path = _copy_database(source, tmp_path / "dense-evidence.sqlite3")
    low_path = _copy_database(source, tmp_path / "low-evidence.sqlite3")
    retained_observations = [f"obs:scale-{index:04d}" for index in range(2_950, 2_975)]
    placeholders = ",".join("?" for _ in retained_observations)
    writer = connect(low_path)
    try:
        writer.execute(
            f"DELETE FROM participations WHERE observation_id NOT IN ({placeholders})",
            retained_observations,
        )
        writer.execute(
            f"DELETE FROM observations WHERE id NOT IN ({placeholders})",
            retained_observations,
        )
        writer.commit()
    finally:
        writer.close()

    token = hashlib.sha256(
        json.dumps(
            [_APP_ID, _GENERATED_AT, _GENERATOR_VERSION],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cursor = CandidateCursor(token, "candidate:scale-2949")

    def trace_page(
        database_path: Path,
    ) -> tuple[list[str], _PageDiagnostics, tuple[CandidateListItem, ...]]:
        statements: list[str] = []
        diagnostics = _PageDiagnostics()
        connection = connect_read_only(database_path)
        connection.set_trace_callback(statements.append)
        try:
            page = _candidate_page(
                connection,
                config,
                _APP_ID,
                cursor=cursor,
                diagnostics=diagnostics,
            )
        finally:
            connection.close()
        return statements, diagnostics, page.items

    low_statements, low_diagnostics, low_items = trace_page(low_path)
    dense_statements, dense_diagnostics, dense_items = trace_page(dense_path)
    combined_authority_context = "worktrace_page_authority_evidence_context"
    participation_context = "worktrace_page_participation_context"

    assert sum(combined_authority_context in value for value in low_statements) == 1
    assert sum(combined_authority_context in value for value in dense_statements) == 1
    assert sum(participation_context in value for value in low_statements) == 1
    assert sum(participation_context in value for value in dense_statements) == 1
    assert low_diagnostics.authority_context_builds == 1
    assert dense_diagnostics.authority_context_builds == 1
    assert low_diagnostics.authority_observation_count == 25
    assert dense_diagnostics.authority_observation_count == 3_000
    assert low_diagnostics.authority_participation_count == 25
    assert dense_diagnostics.authority_participation_count == 3_000
    assert dense_items == low_items


def test_page_contexts_preserve_canonical_candidate_and_evidence_output(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, config = scale_state
    connection = connect_read_only(database_path)
    try:
        decision_context = _build_decision_projection_context(connection)
        authority_context = _build_authority_evidence_context(connection, _APP_ID)
        assert isinstance(decision_context.decisions_by_target, MappingProxyType)
        assert isinstance(decision_context.decision_scopes, MappingProxyType)
        assert isinstance(decision_context.lineages_by_identifier, MappingProxyType)
        assert isinstance(authority_context.current_observations, MappingProxyType)
        assert isinstance(authority_context.evidence_records, MappingProxyType)
        assert isinstance(authority_context.participation_rows_by_observation, MappingProxyType)
        regular = project_candidate(connection, "candidate:scale-2950")
        contextual = project_candidate(
            connection,
            "candidate:scale-2950",
            decision_context=decision_context,
            current_observations=authority_context.current_observations,
        )
        regular_confirmed = project_candidate(connection, "candidate:scale-1000")
        contextual_confirmed = project_candidate(
            connection,
            "candidate:scale-1000",
            decision_context=decision_context,
            current_observations=authority_context.current_observations,
        )
        regular_ignored = project_candidate(connection, "candidate:scale-0000")
        contextual_ignored = project_candidate(
            connection,
            "candidate:scale-0000",
            decision_context=decision_context,
            current_observations=authority_context.current_observations,
        )
        regular_item = PacketBuilder(connection, config).candidate_list_item(
            _APP_ID,
            "candidate:scale-2950",
        )
        contextual_item = PacketBuilder(
            connection,
            config,
            decision_context=decision_context,
            authority_context=authority_context,
        ).candidate_list_item(
            _APP_ID,
            "candidate:scale-2950",
        )
        regular_confirmed_item = PacketBuilder(connection, config).candidate_list_item(
            _APP_ID,
            "candidate:scale-1000",
        )
        contextual_confirmed_item = PacketBuilder(
            connection,
            config,
            decision_context=decision_context,
            authority_context=authority_context,
        ).candidate_list_item(
            _APP_ID,
            "candidate:scale-1000",
        )
        regular_record = PacketBuilder(connection, config)._record_for_object(
            "obj:scale-2950",
            context_only=False,
        )
        contextual_builder = PacketBuilder(
            connection,
            config,
            authority_context=authority_context,
        )
        contextual_record = contextual_builder._record_for_object(
            "obj:scale-2950",
            context_only=False,
        )
        contextual_context_only = contextual_builder._record_for_object(
            "obj:scale-2950",
            context_only=True,
        )
    finally:
        connection.close()

    assert contextual == regular
    assert contextual_confirmed == regular_confirmed
    assert contextual_ignored == regular_ignored
    assert contextual_ignored.status == "ignored"
    assert contextual_confirmed.unsupported_member_ids == ("obj:unsupported-1000",)
    assert contextual_item == regular_item
    assert json.dumps(contextual_item, separators=(",", ":")) == json.dumps(
        regular_item,
        separators=(",", ":"),
    )
    assert contextual_item["participation_indicators"] == ["authored"]
    assert contextual_confirmed_item == regular_confirmed_item
    assert contextual_record == regular_record
    assert contextual_record is not None
    assert contextual_record.availability == "unavailable"
    assert contextual_record.availability_evidence_id == "availability:scale-2950"
    assert contextual_record.availability_reason == "fixture_unavailable"
    assert contextual_record.context_only is False
    assert contextual_context_only is not None
    assert contextual_context_only.context_only is True
    assert authority_context.evidence_records["obj:scale-2950"].context_only is False


def test_authority_context_is_application_scoped_without_cross_app_projection_drift(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, config = scale_state
    connection = connect_read_only(database_path)
    try:
        sample_context = _build_authority_evidence_context(connection, _APP_ID)
        other_context = _build_authority_evidence_context(connection, "other_app")
        regular = PacketBuilder(connection, config).candidate_list_item(
            "other_app",
            "candidate:other",
        )
        contextual = PacketBuilder(
            connection,
            config,
            authority_context=other_context,
        ).candidate_list_item(
            "other_app",
            "candidate:other",
        )
        with pytest.raises(ScopeViolation, match="another application"):
            PacketBuilder(
                connection,
                config,
                authority_context=sample_context,
            ).candidate_list_item(
                "other_app",
                "candidate:other",
            )
    finally:
        connection.close()

    assert "obj:other" not in sample_context.current_observations
    assert set(other_context.current_observations) == {"obj:other"}
    assert contextual == regular


def test_empty_generation_has_no_token_or_cursor(tmp_path: Path) -> None:
    database_path = tmp_path / "empty.sqlite3"
    connection = _initialize(database_path)
    connection.commit()
    connection.close()

    read_only = connect_read_only(database_path)
    try:
        page = candidate_page(read_only, _config(tmp_path), _APP_ID)
        assert page.generation_token is None
        assert page.items == ()
        assert page.next_cursor is None
        with pytest.raises(CandidateGenerationChanged):
            candidate_page(
                read_only,
                _config(tmp_path),
                _APP_ID,
                cursor=CandidateCursor("stale-token", "candidate:old"),
            )
    finally:
        read_only.close()


def test_rebuild_generation_invalidates_an_existing_cursor(
    tmp_path: Path,
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    source, config = scale_state
    database_path = _copy_database(source, tmp_path / "rebuilt.sqlite3")
    read_only = connect_read_only(database_path)
    try:
        first = candidate_page(read_only, config, _APP_ID)
        assert first.next_cursor is not None
    finally:
        read_only.close()

    writer = connect(database_path)
    try:
        writer.execute("UPDATE candidate_groups SET generated_at='2026-08-31T12:00:00+00:00'")
        writer.commit()
    finally:
        writer.close()

    read_only = connect_read_only(database_path)
    try:
        with pytest.raises(CandidateGenerationChanged, match="restart"):
            candidate_page(read_only, config, _APP_ID, cursor=first.next_cursor)
    finally:
        read_only.close()


def test_mixed_generation_metadata_fails_explicitly(
    tmp_path: Path,
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    source, config = scale_state
    mixed_timestamp = _copy_database(source, tmp_path / "mixed-timestamp.sqlite3")
    writer = connect(mixed_timestamp)
    try:
        writer.execute(
            "UPDATE candidate_groups SET generated_at='2026-08-29T12:00:00+00:00' "
            "WHERE id='candidate:scale-2999'"
        )
        writer.commit()
    finally:
        writer.close()
    read_only = connect_read_only(mixed_timestamp)
    try:
        with pytest.raises(CandidateGenerationInconsistent, match="timestamps"):
            candidate_page(read_only, config, _APP_ID)
    finally:
        read_only.close()

    mixed_version = _copy_database(source, tmp_path / "mixed-version.sqlite3")
    writer = connect(mixed_version)
    try:
        writer.execute(
            "UPDATE candidate_groups SET generator_version='fixture-v2' "
            "WHERE id='candidate:scale-0001'"
        )
        writer.commit()
    finally:
        writer.close()
    read_only = connect_read_only(mixed_version)
    try:
        with pytest.raises(CandidateGenerationInconsistent, match="versions"):
            candidate_page(read_only, config, _APP_ID)
    finally:
        read_only.close()


def test_page_transaction_keeps_one_snapshot_and_next_page_detects_rebuild(
    tmp_path: Path,
    scale_state: tuple[Path, WorkTraceConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, config = scale_state
    database_path = _copy_database(source, tmp_path / "snapshot.sqlite3")
    writer_started = threading.Event()
    writer_updated = threading.Event()
    original = PacketBuilder.candidate_list_item
    first_projection = True

    def pause_after_snapshot(
        builder: PacketBuilder,
        app_id: str,
        candidate_id: str,
    ) -> dict[str, object]:
        nonlocal first_projection
        if first_projection:
            first_projection = False
            writer_started.set()
            assert writer_updated.wait(timeout=5)
        return original(builder, app_id, candidate_id)

    monkeypatch.setattr(PacketBuilder, "candidate_list_item", pause_after_snapshot)

    def rebuild() -> None:
        assert writer_started.wait(timeout=5)
        writer = connect(database_path)
        try:
            writer.autocommit = True
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE candidate_groups SET generated_at='2026-08-31T12:00:00+00:00'")
            writer_updated.set()
            writer.execute("COMMIT")
        finally:
            writer.close()

    thread = threading.Thread(target=rebuild, daemon=True)
    thread.start()
    read_only = connect_read_only(database_path)
    try:
        first = candidate_page(read_only, config, _APP_ID)
        assert first.next_cursor is not None
    finally:
        read_only.close()
    thread.join(timeout=5)
    assert not thread.is_alive()

    read_only = connect_read_only(database_path)
    try:
        with pytest.raises(CandidateGenerationChanged):
            candidate_page(read_only, config, _APP_ID, cursor=first.next_cursor)
    finally:
        read_only.close()


def test_each_page_rebuilds_context_and_observes_decision_then_undo(
    tmp_path: Path,
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    source, config = scale_state
    database_path = _copy_database(source, tmp_path / "decision-refresh.sqlite3")
    token = hashlib.sha256(
        json.dumps(
            [_APP_ID, _GENERATED_AT, _GENERATOR_VERSION],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cursor = CandidateCursor(token, "candidate:scale-2974")

    read_only = connect_read_only(database_path)
    first_diagnostics = _PageDiagnostics()
    try:
        before = _candidate_page(
            read_only,
            config,
            _APP_ID,
            cursor=cursor,
            diagnostics=first_diagnostics,
        )
    finally:
        read_only.close()
    before_item = next(item for item in before.items if item.candidate_id == "candidate:scale-2975")
    assert before_item.title == "Candidate 2975"

    writer = connect(database_path)
    try:
        rename_id = append_decision(
            writer,
            "rename_contribution",
            "candidate:scale-2975",
            {"app_id": _APP_ID, "title": "Reviewed 2975"},
        )
    finally:
        writer.close()

    read_only = connect_read_only(database_path)
    second_diagnostics = _PageDiagnostics()
    try:
        renamed = _candidate_page(
            read_only,
            config,
            _APP_ID,
            cursor=cursor,
            diagnostics=second_diagnostics,
        )
    finally:
        read_only.close()
    renamed_item = next(
        item for item in renamed.items if item.candidate_id == "candidate:scale-2975"
    )
    assert renamed.generation_token == before.generation_token
    assert renamed_item.title == "Reviewed 2975"
    assert renamed_item.title_authority == "human_decision"

    writer = connect(database_path)
    try:
        undo_decision(writer, rename_id)
    finally:
        writer.close()

    read_only = connect_read_only(database_path)
    third_diagnostics = _PageDiagnostics()
    try:
        undone = _candidate_page(
            read_only,
            config,
            _APP_ID,
            cursor=cursor,
            diagnostics=third_diagnostics,
        )
    finally:
        read_only.close()
    undone_item = next(item for item in undone.items if item.candidate_id == "candidate:scale-2975")
    assert undone_item.title == "Candidate 2975"
    assert undone_item.title_authority == "provider_observed"
    assert (
        first_diagnostics.authority_context_builds,
        second_diagnostics.authority_context_builds,
        third_diagnostics.authority_context_builds,
    ) == (1, 1, 1)


def test_candidate_scan_query_plan_uses_existing_indexes_without_a_temp_sort(
    scale_state: tuple[Path, WorkTraceConfig],
) -> None:
    database_path, _ = scale_state
    connection = connect_read_only(database_path)
    try:
        active_plan = [
            str(row["detail"])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {_ACTIVE_GENERATION_SQL}", (_APP_ID,)
            )
        ]
        older_plan = [
            str(row["detail"])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {_OLDER_GENERATION_SQL}",
                (_APP_ID, _GENERATED_AT),
            )
        ]
        newer_plan = [
            str(row["detail"])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {_NEWER_GENERATION_SQL}",
                (_APP_ID, _GENERATED_AT),
            )
        ]
        scan_plan = [
            str(row["detail"])
            for row in connection.execute(
                f"EXPLAIN QUERY PLAN {_RAW_CANDIDATE_SQL}",
                ("", _APP_ID, _GENERATED_AT, 50),
            )
        ]
    finally:
        connection.close()

    assert any("idx_candidates_app_time" in detail for detail in active_plan)
    assert any("idx_candidates_app_time" in detail for detail in older_plan)
    assert any("idx_candidates_app_time" in detail for detail in newer_plan)
    assert any("sqlite_autoindex_candidate_groups_1" in detail for detail in scan_plan)
    plans = active_plan + older_plan + newer_plan + scan_plan
    assert all("SCAN candidate_groups" not in detail for detail in plans)
    assert all("USE TEMP B-TREE" not in detail for detail in plans)
