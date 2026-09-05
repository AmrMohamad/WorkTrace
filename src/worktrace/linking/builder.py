from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from worktrace.config import AppConfig
from worktrace.db.read_state import mark_read_states_changed
from worktrace.db.repository import EvidenceRepository, stable_id
from worktrace.linking.extractors import (
    extract_commit_shas,
    extract_full_commit_shas,
    extract_jira_keys,
    extract_mr_iids,
)
from worktrace.linking.mappings import mapped_commit_sha_allowed


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
    by_source_kind_external: dict[tuple[str, str, str], list[CurrentObject]] = {}
    for item in objects:
        by_source_kind_external.setdefault((item.source, item.kind, item.external_id), []).append(
            item
        )
    for item in objects:
        if item.kind == "jira_issue" and isinstance(item.data.get("key"), str):
            alias_key = ("jira", "jira_issue", str(item.data["key"]))
            if alias_key != (item.source, item.kind, item.external_id):
                by_source_kind_external.setdefault(alias_key, []).append(item)
    jira = {
        str(item.data.get("key", item.external_id)).upper(): item
        for item in objects
        if item.kind == "jira_issue"
    }
    commits = [item for item in objects if item.kind == "git_commit"]
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
                matches = [
                    commit for commit in commits if commit.external_id.lower().startswith(prefix)
                ]
                if len(matches) == 1 and matches[0].object_id != item.object_id:
                    target = matches[0]
                    if (
                        item.source == target.source
                        and item.source_instance != target.source_instance
                    ):
                        continue
                    if item.source == "gitlab" and target.source == "git":
                        # Cross-provider SHA matches require a full, explicit mapping.
                        continue
                    _insert(
                        connection,
                        app.id,
                        item,
                        target,
                        "mentions_commit_sha",
                        "exact_text",
                        prefix,
                    )

            if item.source == "gitlab":
                for sha in sorted(extract_full_commit_shas(text)):
                    matches = [
                        commit
                        for commit in commits
                        if commit.external_id.casefold() == sha
                        and commit.object_id != item.object_id
                    ]
                    for target in matches:
                        if mapped_commit_sha_allowed(app, item, target, sha):
                            _insert(
                                connection,
                                app.id,
                                item,
                                target,
                                "mapped_commit_sha",
                                "explicit_repo_project_full_sha:textual_reference",
                                sha,
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
                    targets = (
                        [
                            commit
                            for commit in commits
                            if commit.external_id.casefold()
                            == str(raw.get("target_external_id", "")).casefold()
                        ]
                        if target_key[:2] == ("git", "git_commit")
                        else by_source_kind_external.get(target_key, [])
                    )
                    exact_value = (
                        str(raw["exact_value"]) if raw.get("exact_value") is not None else None
                    )
                    for target in targets:
                        if target.object_id == item.object_id:
                            continue
                        if (
                            item.source == target.source
                            and item.source_instance != target.source_instance
                        ):
                            continue
                        if item.source == "gitlab" and target.source == "git":
                            if target.kind != "git_commit" or exact_value is None:
                                continue
                            if mapped_commit_sha_allowed(app, item, target, exact_value):
                                field = {
                                    "gitlab_source_head": "source_head",
                                    "gitlab_merge_commit": "merge_commit_sha",
                                    "gitlab_squash_commit": "squash_commit_sha",
                                    "gitlab_release_commit": "commit_record",
                                    "gitlab_deployment_commit": "deployment",
                                }.get(str(raw.get("relationship_type")), "pending_reference")
                                _insert(
                                    connection,
                                    app.id,
                                    item,
                                    target,
                                    "mapped_commit_sha",
                                    f"explicit_repo_project_full_sha:{field}",
                                    exact_value.casefold(),
                                )
                            continue
                        _insert(
                            connection,
                            app.id,
                            item,
                            target,
                            str(raw.get("relationship_type", "related_to")),
                            str(raw.get("extraction_method", "source_metadata")),
                            exact_value,
                        )

            structural: list[tuple[str, str, str]] = []
            if item.kind == "gitlab_mr":
                commit_shas = item.data.get("commit_shas")
                if isinstance(commit_shas, list):
                    for commit_sha in commit_shas:
                        structural.append(("git_commit", str(commit_sha), "mr_contains_commit"))
                for field in ("merge_commit_sha", "squash_commit_sha"):
                    field_sha = item.data.get(field)
                    if isinstance(field_sha, str) and field_sha:
                        structural.append(("git_commit", field_sha, "commit_introduced_by_mr"))
            elif item.kind == "gitlab_merge_request_commit":
                commit_sha = item.data.get("sha")
                if isinstance(commit_sha, str):
                    structural.append(("git_commit", commit_sha, "commit_record"))
            elif item.kind == "git_deployment":
                deployment_sha = item.data.get("sha")
                if isinstance(deployment_sha, str):
                    structural.append(("git_commit", deployment_sha, "deployment_contains_sha"))
            elif item.kind == "git_tag":
                target_sha = item.data.get("target_commit_sha")
                if isinstance(target_sha, str):
                    structural.append(("git_commit", target_sha, "tag_points_to_commit"))
            for kind, external_id, relationship in structural:
                targets = (
                    [
                        commit
                        for commit in commits
                        if commit.external_id.casefold() == external_id.casefold()
                    ]
                    if kind == "git_commit"
                    else by_source_kind_external.get(("git", kind, external_id), [])
                )
                for target in targets:
                    if (
                        item.source == target.source
                        and item.source_instance != target.source_instance
                    ):
                        continue
                    if item.source == "gitlab" and target.source == "git":
                        if mapped_commit_sha_allowed(app, item, target, external_id):
                            _insert(
                                connection,
                                app.id,
                                item,
                                target,
                                "mapped_commit_sha",
                                f"explicit_repo_project_full_sha:{relationship}",
                                external_id.casefold(),
                            )
                        continue
                    _insert(
                        connection,
                        app.id,
                        item,
                        target,
                        relationship,
                        "source_metadata",
                        external_id,
                    )
        mark_read_states_changed(connection, [app.id])

    row = connection.execute(
        'SELECT COUNT(*) AS count FROM "references" WHERE app_id=?', (app.id,)
    ).fetchone()
    return int(row["count"] if row else 0)
