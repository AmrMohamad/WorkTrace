from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, cast

import httpx
import typer

from worktrace import __version__
from worktrace.adapters.git_local import LocalGitAdapter, LocalGitConfig
from worktrace.adapters.gitlab import GitLabAdapter, GitLabConfig
from worktrace.adapters.jira import JiraAdapter, JiraConfig
from worktrace.candidates.builder import rebuild_candidates
from worktrace.candidates.decisions import append_decision, undo_decision
from worktrace.candidates.projector import list_candidates, project_candidate
from worktrace.config import (
    AppConfig,
    WorkTraceConfig,
    gitlab_credentials,
    jira_credentials,
    load_config,
)
from worktrace.db.connection import connect
from worktrace.db.migrations import backup_database, migrate
from worktrace.db.queries import search_evidence, source_status
from worktrace.db.repository import EvidenceRepository, stable_id
from worktrace.doctor import run_doctor
from worktrace.domain.models import JsonValue
from worktrace.errors import ConfigurationError, WorkTraceError
from worktrace.importers.orchestrator import ImportResult, import_snapshot
from worktrace.linking.builder import rebuild_references
from worktrace.linking.extractors import extract_jira_keys
from worktrace.local_security import email_hmac_key
from worktrace.normalize.redaction import Redactor
from worktrace.paths import ensure_private_directory
from worktrace.services import add_manual_evidence, export_app

app = typer.Typer(no_args_is_help=True, help="Local evidence-oriented contribution reconstruction.")
import_app = typer.Typer(no_args_is_help=True, help="Import full source snapshots.")
candidates_app = typer.Typer(
    no_args_is_help=True, help="Inspect generated contribution candidates."
)
evidence_app = typer.Typer(no_args_is_help=True, help="Add or inspect explicit evidence.")
rebuild_app = typer.Typer(no_args_is_help=True, help="Deterministically rebuild derived data.")
app.add_typer(import_app, name="import")
app.add_typer(candidates_app, name="candidates")
app.add_typer(evidence_app, name="evidence")
app.add_typer(rebuild_app, name="rebuild")

ConfigOption = Annotated[Path | None, typer.Option("--config", exists=True, dir_okay=False)]


def _emit(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def _iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorkTraceError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def _window(configuration: WorkTraceConfig, start: str, end: str) -> tuple[date, date]:
    date_from = _iso_date(start, "date_from")
    date_to = _iso_date(end, "date_to")
    if date_from > date_to:
        raise WorkTraceError("date_from must not be after date_to")
    if date_from < configuration.employment_from or date_to > configuration.employment_to:
        raise WorkTraceError("import window is outside the configured employment scope")
    return date_from, date_to


def _source_instance(app_id: str, source: str, identifier: object) -> str:
    return stable_id("source", app_id, source, identifier)


def _open(
    config_path: Path | None = None,
) -> tuple[WorkTraceConfig, sqlite3.Connection, EvidenceRepository]:
    config = load_config(config_path)
    connection = connect(config.database_path)
    redactor = Redactor(email_hmac_key(config.data_directory, create=False))
    return config, connection, EvidenceRepository(connection, redactor)


@app.callback()
def main() -> None:
    """WorkTrace keeps source observations separate from human claims."""


@app.command()
def version() -> None:
    """Print the WorkTrace version."""
    typer.echo(__version__)


@app.command("init")
def initialize(config: ConfigOption = None) -> None:
    """Create or upgrade the private local ledger idempotently."""
    configuration = load_config(config)
    ensure_private_directory(configuration.data_directory)
    email_hmac_key(configuration.data_directory, create=True)
    connection = connect(configuration.database_path)
    try:
        applied = migrate(connection, configuration.database_path)
        repository = EvidenceRepository(connection)
        repository.ensure_apps(configuration)
    finally:
        connection.close()
    configuration.database_path.chmod(0o600)
    _emit({"database": str(configuration.database_path), "migrations_applied": applied})


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config", dir_okay=False)] = None,
    live: Annotated[
        bool,
        typer.Option("--live", help="Perform explicit non-persistent provider checks."),
    ] = False,
) -> None:
    """Validate configuration, storage, source scope, and optional credentials."""
    try:
        configuration = load_config(config)
    except ConfigurationError as exc:
        result: dict[str, object] = {
            "ok": False,
            "checks": [
                {
                    "name": "configuration",
                    "scope": "local",
                    "status": "fail",
                    "message": str(exc),
                    "remediation": "Correct the WorkTrace TOML configuration and scope mappings.",
                    "ok": False,
                }
            ],
        }
    else:
        result = run_doctor(configuration, live=live)
    _emit(result)
    if not bool(result["ok"]):
        raise typer.Exit(2)


