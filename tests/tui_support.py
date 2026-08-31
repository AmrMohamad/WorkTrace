from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

from worktrace.config import AppConfig, WorkTraceConfig


def add_second_application(
    connection: sqlite3.Connection,
    config: WorkTraceConfig,
    root: Path,
) -> WorkTraceConfig:
    other = AppConfig(
        id="other_app",
        name="Other App",
        market="YY",
        business_type="fixture",
        jira_project_keys=(),
        gitlab_project_ids=(),
        repo_paths=(root / "other-repo",),
        jira_key_patterns=(),
        production_environments=(),
        release_tag_patterns=(),
        ignored_paths=(),
    )
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) VALUES (?, ?, ?, ?)",
        (other.id, other.name, other.market, other.business_type),
    )
    connection.execute(
        """
        INSERT INTO sync_runs(
            id, app_id, source, source_instance, status, started_at, completed_at,
            adapter_version, scope_json, completeness
        ) VALUES (
            'run:other', 'other_app', 'manual', 'local', 'complete',
            '2026-08-30T12:00:00+00:00', '2026-08-30T12:00:00+00:00',
            'fixture', '{}', 'complete_for_scope'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO source_objects(
            id, app_id, source, source_instance, kind, external_id,
            first_seen_run_id, last_seen_run_id
        ) VALUES (
            'obj:other', 'other_app', 'manual', 'local', 'manual_evidence', 'other',
            'run:other', 'run:other'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO observations(
            id, source_object_id, sync_run_id, source_updated_at, fetched_at,
            payload_hash, title, body_text, data_json, completeness,
            adapter_version, normalization_version, redaction_version
        ) VALUES (
            'obs:other', 'obj:other', 'run:other', '2026-08-30T12:00:00+00:00',
            '2026-08-30T12:00:00+00:00', 'hash-other', 'Other title', 'Other body', '{}',
            'complete', 'fixture', '1', '1'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO candidate_groups(
            id, app_id, seed_object_id, generator_version, suggested_title,
            suggested_type, generated_at
        ) VALUES (
            'candidate:other', 'other_app', 'obj:other', 'fixture-v1', 'Other title',
            'feature', '2026-08-30T12:00:00+00:00'
        )
        """
    )
    connection.execute(
        "INSERT INTO candidate_members VALUES ('candidate:other', 'obj:other', 'fixture', 0)"
    )
    connection.commit()
    return replace(config, apps=(*config.apps, other))


def add_visible_candidate_page(connection: sqlite3.Connection, *, count: int = 30) -> None:
    generation = connection.execute(
        "SELECT generated_at, generator_version FROM candidate_groups LIMIT 1"
    ).fetchone()
    assert generation is not None
    connection.executemany(
        """
        INSERT INTO source_objects(
            id, app_id, source, source_instance, kind, external_id,
            first_seen_run_id, last_seen_run_id
        ) VALUES (?, 'sample_store', 'manual', 'local', 'manual_evidence', ?,
                  'run:manual', 'run:manual')
        """,
        [(f"obj:paged-{index:02d}", f"paged-{index:02d}") for index in range(count)],
    )
    connection.executemany(
        """
        INSERT INTO observations(
            id, source_object_id, sync_run_id, source_updated_at, fetched_at,
            payload_hash, title, body_text, data_json, completeness,
            adapter_version, normalization_version, redaction_version
        ) VALUES (?, ?, 'run:manual', '2026-08-26T12:00:00+00:00',
                  '2026-08-26T12:00:00+00:00', ?, ?, 'Synthetic page evidence.', '{}',
                  'complete', 'fixture', '1', '1')
        """,
        [
            (
                f"obs:paged-{index:02d}",
                f"obj:paged-{index:02d}",
                f"hash-paged-{index:02d}",
                f"Paged candidate {index:02d}",
            )
            for index in range(count)
        ],
    )
    connection.executemany(
        """
        INSERT INTO candidate_groups(
            id, app_id, seed_object_id, generator_version, suggested_title,
            suggested_type, generated_at
        ) VALUES (?, 'sample_store', ?, ?, ?, 'feature', ?)
        """,
        [
            (
                f"candidate:paged-{index:02d}",
                f"obj:paged-{index:02d}",
                str(generation["generator_version"]),
                f"Paged candidate {index:02d}",
                str(generation["generated_at"]),
            )
            for index in range(count)
        ],
    )
    connection.executemany(
        """
        INSERT INTO candidate_members(
            candidate_id, source_object_id, membership_reason, context_only
        ) VALUES (?, ?, 'fixture', 0)
        """,
        [(f"candidate:paged-{index:02d}", f"obj:paged-{index:02d}") for index in range(count)],
    )
    connection.commit()
