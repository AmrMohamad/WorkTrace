from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from worktrace.candidates.decisions import (
    CREATION_ACTIONS,
    active_decisions,
    decision_lineages,
    decision_scope_map,
    snapshot_member_ids,
)
from worktrace.candidates.projector import CandidateView, project_candidate
from worktrace.config import AppConfig, WorkTraceConfig
from worktrace.db.authority import (
    authoritative_current_availability_ctes,
    authoritative_current_participation_ctes,
    authoritative_current_reference_ctes,
    authoritative_current_run_ctes,
)
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import NormalizedObject, SourceIdentity
from worktrace.errors import NotFound


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
    current_object_ids = {str(row["source_object_id"]) for row in current}
    current_observation_ids = {str(row["id"]) for row in current}
    current_sync_run_ids = {str(row["sync_run_id"]) for row in current}

    def placeholders(values: list[str]) -> str:
        return ",".join("?" for _ in values) or "NULL"

    all_decisions = [
        dict(row)
        for row in connection.execute("SELECT * FROM human_decisions ORDER BY created_at, id")
    ]
    decision_scopes = decision_scope_map(connection)
    active_decision_scopes = decision_scope_map(connection, active_only=True)
    decision_payloads: dict[str, dict[str, object]] = {}
    for decision in all_decisions:
        try:
            raw_payload = json.loads(str(decision["payload_json"]))
        except json.JSONDecodeError:
            raw_payload = {}
        decision_payloads[str(decision["id"])] = (
            raw_payload if isinstance(raw_payload, dict) else {}
        )

    valid_creation_decisions = [
        decision
        for decision in all_decisions
        if str(decision["action"]) in CREATION_ACTIONS
        and decision_scopes.get(str(decision["id"])) == app_id
        and isinstance(decision_payloads[str(decision["id"])].get("contribution_id"), str)
        and decision_payloads[str(decision["id"])]["contribution_id"]
    ]
    history_candidate_ids = {str(decision["target_id"]) for decision in valid_creation_decisions}
    contribution_ids = {
        str(decision_payloads[str(decision["id"])]["contribution_id"])
        for decision in valid_creation_decisions
    }
    active_lineages = tuple(
        lineage for lineage in decision_lineages(connection) if lineage.app_id == app_id
    )
    active_lineage_candidate_ids = {
        candidate_id for lineage in active_lineages for candidate_id in lineage.candidate_ids
    }
    active_lineage_contribution_ids = {
        contribution_id
        for lineage in active_lineages
        for contribution_id in lineage.contribution_ids
    }
    scoped_source_object_ids = {
        str(row[0])
        for row in connection.execute("SELECT id FROM source_objects WHERE app_id=?", (app_id,))
    }

    availability_rows = [
        dict(row)
        for row in connection.execute(
            f"""
            WITH {authoritative_current_availability_ctes()}
            SELECT event.*
            FROM authoritative_current_availability_events event
            JOIN source_objects object ON object.id=event.source_object_id
            WHERE object.app_id=?
            ORDER BY event.source_object_id, event.id
            """,
            (app_id,),
        )
    ]
    availability_object_ids = {str(row["source_object_id"]) for row in availability_rows}
    availability_sync_run_ids = {str(row["sync_run_id"]) for row in availability_rows}
    provenance_object_ids = sorted(availability_object_ids - current_object_ids)
    provenance_observations = [
        dict(row)
        for row in connection.execute(
            f"""
            WITH {authoritative_current_run_ctes()},
            ranked_export_provenance AS (
                SELECT observation.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY observation.source_object_id
                        ORDER BY eligible_run.completed_at DESC,
                                 observation.fetched_at DESC,
                                 observation.id DESC
                    ) AS position
                FROM observations observation
                JOIN source_objects object
                  ON object.id=observation.source_object_id
                JOIN ranked_authoritative_runs eligible_run
                  ON eligible_run.id=observation.sync_run_id
                 AND eligible_run.app_id=object.app_id
                 AND eligible_run.source=object.source
                 AND eligible_run.source_instance=object.source_instance
                WHERE object.app_id=?
                  AND observation.source_object_id IN ({placeholders(provenance_object_ids)})
            )
            SELECT * FROM ranked_export_provenance
            WHERE position=1
            ORDER BY source_object_id, id
            """,
            [app_id, *provenance_object_ids],
        )
    ]
    provenance_observation_ids = {str(row["id"]) for row in provenance_observations}
    provenance_sync_run_ids = {str(row["sync_run_id"]) for row in provenance_observations}
    object_ids = sorted(current_object_ids | availability_object_ids)
    observation_ids = sorted(current_observation_ids | provenance_observation_ids)
    sync_run_ids = sorted(
        current_sync_run_ids | availability_sync_run_ids | provenance_sync_run_ids
    )

    candidate_rows = list(
        connection.execute(
            "SELECT * FROM candidate_groups WHERE app_id=? ORDER BY generated_at, id",
            (app_id,),
        )
    )
    projected_candidates: list[tuple[sqlite3.Row, CandidateView]] = []
    unsupported_candidates: list[tuple[sqlite3.Row, CandidateView]] = []
    for candidate_row in candidate_rows:
        try:
            projected = project_candidate(connection, str(candidate_row["id"]))
        except NotFound:
            continue
        if projected.metadata_source_object_id is None:
            if projected.status == "confirmed":
                unsupported_candidates.append((candidate_row, projected))
            continue
        projected_candidates.append((candidate_row, projected))
    candidate_ids = sorted(projected.id for _, projected in projected_candidates)

    decision_roots = {
        *candidate_ids,
        *(projected.id for _, projected in unsupported_candidates),
        *history_candidate_ids,
        *active_lineage_candidate_ids,
        *active_lineage_contribution_ids,
        *object_ids,
    }
    included_decision_ids: set[str] = set()

    def decision_is_scoped(decision: dict[str, object]) -> bool:
        return decision_scopes.get(str(decision["id"])) == app_id

    changed = True
    while changed:
        changed = False
        scoped_targets = decision_roots | contribution_ids
        for decision in all_decisions:
            decision_id = str(decision["id"])
            target_id = str(decision["target_id"])
            undo_target_id = (
                str(decision["undo_target_id"]) if decision.get("undo_target_id") else None
            )
            if str(decision["action"]) in {"undo", "undo_decision"}:
                if (
                    target_id in scoped_targets
                    and undo_target_id in included_decision_ids
                    and decision_id not in included_decision_ids
                ):
                    included_decision_ids.add(decision_id)
                    changed = True
                continue
            if (
                target_id not in scoped_targets
                and decision_id not in included_decision_ids
                and undo_target_id not in included_decision_ids
            ):
                continue
            if not decision_is_scoped(decision):
                continue
            if decision_id not in included_decision_ids:
                included_decision_ids.add(decision_id)
                changed = True

    payload: dict[str, object] = {
        "schema": "worktrace-export-v3",
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
            f"""
            WITH {authoritative_current_participation_ctes()}
            SELECT * FROM authoritative_current_participations
            WHERE observation_id IN ({placeholders(observation_ids)})
            """,
            observation_ids,
        )
    ]
    payload["actors"] = [
        dict(row)
        for row in connection.execute(
            f"""
            WITH {authoritative_current_participation_ctes()}
            SELECT DISTINCT a.* FROM actors a
            JOIN authoritative_current_participations p ON p.actor_id=a.id
            WHERE p.observation_id IN ({placeholders(observation_ids)})
            """,
            observation_ids,
        )
    ]
    payload["source_object_availability_events"] = availability_rows
    payload["references"] = [
        dict(row)
        for row in connection.execute(
            f"""
            WITH {authoritative_current_reference_ctes()}
            SELECT * FROM authoritative_current_references WHERE app_id=?
              AND from_object_id IN ({placeholders(object_ids)})
              AND to_object_id IN ({placeholders(object_ids)})
              AND supporting_observation_id IN ({placeholders(observation_ids)})
            """,
            [app_id, *object_ids, *object_ids, *observation_ids],
        )
    ]
    payload["candidate_groups"] = [
        {
            "id": projected.id,
            "app_id": projected.app_id,
            "seed_object_id": (
                projected.seed_object_id if projected.seed_object_id in current_object_ids else None
            ),
            "metadata_source_object_id": projected.metadata_source_object_id,
            "unsupported_seed_object_id": (
                projected.seed_object_id
                if projected.seed_object_id not in current_object_ids
                else None
            ),
            "generator_version": str(row["generator_version"]),
            "suggested_title": projected.title,
            "suggested_type": projected.contribution_type,
            "status": projected.status,
            "generated_at": str(row["generated_at"]),
            "projection_version": 2,
            "unsupported_member_ids": list(projected.unsupported_member_ids),
        }
        for row, projected in projected_candidates
    ]
    payload["candidate_members"] = [
        {
            "candidate_id": projected.id,
            "source_object_id": str(member["source_object_id"]),
            "membership_reason": str(member["membership_reason"]),
            "context_only": int(bool(member["context_only"])),
        }
        for _, projected in projected_candidates
        for member in projected.members
    ]
    payload["human_decisions"] = [
        decision for decision in all_decisions if str(decision["id"]) in included_decision_ids
    ]

    unsupported_history: list[dict[str, object]] = []
    projected_by_id = {projected.id: projected for _, projected in unsupported_candidates}
    current_candidate_ids = {projected.id for _, projected in projected_candidates}
    creations_by_candidate: dict[str, list[dict[str, object]]] = {}
    for decision in valid_creation_decisions:
        creations_by_candidate.setdefault(str(decision["target_id"]), []).append(decision)
    for candidate_id in sorted(creations_by_candidate):
        if candidate_id in current_candidate_ids:
            continue
        creations = creations_by_candidate[candidate_id]
        active_creation_ids = {
            decision.id
            for decision in active_decisions(connection, candidate_id)
            if decision.action in CREATION_ACTIONS
            and active_decision_scopes.get(decision.id) == app_id
        }
        creation = next(
            (
                decision
                for decision in reversed(creations)
                if str(decision["id"]) in active_creation_ids
            ),
            creations[-1],
        )
        creation_id = str(creation["id"])
        creation_payload = decision_payloads[creation_id]
        contribution_id = str(creation_payload["contribution_id"])
        history_decision_ids = [
            str(decision["id"])
            for decision in all_decisions
            if str(decision["id"]) in included_decision_ids
            and str(decision["target_id"]) in {candidate_id, contribution_id}
        ]
        unsupported_projected = projected_by_id.get(candidate_id)
        unsupported_member_ids = sorted(
            (
                set(unsupported_projected.unsupported_member_ids)
                if unsupported_projected is not None
                else snapshot_member_ids(creation_payload) & scoped_source_object_ids
            )
            - current_object_ids
        )
        raw_title = creation_payload.get("title")
        unsupported_history.append(
            {
                "app_id": app_id,
                "candidate_id": candidate_id,
                "contribution_id": contribution_id,
                "current_evidence_available": False,
                "decision_ids": history_decision_ids,
                "status": (
                    "confirmed_history_unsupported"
                    if creation_id in active_creation_ids
                    else "confirmed_history_undone"
                ),
                "title": (
                    str(raw_title)
                    if isinstance(raw_title, str) and raw_title
                    else "Confirmed contribution history (current evidence unavailable)"
                ),
                "unsupported_member_ids": unsupported_member_ids,
            }
        )
    payload["unsupported_contribution_history"] = unsupported_history
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