def _git_self_ids(configuration: WorkTraceConfig, key: bytes) -> set[str]:
    redactor = Redactor(key)
    return {redactor.hash_email(email) for email in configuration.identity.git_author_emails}


@dataclass(frozen=True, slots=True)
class _CommitSeedSelection:
    values: tuple[str, ...]
    total_count: int
    limit: int
    policy: str = "source_updated_at_desc_then_sha_desc"

    @property
    def dropped_count(self) -> int:
        return self.total_count - len(self.values)


def _relevant_git_commit_shas(
    repository: EvidenceRepository, app_id: str, *, limit: int = 200
) -> _CommitSeedSelection:
    """Return bounded current self-authored/coauthored local commit identities."""

    current = repository.current_observations(app_id)
    current_observation_ids = [str(row["id"]) for row in current]
    if not current_observation_ids:
        return _CommitSeedSelection((), 0, limit)
    placeholders = ",".join("?" for _ in current_observation_ids)
    allowed_objects = {
        str(row["source_object_id"])
        for row in repository.connection.execute(
            f"""
            SELECT DISTINCT p.source_object_id
            FROM participations p
            JOIN actors a ON a.id=p.actor_id
            JOIN source_objects so ON so.id=p.source_object_id
            WHERE so.app_id=? AND so.source='git' AND so.kind='git_commit'
              AND a.is_self=1 AND p.role IN ('git_author', 'git_coauthor')
              AND p.observation_id IN ({placeholders})
            """,
            [app_id, *current_observation_ids],
        )
    }
    candidates: dict[str, str] = {}
    for row in current:
        if (
            str(row["source_object_id"]) not in allowed_objects
            or str(row["source"]) != "git"
            or str(row["kind"]) != "git_commit"
        ):
            continue
        sha = str(row["external_id"]).lower()
        source_updated_at = str(row["source_updated_at"] or "")
        candidates[sha] = max(source_updated_at, candidates.get(sha, ""))
    ordered = sorted(
        candidates,
        key=lambda sha: (candidates[sha], sha),
        reverse=True,
    )
    return _CommitSeedSelection(tuple(ordered[:limit]), len(ordered), limit)


def _commit_seed_scope(selection: _CommitSeedSelection) -> dict[str, JsonValue]:
    details: dict[str, JsonValue] = {
        "relevant_local_commit_sha_count": len(selection.values),
        "relevant_local_commit_sha_total": selection.total_count,
        "relevant_local_commit_sha_limit": selection.limit,
        "relevant_local_commit_selection_policy": selection.policy,
    }
    if selection.dropped_count:
        limitation = (
            "Local Git commit seeds exceeded the GitLab association bound; "
            "the most recently updated seeds were retained deterministically."
        )
        details.update(
            {
                "selection_biased": True,
                "limitations": [limitation],
                "selection_events": [
                    {
                        "kind": "local_git_commit_seed_cap",
                        "input_count": selection.total_count,
                        "selected_count": len(selection.values),
                        "dropped_count": selection.dropped_count,
                        "limit": selection.limit,
                        "selection_policy": selection.policy,
                    }
                ],
            }
        )
    return details


def _discovered_jira_keys(
    repository: EvidenceRepository, configured_app: AppConfig, *, limit: int = 500
) -> tuple[str, ...]:
    """Derive exact in-scope Jira keys from the authoritative current ledger view."""

    app_config = configured_app
    keys: set[str] = set()
    for row in repository.current_observations(app_config.id):
        text = f"{row['title'] or ''}\n{row['body_text'] or ''}"
        keys.update(extract_jira_keys(text, app_config))
        try:
            data = json.loads(str(row["data_json"]))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        pending = data.get("_pending_references", [])
        if not isinstance(pending, list):
            continue
        for raw in pending:
            if not isinstance(raw, dict) or raw.get("target_source") != "jira":
                continue
            key = str(raw.get("target_external_id", "")).upper()
            if app_config.allows_jira_key(key):
                keys.add(key)
    return tuple(sorted(keys)[:limit])


