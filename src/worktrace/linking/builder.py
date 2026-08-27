from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from worktrace.config import AppConfig
from worktrace.db.repository import EvidenceRepository, stable_id
from worktrace.linking.extractors import extract_commit_shas, extract_jira_keys, extract_mr_iids


@dataclass(frozen=True)
class CurrentObject:
    object_id: str
    observation_id: str
    source: str
    source_instance: str
    kind: str
    external_id: str
    title: str
    body: str
    data: dict[str, object]


def _objects(repository: EvidenceRepository, app_id: str) -> list[CurrentObject]:
    values: list[CurrentObject] = []
    for row in repository.current_observations(app_id):
        data = json.loads(str(row["data_json"]))
        values.append(
            CurrentObject(
                object_id=str(row["source_object_id"]),
                observation_id=str(row["id"]),
                source=str(row["source"]),
                source_instance=str(row["source_instance"]),
                kind=str(row["kind"]),
                external_id=str(row["external_id"]),
                title=str(row["title"] or ""),
                body=str(row["body_text"] or ""),
                data=data if isinstance(data, dict) else {},
            )
        )
    return values


def _insert(
    connection: sqlite3.Connection,
    app_id: str,
    source: CurrentObject,
    target: CurrentObject,
    relationship: str,
    method: str,
    exact_value: str | None,
) -> None:
    reference_id = stable_id(
        "ref", source.object_id, target.object_id, relationship, method, exact_value
    )
    connection.execute(
        """
        INSERT OR IGNORE INTO "references"(
            id, app_id, from_object_id, to_object_id, relationship_type,
            extraction_method, exact_value, supporting_observation_id, derived
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (
            reference_id,
            app_id,
            source.object_id,
            target.object_id,
            relationship,
            method,
            exact_value,
            source.observation_id,
        ),
    )


def rebuild_references(app: AppConfig, repository: EvidenceRepository) -> int:
    connection = repository.connection
    objects = _objects(repository, app.id)
    by_source_kind_external: dict[tuple[str, str, str], CurrentObject] = {
        (item.source, item.kind, item.external_id): item for item in objects
    }
    for item in objects:
        if item.kind == "jira_issue" and isinstance(item.data.get("key"), str):
            by_source_kind_external[("jira", "jira_issue", str(item.data["key"]))] = item
    jira = {
        str(item.data.get("key", item.external_id)).upper(): item
        for item in objects
        if item.kind == "jira_issue"
    }
    commits = {item.external_id.lower(): item for item in objects if item.kind == "git_commit"}
    mrs_by_iid: dict[str, list[CurrentObject]] = {}
    for item in objects:
        if item.kind == "gitlab_mr":
            mrs_by_iid.setdefault(str(item.data.get("iid", item.external_id)), []).append(item)

    with connection:
        connection.execute('DELETE FROM "references" WHERE app_id=? AND derived=1', (app.id,))
        for item in objects:
            text = f"{item.title}\n{item.body}"
            for key in sorted(extract_jira_keys(text, app)):
                target = jira.get(key)
                if target and target.object_id != item.object_id:
                    _insert(
                        connection, app.id, item, target, "mentions_jira_key", "exact_text", key
                    )

            for prefix in sorted(extract_commit_shas(text)):
                matches = [commit for sha, commit in commits.items() if sha.startswith(prefix)]
                if len(matches) == 1 and matches[0].object_id != item.object_id:
                    _insert(
                        connection,
                        app.id,
                        item,
                        matches[0],
                        "mentions_commit_sha",
                        "exact_text",
                        prefix,
                    )

            for iid in sorted(extract_mr_iids(text)):
                matches = mrs_by_iid.get(iid, [])
                target = matches[0] if len(matches) == 1 else None
                if target is not None and target.object_id != item.object_id:
                    _insert(
                        connection, app.id, item, target, "mentions_mr", "exact_text", f"!{iid}"
                    )

            pending = item.data.get("_pending_references", [])
            if isinstance(pending, list):
                for raw in pending:
                    if not isinstance(raw, dict):
                        continue
                    target_key = (
                        str(raw.get("target_source", "")),
                        str(raw.get("target_kind", "")),
                        str(raw.get("target_external_id", "")),
                    )
                    target = by_source_kind_external.get(target_key)
                    if target and target.object_id != item.object_id:
                        _insert(
                            connection,
                            app.id,
                            item,
                            target,
                            str(raw.get("relationship_type", "related_to")),
                            str(raw.get("extraction_method", "source_metadata")),
                            str(raw["exact_value"]) if raw.get("exact_value") is not None else None,
                        )

            structural: list[tuple[str, str, str]] = []
            if item.kind == "gitlab_mr":
                commit_shas = item.data.get("commit_shas")
                if isinstance(commit_shas, list):
                    for sha in commit_shas:
                        structural.append(("git_commit", str(sha), "mr_contains_commit"))
                for field in ("merge_commit_sha", "squash_commit_sha"):
                    sha = item.data.get(field)
                    if isinstance(sha, str) and sha:
                        structural.append(("git_commit", sha, "commit_introduced_by_mr"))
            elif item.kind == "git_deployment":
                sha = item.data.get("sha")
                if isinstance(sha, str):
                    structural.append(("git_commit", sha, "deployment_contains_sha"))
            elif item.kind == "git_tag":
                sha = item.data.get("target_commit_sha")
                if isinstance(sha, str):
                    structural.append(("git_commit", sha, "tag_points_to_commit"))
            for kind, external_id, relationship in structural:
                target = by_source_kind_external.get(("git", kind, external_id))
                if target:
                    _insert(
                        connection,
                        app.id,
                        item,
                        target,
                        relationship,
                        "source_metadata",
                        external_id,
                    )

    row = connection.execute(
        'SELECT COUNT(*) AS count FROM "references" WHERE app_id=?', (app.id,)
    ).fetchone()
    return int(row["count"] if row else 0)
