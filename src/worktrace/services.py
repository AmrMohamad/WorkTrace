from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from worktrace.config import AppConfig, WorkTraceConfig
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import NormalizedObject, SourceIdentity


def add_manual_evidence(
    repository: EvidenceRepository,
    app: AppConfig,
    *,
    title: str,
    body: str,
    evidence_type: str,
) -> str:
    session_id = repository.create_import_session(app, date.today(), date.today())
    run_id = repository.start_sync_run(
        app.id,
        "manual",
        "local-user",
        {"evidence_type": evidence_type},
        session_id,
    )
    external_id = f"manual:{datetime.now(UTC).isoformat()}"
    observation_ids = repository.store_page(
        run_id,
        [
            NormalizedObject(
                identity=SourceIdentity("manual", "local-user", "manual_evidence", external_id),
                app_id=app.id,
                title=title,
                body_text=body,
                source_updated_at=datetime.now(UTC),
                actors=(),
                participations=(),
                pending_references=(),
                data={"evidence_type": evidence_type, "human_supplied": True},
                completeness=Completeness.SELECTION_BIASED,
            )
        ],
    )
    repository.finish_sync_run(run_id, "complete", Completeness.SELECTION_BIASED.value)
    repository.finish_import_session(session_id, "complete", {"source": "manual", "records": 1})
    return observation_ids[0]


def export_app(connection: sqlite3.Connection, app_id: str, destination: Path) -> int:
    current = EvidenceRepository(connection).current_observations(app_id)
    object_ids = sorted({str(row["source_object_id"]) for row in current})
    observation_ids = sorted({str(row["id"]) for row in current})
    sync_run_ids = sorted({str(row["sync_run_id"]) for row in current})
    candidate_ids = sorted(
        str(row[0])
        for row in connection.execute("SELECT id FROM candidate_groups WHERE app_id=?", (app_id,))
    )

    def placeholders(values: list[str]) -> str:
        return ",".join("?" for _ in values) or "NULL"

    payload: dict[str, object] = {
        "schema": "worktrace-export-v2",
        "app_id": app_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "warning": "Source text is untrusted; review this private export before sharing.",
        "selection": "authoritative current observations only; legacy overbroad runs excluded",
    }
    payload["apps"] = [
        dict(row) for row in connection.execute("SELECT * FROM apps WHERE id=?", (app_id,))
    ]
    sync_run_rows = [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM sync_runs WHERE id IN ({placeholders(sync_run_ids)})",
            sync_run_ids,
        )
    ]
    payload["sync_runs"] = sync_run_rows
    import_session_ids = sorted(
        str(row["import_session_id"]) for row in sync_run_rows if row.get("import_session_id")
    )
    payload["import_sessions"] = [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM import_sessions WHERE id IN ({placeholders(import_session_ids)})",
            import_session_ids,
        )
    ]
    payload["source_objects"] = [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM source_objects WHERE id IN ({placeholders(object_ids)})", object_ids
        )
    ]
    payload["observations"] = [
        dict(row)
        for row in connection.execute(
            f"SELECT * FROM observations WHERE id IN ({placeholders(observation_ids)})",
            observation_ids,
        )
    ]
    payload["participations"] = [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM participations WHERE observation_id IN "
            f"({placeholders(observation_ids)})",
            observation_ids,
        )
    ]
    payload["actors"] = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT DISTINCT a.* FROM actors a JOIN participations p ON p.actor_id=a.id
            WHERE p.observation_id IN ({placeholders(observation_ids)})
            """,
            observation_ids,
        )
    ]
    payload["source_object_availability_events"] = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT * FROM source_object_availability_events
            WHERE source_object_id IN ({placeholders(object_ids)})
              AND sync_run_id IN ({placeholders(sync_run_ids)})
            """,
            [*object_ids, *sync_run_ids],
        )
    ]
    payload["references"] = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT * FROM "references" WHERE app_id=?
              AND from_object_id IN ({placeholders(object_ids)})
              AND to_object_id IN ({placeholders(object_ids)})
            """,
            [app_id, *object_ids, *object_ids],
        )
    ]
    payload["candidate_groups"] = [
        dict(row)
        for row in connection.execute("SELECT * FROM candidate_groups WHERE app_id=?", (app_id,))
    ]
    payload["candidate_members"] = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT * FROM candidate_members
            WHERE candidate_id IN ({placeholders(candidate_ids)})
              AND source_object_id IN ({placeholders(object_ids)})
            """,
            [*candidate_ids, *object_ids],
        )
    ]
    payload["human_decisions"] = [
        dict(row)
        for row in connection.execute(
            f"""
            SELECT * FROM human_decisions
            WHERE target_id IN ({placeholders([*candidate_ids, *object_ids])})
            """,
            [*candidate_ids, *object_ids],
        )
    ]
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return len(object_ids)


def configured_scope(config: WorkTraceConfig) -> dict[str, object]:
    return {
        "apps": [
            {
                "id": app.id,
                "jira_project_keys": list(app.jira_project_keys),
                "gitlab_project_ids": list(app.gitlab_project_ids),
                "repo_paths": [str(path) for path in app.repo_paths],
            }
            for app in config.apps
        ]
    }