def _import_summary(
    result: ImportResult,
    *,
    discovery_counts: dict[str, int] | None = None,
    discovery_reasons: tuple[str, ...] = (),
) -> dict[str, object]:
    summary = cast(dict[str, object], asdict(result))
    summary["completeness"] = result.completeness
    summary["discovery_counts"] = discovery_counts or {}
    summary["discovery_reasons"] = list(discovery_reasons)
    return summary


@import_app.command("git")
def import_git(
    app_id: str,
    repo: Path,
    date_from: str,
    date_to: str,
    config: ConfigOption = None,
) -> None:
    configuration, connection, repository = _open(config)
    try:
        start, end = _window(configuration, date_from, date_to)
        configured_app = configuration.app(app_id)
        scoped_repo = configured_app.assert_repo_scope(repo)
        source_instance = _source_instance(app_id, "git", scoped_repo)
        key = email_hmac_key(configuration.data_directory, create=False)
        adapter = LocalGitAdapter(
            LocalGitConfig(
                repository_path=scoped_repo,
                allowed_root=scoped_repo,
                source_instance=source_instance,
                app_id=app_id,
                email_key=key,
                jira_project_keys=configured_app.jira_project_keys,
                date_from=start,
                date_to=end,
            )
        )
        result = import_snapshot(
            configured_app,
            adapter,
            repository,
            source="git",
            source_instance=source_instance,
            date_from=start,
            date_to=end,
            self_actor_ids=_git_self_ids(configuration, key),
            self_display_names={
                name.casefold() for name in configuration.identity.git_author_names
            },
            scope_details={"selection_reasons": ["configured repository read-only snapshot"]},
        )
        _emit(
            _import_summary(
                result,
                discovery_reasons=("configured repository read-only snapshot",),
            )
        )
        if result.status != "complete":
            raise typer.Exit(2)
    finally:
        connection.close()


@import_app.command("jira")
def import_jira(
    app_id: str,
    date_from: str,
    date_to: str,
    config: ConfigOption = None,
) -> None:
    configuration, connection, repository = _open(config)
    try:
        start, end = _window(configuration, date_from, date_to)
        configured_app = configuration.app(app_id)
        credentials = jira_credentials()
        if credentials is None:
            raise WorkTraceError("Jira credentials are not configured")
        key = email_hmac_key(configuration.data_directory, create=False)
        source_instance = _source_instance(app_id, "jira", credentials.base_url)
        discovered_keys = _discovered_jira_keys(repository, configured_app)
        with httpx.Client(
            base_url=credentials.base_url,
            auth=(credentials.email, credentials.token),
            headers={"Accept": "application/json"},
            timeout=30,
        ) as client:
            adapter = JiraAdapter(
                JiraConfig(
                    base_url=credentials.base_url,
                    source_instance=source_instance,
                    app_id=app_id,
                    project_keys=configured_app.jira_project_keys,
                    email_key=key,
                    date_from=start,
                    date_to=end,
                    account_id=configuration.identity.jira_account_id,
                    discovered_issue_keys=discovered_keys,
                ),
                client,
            )
            result = import_snapshot(
                configured_app,
                adapter,
                repository,
                source="jira",
                source_instance=source_instance,
                date_from=start,
                date_to=end,
                self_actor_ids=(
                    {configuration.identity.jira_account_id}
                    if configuration.identity.jira_account_id
                    else set()
                ),
                scope_details={
                    "exact_jira_key_count": len(discovered_keys),
                    "selection_reasons": [
                        "configured identity participation",
                        "current ledger exact-key references",
                    ],
                    "known_selection_bias": (
                        "comment-only participation may not be discoverable by provider search"
                    ),
                },
            )
        _emit(
            _import_summary(
                result,
                discovery_counts={"exact_jira_keys": len(discovered_keys)},
                discovery_reasons=(
                    "configured identity participation",
                    "current ledger exact-key references",
                ),
            )
        )
        if result.status != "complete":
            raise typer.Exit(2)
    finally:
        connection.close()


@import_app.command("gitlab")
def import_gitlab(
    app_id: str,
    project_id: int,
    date_from: str,
    date_to: str,
    config: ConfigOption = None,
) -> None:
    configuration, connection, repository = _open(config)
    try:
        start, end = _window(configuration, date_from, date_to)
        configured_app = configuration.app(app_id)
        if not configured_app.allows_gitlab_project(project_id):
            raise WorkTraceError("GitLab project is not configured for this app")
        credentials = gitlab_credentials()
        if credentials is None:
            raise WorkTraceError("GitLab credentials are not configured")
        if (
            configuration.identity.gitlab_user_id is None
            and configuration.identity.gitlab_username is None
        ):
            raise WorkTraceError("GitLab identity is required for user-scoped discovery")
        key = email_hmac_key(configuration.data_directory, create=False)
        commit_seeds = _relevant_git_commit_shas(repository, app_id)
        with httpx.Client(
            base_url=credentials.base_url,
            headers={"PRIVATE-TOKEN": credentials.token, "Accept": "application/json"},
            timeout=30,
        ) as client:
            source_instance = _source_instance(app_id, "gitlab", project_id)
            adapter = GitLabAdapter(
                GitLabConfig(
                    base_url=credentials.base_url,
                    source_instance=source_instance,
                    app_id=app_id,
                    project_id=project_id,
                    email_key=key,
                    date_from=start,
                    date_to=end,
                    jira_project_keys=configured_app.jira_project_keys,
                    user_id=configuration.identity.gitlab_user_id,
                    username=configuration.identity.gitlab_username,
                    production_environments=configured_app.production_environments,
                    relevant_commit_shas=commit_seeds.values,
                ),
                client,
            )
            own_ids = set()
            if configuration.identity.gitlab_user_id is not None:
                own_ids.add(str(configuration.identity.gitlab_user_id))
            if configuration.identity.gitlab_username:
                own_ids.add(configuration.identity.gitlab_username)
            result = import_snapshot(
                configured_app,
                adapter,
                repository,
                source="gitlab",
                source_instance=source_instance,
                date_from=start,
                date_to=end,
                self_actor_ids=own_ids,
                scope_details={
                    **_commit_seed_scope(commit_seeds),
                    "selection_reasons": [
                        "configured identity authored/assigned/reviewed merge requests",
                        "current self-authored/coauthored local commits",
                    ],
                },
            )
        _emit(
            _import_summary(
                result,
                discovery_counts={
                    "relevant_local_commit_shas": len(commit_seeds.values),
                    "relevant_local_commit_shas_total": commit_seeds.total_count,
                    "relevant_local_commit_shas_dropped": commit_seeds.dropped_count,
                },
                discovery_reasons=(
                    "configured identity authored/assigned/reviewed merge requests",
                    "current self-authored/coauthored local commits",
                ),
            )
        )
        if result.status != "complete":
            raise typer.Exit(2)
    finally:
        connection.close()


@import_app.command("all")
def import_all(
    app_id: str,
    date_from: str,
    date_to: str,
    config: ConfigOption = None,
) -> None:
    """Import every configured source and report partial sources explicitly."""
    configuration, connection, repository = _open(config)
    start, end = _window(configuration, date_from, date_to)
    configured_app = configuration.app(app_id)
    session_id = repository.create_import_session(configured_app, start, end)
    results: list[dict[str, object]] = []
    try:
        key = email_hmac_key(configuration.data_directory, create=False)
        for repo_path in configured_app.repo_paths:
            source_instance = _source_instance(app_id, "git", repo_path)
            adapter = LocalGitAdapter(
                LocalGitConfig(
                    repository_path=repo_path,
                    allowed_root=repo_path,
                    source_instance=source_instance,
                    app_id=app_id,
                    email_key=key,
                    jira_project_keys=configured_app.jira_project_keys,
                    date_from=start,
                    date_to=end,
                )
            )
            result = import_snapshot(
                configured_app,
                adapter,
                repository,
                source="git",
                source_instance=source_instance,
                date_from=start,
                date_to=end,
                self_actor_ids=_git_self_ids(configuration, key),
                self_display_names={
                    name.casefold() for name in configuration.identity.git_author_names
                },
                import_session_id=session_id,
                finish_session=False,
                scope_details={"selection_reasons": ["configured repository read-only snapshot"]},
            )
            results.append(
                _import_summary(
                    result,
                    discovery_reasons=("configured repository read-only snapshot",),
                )
            )

        commit_seeds = _relevant_git_commit_shas(repository, app_id)
        gitlab_auth = gitlab_credentials()
        gitlab_identity_configured = (
            configuration.identity.gitlab_user_id is not None
            or configuration.identity.gitlab_username is not None
        )
        for project_id in configured_app.gitlab_project_ids:
            source_instance = _source_instance(app_id, "gitlab", project_id)
            if gitlab_auth is None or not gitlab_identity_configured:
                failure = (
                    "GitLab credentials are not configured"
                    if gitlab_auth is None
                    else "GitLab identity is required for user-scoped discovery"
                )
                run_id = repository.start_sync_run(
                    app_id,
                    "gitlab",
                    source_instance,
                    {
                        "project_id": project_id,
                        "selection_policy_version": 2,
                        "date_from": start.isoformat(),
                        "date_to": end.isoformat(),
                    },
                    session_id,
                )
                repository.finish_sync_run(
                    run_id,
                    "failed",
                    "source_unavailable",
                    failure,
                )
                results.append(
                    {
                        "run_id": run_id,
                        "source": "gitlab",
                        "project_id": project_id,
                        "status": "source_unavailable",
                        "completeness": "partial",
                        "discovery_counts": {
                            "relevant_local_commit_shas": len(commit_seeds.values),
                            "relevant_local_commit_shas_total": commit_seeds.total_count,
                            "relevant_local_commit_shas_dropped": commit_seeds.dropped_count,
                        },
                        "discovery_reasons": [
                            "configured identity authored/assigned/reviewed merge requests",
                            "current self-authored/coauthored local commits",
                        ],
                    }
                )
                continue
            with httpx.Client(
                base_url=gitlab_auth.base_url,
                headers={"PRIVATE-TOKEN": gitlab_auth.token, "Accept": "application/json"},
                timeout=30,
            ) as client:
                own_ids = {
                    str(value)
                    for value in (
                        configuration.identity.gitlab_user_id,
                        configuration.identity.gitlab_username,
                    )
                    if value is not None
                }
                result = import_snapshot(
                    configured_app,
                    GitLabAdapter(
                        GitLabConfig(
                            base_url=gitlab_auth.base_url,
                            source_instance=source_instance,
                            app_id=app_id,
                            project_id=project_id,
                            email_key=key,
                            date_from=start,
                            date_to=end,
                            jira_project_keys=configured_app.jira_project_keys,
                            user_id=configuration.identity.gitlab_user_id,
                            username=configuration.identity.gitlab_username,
                            production_environments=configured_app.production_environments,
                            relevant_commit_shas=commit_seeds.values,
                        ),
                        client,
                    ),
                    repository,
                    source="gitlab",
                    source_instance=source_instance,
                    date_from=start,
                    date_to=end,
                    self_actor_ids=own_ids,
                    import_session_id=session_id,
                    finish_session=False,
                    scope_details={
                        **_commit_seed_scope(commit_seeds),
                        "selection_reasons": [
                            "configured identity authored/assigned/reviewed merge requests",
                            "current self-authored/coauthored local commits",
                        ],
                    },
                )
                results.append(
                    _import_summary(
                        result,
                        discovery_counts={
                            "relevant_local_commit_shas": len(commit_seeds.values),
                            "relevant_local_commit_shas_total": commit_seeds.total_count,
                            "relevant_local_commit_shas_dropped": commit_seeds.dropped_count,
                        },
                        discovery_reasons=(
                            "configured identity authored/assigned/reviewed merge requests",
                            "current self-authored/coauthored local commits",
                        ),
                    )
                )

        discovered_keys = _discovered_jira_keys(repository, configured_app)
        jira_auth = jira_credentials()
        if configured_app.jira_project_keys:
            if jira_auth is None:
                run_id = repository.start_sync_run(
                    app_id,
                    "jira",
                    _source_instance(app_id, "jira", "configured"),
                    {
                        "project_keys": list(configured_app.jira_project_keys),
                        "selection_policy_version": 2,
                        "date_from": start.isoformat(),
                        "date_to": end.isoformat(),
                    },
                    session_id,
                )
                repository.finish_sync_run(
                    run_id,
                    "failed",
                    "source_unavailable",
                    "Jira credentials are not configured",
                )
                results.append(
                    {
                        "run_id": run_id,
                        "source": "jira",
                        "status": "source_unavailable",
                        "completeness": "partial",
                        "discovery_counts": {"exact_jira_keys": len(discovered_keys)},
                        "discovery_reasons": [
                            "configured identity participation",
                            "current ledger exact-key references",
                        ],
                    }
                )
            else:
                source_instance = _source_instance(app_id, "jira", jira_auth.base_url)
                with httpx.Client(
                    base_url=jira_auth.base_url,
                    auth=(jira_auth.email, jira_auth.token),
                    headers={"Accept": "application/json"},
                    timeout=30,
                ) as client:
                    result = import_snapshot(
                        configured_app,
                        JiraAdapter(
                            JiraConfig(
                                base_url=jira_auth.base_url,
                                source_instance=source_instance,
                                app_id=app_id,
                                project_keys=configured_app.jira_project_keys,
                                email_key=key,
                                date_from=start,
                                date_to=end,
                                account_id=configuration.identity.jira_account_id,
                                discovered_issue_keys=discovered_keys,
                            ),
                            client,
                        ),
                        repository,
                        source="jira",
                        source_instance=source_instance,
                        date_from=start,
                        date_to=end,
                        self_actor_ids=(
                            {configuration.identity.jira_account_id}
                            if configuration.identity.jira_account_id
                            else set()
                        ),
                        import_session_id=session_id,
                        finish_session=False,
                        scope_details={
                            "exact_jira_key_count": len(discovered_keys),
                            "selection_reasons": [
                                "configured identity participation",
                                "current ledger exact-key references",
                            ],
                            "known_selection_bias": (
                                "comment-only participation may not be discoverable by provider "
                                "search"
                            ),
                        },
                    )
                    results.append(
                        _import_summary(
                            result,
                            discovery_counts={"exact_jira_keys": len(discovered_keys)},
                            discovery_reasons=(
                                "configured identity participation",
                                "current ledger exact-key references",
                            ),
                        )
                    )

        reference_count = rebuild_references(configured_app, repository)
        candidate_count = rebuild_candidates(app_id, repository)
        complete = all(result.get("status") == "complete" for result in results)
        overall = "complete" if complete else "partial"
        repository.finish_import_session(
            session_id,
            overall,
            {
                "sources": cast(list[JsonValue], results),
                "complete_sources": sum(r.get("status") == "complete" for r in results),
                "reference_count": reference_count,
                "candidate_count": candidate_count,
            },
        )
        _emit(
            {
                "session_id": session_id,
                "status": overall,
                "sources": results,
                "reference_count": reference_count,
                "candidate_count": candidate_count,
            }
        )
        if not complete:
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except Exception as exc:
        repository.finish_import_session(
            session_id,
            "partial",
            {
                "sources": cast(list[JsonValue], results),
                "error": (str(exc)[:500] or type(exc).__name__),
            },
        )
        raise
    finally:
        connection.close()


@app.command()
def status(app_id: str, config: ConfigOption = None) -> None:
    configuration, connection, _ = _open(config)
    try:
        configuration.app(app_id)
        _emit({"app_id": app_id, "sources": source_status(connection, app_id)})
    finally:
        connection.close()


@app.command()
def search(
    app_id: str,
    query: str,
    kind: Annotated[list[str] | None, typer.Option("--kind")] = None,
    limit: int = 20,
    config: ConfigOption = None,
) -> None:
    configuration, connection, _ = _open(config)
    try:
        configuration.app(app_id)
        _emit(search_evidence(connection, app_id, query, kinds=tuple(kind or ()), limit=limit))
    finally:
        connection.close()


@candidates_app.command("list")
def candidates_list(app_id: str, config: ConfigOption = None) -> None:
    configuration, connection, _ = _open(config)
    try:
        configuration.app(app_id)
        _emit([asdict(candidate) for candidate in list_candidates(connection, app_id)])
    finally:
        connection.close()


@candidates_app.command()
def show(candidate_id: str, config: ConfigOption = None) -> None:
    _, connection, _ = _open(config)
    try:
        _emit(asdict(project_candidate(connection, candidate_id)))
    finally:
        connection.close()


def _decision(action: str, target_id: str, payload: dict[str, object], config: Path | None) -> None:
    configuration, connection, _ = _open(config)
    try:
        sanitized = Redactor(
            email_hmac_key(configuration.data_directory, create=False)
        ).redact_payload(payload)
        if not isinstance(sanitized, dict):
            raise WorkTraceError("decision payload must be an object")
        safe_payload = cast(dict[str, object], sanitized)
        _emit(
            {
                "decision_id": append_decision(
                    connection,
                    action,
                    target_id,
                    safe_payload,
                    redactor=Redactor(email_hmac_key(configuration.data_directory, create=False)),
                )
            }
        )
    finally:
        connection.close()


@app.command()
def confirm(candidate_id: str, config: ConfigOption = None) -> None:
    _, connection, _ = _open(config)
    try:
        candidate = project_candidate(connection, candidate_id)
        contribution_id = stable_id("contribution", candidate_id)
        payload: dict[str, object] = {
            "contribution_id": contribution_id,
            "app_id": candidate.app_id,
            "title": candidate.title,
            "type": candidate.contribution_type,
            "members": [str(member["source_object_id"]) for member in candidate.members],
            "context_members": [
                str(member["source_object_id"])
                for member in candidate.members
                if bool(member.get("context_only"))
            ],
        }
        decision_id = append_decision(connection, "confirm_candidate", candidate_id, payload)
        _emit({"decision_id": decision_id, "contribution_id": contribution_id})
    finally:
        connection.close()


@app.command("ignore")
def ignore_candidate(candidate_id: str, reason: str = "", config: ConfigOption = None) -> None:
    _decision("ignore_candidate", candidate_id, {"reason": reason}, config)


@app.command()
def rename(
    candidate_id: str, title: str, contribution_type: str = "unknown", config: ConfigOption = None
) -> None:
    _decision(
        "rename_contribution",
        candidate_id,
        {"title": title, "type": contribution_type},
        config,
    )


@app.command("add-member")
def add_member(candidate_id: str, source_object_id: str, config: ConfigOption = None) -> None:
    _decision("add_member", candidate_id, {"source_object_id": source_object_id}, config)


@app.command("remove-member")
def remove_member(candidate_id: str, source_object_id: str, config: ConfigOption = None) -> None:
    _decision("remove_member", candidate_id, {"source_object_id": source_object_id}, config)


@app.command()
def merge(candidate_id: str, other_candidate_ids: list[str], config: ConfigOption = None) -> None:
    _, connection, _ = _open(config)
    try:
        views = [
            project_candidate(connection, value) for value in [candidate_id, *other_candidate_ids]
        ]
        app_ids = {view.app_id for view in views}
        if len(app_ids) != 1:
            raise WorkTraceError("merged candidates must belong to one app")
        member_ids = sorted(
            {str(member["source_object_id"]) for view in views for member in view.members}
        )
        contribution_id = stable_id("contribution", candidate_id, *sorted(other_candidate_ids))
        decision_id = append_decision(
            connection,
            "merge_contributions",
            candidate_id,
            {
                "contribution_id": contribution_id,
                "candidate_ids": other_candidate_ids,
                "app_id": views[0].app_id,
                "title": views[0].title,
                "type": views[0].contribution_type,
                "members": member_ids,
            },
        )
        _emit({"decision_id": decision_id, "contribution_id": contribution_id})
    finally:
        connection.close()


@app.command()
def split(
    candidate_id: str, keep_source_object_ids: list[str], config: ConfigOption = None
) -> None:
    candidate = candidate_id
    contribution_id = stable_id("contribution", candidate, *sorted(keep_source_object_ids))
    _decision(
        "split_contribution",
        candidate,
        {
            "contribution_id": contribution_id,
            "keep_source_object_ids": keep_source_object_ids,
            "members": keep_source_object_ids,
        },
        config,
    )


@app.command()
def undo(decision_id: str, config: ConfigOption = None) -> None:
    _, connection, _ = _open(config)
    try:
        _emit({"decision_id": undo_decision(connection, decision_id)})
    finally:
        connection.close()


@evidence_app.command("add")
def evidence_add(
    app_id: str,
    title: str,
    body: str,
    evidence_type: str = "manual_attestation",
    config: ConfigOption = None,
) -> None:
    configuration, connection, repository = _open(config)
    try:
        redactor = Redactor(email_hmac_key(configuration.data_directory, create=False))
        evidence_id = add_manual_evidence(
            repository,
            configuration.app(app_id),
            title=redactor.redact_text(title),
            body=redactor.redact_text(body),
            evidence_type=evidence_type,
        )
        _emit({"evidence_id": evidence_id, "selection_biased": True})
    finally:
        connection.close()


@app.command()
def attest(
    contribution_id: str,
    claim: str,
    statement: str,
    source_note: str | None = None,
    config: ConfigOption = None,
) -> None:
    _decision(
        "attest_claim",
        contribution_id,
        {"claim": claim, "statement": statement, "source_note": source_note},
        config,
    )


def _rebuild(
    app_id: str, config_path: Path | None, *, refs: bool, candidates: bool
) -> dict[str, int]:
    configuration, connection, repository = _open(config_path)
    try:
        configured_app = configuration.app(app_id)
        result: dict[str, int] = {}
        if refs:
            result["references"] = rebuild_references(configured_app, repository)
        if candidates:
            result["candidates"] = rebuild_candidates(app_id, repository)
        return result
    finally:
        connection.close()


@rebuild_app.command("references")
def rebuild_refs(app_id: str, config: ConfigOption = None) -> None:
    _emit(_rebuild(app_id, config, refs=True, candidates=False))


@rebuild_app.command("candidates")
def rebuild_candidate_groups(app_id: str, config: ConfigOption = None) -> None:
    _emit(_rebuild(app_id, config, refs=False, candidates=True))


@rebuild_app.command("all")
def rebuild_all(app_id: str, config: ConfigOption = None) -> None:
    _emit(_rebuild(app_id, config, refs=True, candidates=True))


@app.command("export")
def export_command(app_id: str, destination: Path, config: ConfigOption = None) -> None:
    configuration, connection, _ = _open(config)
    try:
        configuration.app(app_id)
        _emit({"objects": export_app(connection, app_id, destination), "path": str(destination)})
    finally:
        connection.close()


@app.command()
def backup(destination: Path | None = None, config: ConfigOption = None) -> None:
    configuration = load_config(config)
    result = backup_database(configuration.database_path, destination)
    _emit({"backup": str(result) if result else None})


@app.command()
def purge(
    yes: Annotated[bool, typer.Option("--yes", help="Confirm permanent local deletion.")] = False,
    config: ConfigOption = None,
) -> None:
    if not yes:
        raise typer.BadParameter("purge requires --yes")
    configuration = load_config(config)
    removed: list[str] = []
    for path in (
        configuration.database_path,
        configuration.database_path.with_name(configuration.database_path.name + "-wal"),
        configuration.database_path.with_name(configuration.database_path.name + "-shm"),
        configuration.data_directory / "email-hmac.key",
    ):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    for path in configuration.data_directory.glob("*.backup"):
        path.unlink()
        removed.append(str(path))
    _emit(
        {
            "removed": removed,
            "recoverable": True,
            "retained": "Exports and custom backups outside the managed data directory remain.",
        }
    )


@app.command("serve-mcp")
def serve_mcp(config: ConfigOption = None) -> None:
    from worktrace.mcp_server.server import run

    run(config)


@app.command()
def packet(candidate_id: str, config: ConfigOption = None) -> None:
    from worktrace.packets.builder import build_phase4_packet

    configuration, connection, _ = _open(config)
    try:
        _emit(build_phase4_packet(connection, candidate_id, configuration))
    finally:
        connection.close()


@app.command()
def gaps(candidate_id: str, config: ConfigOption = None) -> None:
    from worktrace.packets.gaps import list_evidence_gaps

    configuration, connection, _ = _open(config)
    try:
        _emit(list_evidence_gaps(connection, candidate_id, configuration))
    finally:
        connection.close()


def entrypoint() -> None:
    try:
        app()
    except WorkTraceError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc


if __name__ == "__main__":
    entrypoint()
