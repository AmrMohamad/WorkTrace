from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import ExitStack
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
from worktrace.db.authority import (
    authoritative_current_participation_ctes,
    parse_scope,
    run_is_authoritative,
)
from worktrace.db.connection import connect
from worktrace.db.import_status import readiness_contract, source_readiness
from worktrace.db.migrations import backup_database, migrate
from worktrace.db.queries import search_evidence, source_status
from worktrace.db.readiness import DatabaseReadinessStatus, database_readiness
from worktrace.db.repository import EvidenceRepository, stable_id
from worktrace.doctor import run_doctor
from worktrace.domain.models import JsonValue
from worktrace.errors import ConfigurationError, WorkTraceError
from worktrace.identity import (
    finish_identity_rebuild,
    identity_policy_status,
    initialize_identity,
    prepare_identity_import,
    repair_identities,
)
from worktrace.importers.jira_selection import select_jira_seeds
from worktrace.importers.orchestrator import ImportResult, import_snapshot
from worktrace.linking.builder import rebuild_references
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
DateArgument = Annotated[str | None, typer.Argument()]

_TUI_ENVIRONMENT_VARIABLES = (
    "WORKTRACE_JIRA_BASE_URL",
    "WORKTRACE_JIRA_EMAIL",
    "WORKTRACE_JIRA_API_TOKEN",
    "WORKTRACE_GITLAB_BASE_URL",
    "WORKTRACE_GITLAB_TOKEN",
    "WORKTRACE_EMAIL_HMAC_KEY",
    "TEXTUAL",
    "TEXTUAL_DEBUG",
    "TEXTUAL_DRIVER",
    "TEXTUAL_LOG",
    "TEXTUAL_DEVTOOLS_HOST",
    "TEXTUAL_DEVTOOLS_PORT",
    "TEXTUAL_PRESS",
    "TEXTUAL_SCREENSHOT",
    "TEXTUAL_SCREENSHOT_LOCATION",
    "TEXTUAL_SCREENSHOT_FILENAME",
)


def _emit(value: object) -> None:
    typer.echo(json.dumps(value, indent=2, sort_keys=True, default=str))


def _iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise WorkTraceError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def _window(
    configuration: WorkTraceConfig, start: str | None, end: str | None
) -> tuple[date, date]:
    """Admit only a complete configured employment snapshot.

    A sync run is the replacement unit for an authoritative source instance.
    Allowing a later narrow interval would make earlier evidence invisible even
    though it remains stored, so v0.1 deliberately has no partial-window mode.
    """

    if start is None and end is None:
        return configuration.employment_from, configuration.employment_to
    if start is None or end is None:
        raise WorkTraceError("date_from and date_to must be provided together")
    date_from = _iso_date(start, "date_from")
    date_to = _iso_date(end, "date_to")
    if date_from > date_to:
        raise WorkTraceError("date_from must not be after date_to")
    if date_from < configuration.employment_from or date_to > configuration.employment_to:
        raise WorkTraceError("import window is outside the configured employment scope")
    expected = (configuration.employment_from, configuration.employment_to)
    if (date_from, date_to) != expected:
        raise WorkTraceError(
            "unsafe_scope_replacement: WorkTrace v0.1 imports must use the full "
            "configured employment range"
        )
    return date_from, date_to


@dataclass(frozen=True, slots=True)
class _ImportWindow:
    date_from: date
    date_to: date


def _stored_boundary_present(value: object) -> bool:
    return value is not None and value != ""


def _parse_stored_window(
    raw_from: object,
    raw_to: object,
    *,
    source: str,
) -> _ImportWindow:
    has_from = _stored_boundary_present(raw_from)
    has_to = _stored_boundary_present(raw_to)
    if has_from != has_to:
        raise WorkTraceError(
            f"unsafe_scope_replacement: the prior authoritative {source} range is incomplete"
        )
    if not has_from:
        raise WorkTraceError(
            f"unsafe_scope_replacement: the prior authoritative {source} range cannot be verified"
        )
    if not isinstance(raw_from, str) or not isinstance(raw_to, str):
        raise WorkTraceError(
            f"unsafe_scope_replacement: the prior authoritative {source} range is malformed"
        )
    try:
        date_from = _iso_date(raw_from, f"{source} date_from")
        date_to = _iso_date(raw_to, f"{source} date_to")
    except WorkTraceError as exc:
        raise WorkTraceError(
            f"unsafe_scope_replacement: the prior authoritative {source} range is malformed"
        ) from exc
    if date_from > date_to:
        raise WorkTraceError(
            f"unsafe_scope_replacement: the prior authoritative {source} range is reversed"
        )
    return _ImportWindow(date_from, date_to)


def _assert_no_scope_contraction(
    repository: EvidenceRepository,
    app_id: str,
    date_from: date,
    date_to: date,
) -> None:
    """Reject a configuration shrink that would conceal a prior complete run."""

    rows = repository.connection.execute(
        """
        SELECT sr.source, sr.status, sr.completeness, sr.scope_json,
               session.app_id AS session_app_id,
               session.date_from AS session_date_from,
               session.date_to AS session_date_to
        FROM sync_runs sr
        LEFT JOIN import_sessions session ON session.id=sr.import_session_id
        WHERE sr.app_id=?
        """,
        (app_id,),
    )
    for row in rows:
        scope = parse_scope(row["scope_json"])
        if not run_is_authoritative(
            str(row["source"]), str(row["status"]), str(row["completeness"]), scope
        ):
            continue
        if str(row["source"]) == "manual":
            # Manual evidence is additive and never replaces a source snapshot.
            continue
        scope_from = scope.get("date_from")
        scope_to = scope.get("date_to")
        scope_has_from = _stored_boundary_present(scope_from)
        scope_has_to = _stored_boundary_present(scope_to)
        if scope_has_from != scope_has_to:
            raise WorkTraceError(
                "unsafe_scope_replacement: the prior authoritative scope contains "
                "only one range boundary"
            )
        if scope_has_from:
            prior_window = _parse_stored_window(scope_from, scope_to, source="scope")
            if row["session_app_id"] is not None:
                if str(row["session_app_id"]) != app_id:
                    raise WorkTraceError(
                        "unsafe_scope_replacement: parent import session belongs "
                        "to another application"
                    )
                session_window = _parse_stored_window(
                    row["session_date_from"],
                    row["session_date_to"],
                    source="parent session",
                )
                if session_window != prior_window:
                    raise WorkTraceError(
                        "unsafe_scope_replacement: source scope and parent session "
                        "contain contradictory ranges"
                    )
        else:
            if row["session_app_id"] is None or str(row["session_app_id"]) != app_id:
                raise WorkTraceError(
                    "unsafe_scope_replacement: the historical range cannot be verified "
                    "because no valid same-application parent session exists"
                )
            prior_window = _parse_stored_window(
                row["session_date_from"],
                row["session_date_to"],
                source="parent session",
            )
        if prior_window.date_from < date_from or prior_window.date_to > date_to:
            raise WorkTraceError(
                "unsafe_scope_replacement: configured employment range would hide "
                "a prior authoritative import"
            )


def _source_instance(app_id: str, source: str, identifier: object) -> str:
    return stable_id("source", app_id, source, identifier)


def _open(
    config_path: Path | None = None,
) -> tuple[WorkTraceConfig, sqlite3.Connection, EvidenceRepository]:
    config = load_config(config_path)
    if not config.database_path.is_file():
        raise WorkTraceError("database is missing; run worktrace init")
    connection = connect(config.database_path)
    try:
        if database_readiness(connection).status is not DatabaseReadinessStatus.READY:
            raise WorkTraceError("unsupported database schema; run the matching worktrace init")
        redactor = Redactor(email_hmac_key(config.data_directory, create=False))
        return config, connection, EvidenceRepository(connection, redactor)
    except BaseException:
        connection.close()
        raise


@app.callback()
def main() -> None:
    """WorkTrace keeps source observations separate from human claims."""


@app.command()
def version() -> None:
    """Print the WorkTrace version."""
    typer.echo(__version__)


def _sanitize_tui_environment() -> None:
    for name in _TUI_ENVIRONMENT_VARIABLES:
        os.environ.pop(name, None)


@app.command("ui")
def launch_ui(
    app_id: Annotated[str | None, typer.Option("--app")] = None,
    candidate_id: Annotated[str | None, typer.Option("--candidate")] = None,
    config: ConfigOption = None,
) -> None:
    """Review contribution evidence in an interactive, read-only terminal UI."""

    if candidate_id is not None and app_id is None:
        typer.echo("error: --candidate requires --app", err=True)
        raise typer.Exit(2)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        typer.echo("error: worktrace ui requires an interactive terminal", err=True)
        typer.echo("Use `worktrace --help` for non-interactive commands.", err=True)
        raise typer.Exit(2)

    try:
        configuration = load_config(config)
        if app_id is not None:
            configuration.app(app_id)
        if candidate_id is not None:
            from worktrace.mcp_server.schemas import stable_id

            stable_id(candidate_id, "candidate_id")
    except WorkTraceError as exc:
        typer.echo(f"error: {exc}", err=True)
        typer.echo("Run `worktrace doctor` from the CLI, then retry.", err=True)
        raise typer.Exit(1) from exc

    if not configuration.database_path.is_file():
        typer.echo("error: WorkTrace has not been initialized", err=True)
        typer.echo("Run `worktrace init`, then `worktrace doctor`.", err=True)
        raise typer.Exit(1)

    _sanitize_tui_environment()

    from worktrace.read_workspace import ReadOnlyWorkspace
    from worktrace.tui.app import run_worktrace_ui

    run_worktrace_ui(
        ReadOnlyWorkspace(configuration),
        initial_app_id=app_id,
        initial_candidate_id=candidate_id,
    )


@app.command("init")
def initialize(config: ConfigOption = None) -> None:
    """Create or upgrade the private local ledger idempotently."""
    configuration = load_config(config)
    ensure_private_directory(configuration.data_directory)
    existing = (
        configuration.database_path.exists() and configuration.database_path.stat().st_size > 0
    )
    key = email_hmac_key(configuration.data_directory, create=not existing)
    connection = connect(configuration.database_path)
    try:
        binding_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='identity_key_binding'"
        ).fetchone()
        if binding_table and connection.execute("SELECT 1 FROM identity_key_binding").fetchone():
            with connection:
                initialize_identity(connection, configuration, key)
        applied = migrate(connection, configuration.database_path)
        repository = EvidenceRepository(connection)
        repository.ensure_apps(configuration)
        populated = any(
            connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            for table in (
                "actors",
                "source_objects",
                "observations",
                "human_decisions",
                "sync_runs",
            )
        )
        if not populated:
            with connection:
                initialize_identity(connection, configuration, key)
        identity = {
            item.id: identity_policy_status(connection, configuration, item.id)
            for item in configuration.apps
        }
    finally:
        connection.close()
    configuration.database_path.chmod(0o600)
    _emit(
        {
            "database": str(configuration.database_path),
            "migrations_applied": applied,
            "identity": identity,
        }
    )


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


def _verified_repair_identities(
    configuration: WorkTraceConfig, app_id: str, key: bytes
) -> tuple[dict[str, str], frozenset[str]]:
    """Run only for the explicit --verify-providers repair option."""
    selected = configuration.app(app_id)
    verified: dict[str, str] = {}
    verified_emails: frozenset[str] = frozenset()
    if selected.jira_project_keys:
        credentials = jira_credentials()
        if credentials is None or configuration.identity.jira_account_id is None:
            raise WorkTraceError("Jira credentials and account ID are required for verification")
        with httpx.Client(
            base_url=credentials.base_url,
            auth=(credentials.email, credentials.token),
            timeout=30,
        ) as client:
            adapter = JiraAdapter(
                JiraConfig(
                    work_timezone=configuration.employment_timezone,
                    base_url=credentials.base_url,
                    source_instance=_source_instance(app_id, "jira", credentials.base_url),
                    app_id=app_id,
                    project_keys=selected.jira_project_keys,
                    account_id=configuration.identity.jira_account_id,
                    date_from=configuration.employment_from,
                    date_to=configuration.employment_to,
                    email_key=key,
                ),
                client,
            )
            verified["jira"] = adapter.resolved_self_id()
    if selected.gitlab_project_ids:
        credentials_gitlab = gitlab_credentials()
        if credentials_gitlab is None:
            raise WorkTraceError("GitLab credentials are required for verification")
        with httpx.Client(
            base_url=credentials_gitlab.base_url,
            headers={"PRIVATE-TOKEN": credentials_gitlab.token, "Accept": "application/json"},
            timeout=30,
        ) as client:
            gitlab_adapter = GitLabAdapter(
                GitLabConfig(
                    base_url=credentials_gitlab.base_url,
                    source_instance=_source_instance(
                        app_id, "gitlab", selected.gitlab_project_ids[0]
                    ),
                    app_id=app_id,
                    project_id=selected.gitlab_project_ids[0],
                    user_id=configuration.identity.gitlab_user_id,
                    username=configuration.identity.gitlab_username,
                    date_from=configuration.employment_from,
                    date_to=configuration.employment_to,
                    email_key=key,
                ),
                client,
            )
            ids = gitlab_adapter.resolved_self_ids(_git_self_ids(configuration, key))
            verified["gitlab"] = next(value for value in ids if value.isdecimal())
            verified_emails = frozenset(
                value for value in ids if value.startswith("email_hmac_sha256:")
            )
    return verified, verified_emails


@app.command("repair-identities")
def repair_identity_command(
    app_id: str,
    apply: Annotated[bool, typer.Option("--apply")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    proof_actor_id: Annotated[str | None, typer.Option("--proof-actor-id")] = None,
    proof_alias_index: Annotated[int | None, typer.Option("--proof-alias-index", min=0)] = None,
    expected_proposal: Annotated[str | None, typer.Option("--expected-proposal")] = None,
    verify_providers: Annotated[
        bool,
        typer.Option("--verify-providers", help="Explicitly verify provider identities online."),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Preview identity reconciliation; apply only explicitly approved changes."""
    if apply and dry_run:
        raise WorkTraceError("choose --apply or --dry-run, not both")
    configuration, connection, _ = _open(config)
    try:
        configuration.app(app_id)
        key = email_hmac_key(configuration.data_directory, create=False)
        verified, verified_emails = (
            _verified_repair_identities(configuration, app_id, key)
            if verify_providers
            else ({}, frozenset())
        )
        with connection:
            result = repair_identities(
                connection,
                configuration,
                key,
                app_id,
                apply=apply,
                proof_actor_id=proof_actor_id,
                proof_alias_index=proof_alias_index,
                expected_proposal=expected_proposal,
                verified_self_ids=verified,
                verified_gitlab_email_hashes=verified_emails,
            )
        _emit(result)
    finally:
        connection.close()


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
            WITH {authoritative_current_participation_ctes()}
            SELECT DISTINCT p.source_object_id
            FROM authoritative_current_participations p
            JOIN actors a ON a.id=p.actor_id
            JOIN source_objects so ON so.id=p.source_object_id
            WHERE so.app_id=? AND so.source='git' AND so.kind='git_commit'
              AND a.is_self=1 AND a.identity_policy_version=1
              AND p.role IN ('git_author', 'git_coauthor')
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


def _run_source_import(
    configuration: WorkTraceConfig,
    repository: EvidenceRepository,
    configured_app: AppConfig,
    source: str,
    identifier: Path | int | None,
    start: date,
    end: date,
    session_id: str,
    *,
    explicit_jira_keys: tuple[str, ...] = (),
    approve_selector_replacement: str | None = None,
    previous_results: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Establish preparation and identity before creating a real source run."""
    app_id = configured_app.id
    target = (
        str(identifier)
        if source == "gitlab"
        else (_source_instance(app_id, source, identifier) if source == "git" else "jira")
    )
    reason = "identity_policy_unready"
    started = False
    try:
        with ExitStack() as stack:
            credentials = None
            if source in {"jira", "gitlab"}:
                reason = "credentials_missing"
                credentials = jira_credentials() if source == "jira" else gitlab_credentials()
                if credentials is None:
                    raise ConfigurationError("Provider credentials are not configured")
                reason = "identity_missing"
                if source == "jira" and not configuration.identity.jira_account_id:
                    raise ConfigurationError("Jira identity is required")
                if source == "gitlab" and (
                    configuration.identity.gitlab_user_id is None
                    and configuration.identity.gitlab_username is None
                ):
                    raise ConfigurationError("GitLab identity is required")
            reason = "identity_policy_unready"
            key = email_hmac_key(configuration.data_directory, create=False)
            with repository.connection:
                prepare_identity_import(repository.connection, configuration, key, app_id)
            scope: dict[str, JsonValue] = {}
            counts: dict[str, int] = {}
            own_ids = _git_self_ids(configuration, key)
            reason = "source_configuration_invalid"
            adapter: LocalGitAdapter | GitLabAdapter | JiraAdapter
            if source == "git":
                assert isinstance(identifier, Path)
                scoped_repo = configured_app.assert_repo_scope(identifier)
                source_instance = _source_instance(app_id, source, scoped_repo)
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
                scope["selection_reasons"] = ["configured repository read-only snapshot"]
            elif source == "gitlab":
                assert credentials is not None and isinstance(identifier, int)
                source_instance = _source_instance(app_id, source, identifier)
                seeds = _relevant_git_commit_shas(repository, app_id)
                counts = {
                    "relevant_local_commit_shas": len(seeds.values),
                    "relevant_local_commit_shas_total": seeds.total_count,
                    "relevant_local_commit_shas_dropped": seeds.dropped_count,
                }
                reason = "origin_invalid"
                client = stack.enter_context(
                    httpx.Client(
                        base_url=credentials.base_url,
                        headers={"PRIVATE-TOKEN": credentials.token, "Accept": "application/json"},
                        timeout=30,
                    )
                )
                gitlab_adapter = GitLabAdapter(
                    GitLabConfig(
                        base_url=credentials.base_url,
                        source_instance=source_instance,
                        app_id=app_id,
                        project_id=identifier,
                        email_key=key,
                        date_from=start,
                        date_to=end,
                        jira_project_keys=configured_app.jira_project_keys,
                        user_id=configuration.identity.gitlab_user_id,
                        username=configuration.identity.gitlab_username,
                        production_environments=configured_app.production_environments,
                        relevant_commit_shas=seeds.values,
                    ),
                    client,
                )
                reason = "identity_unverified"
                own_ids = gitlab_adapter.resolved_self_ids(own_ids)
                adapter = gitlab_adapter
                scope = {
                    **_commit_seed_scope(seeds),
                    "selection_reasons": [
                        "configured identity authored/assigned/reviewed merge requests",
                        "current self-authored/coauthored local commits",
                    ],
                }
            else:
                assert credentials is not None
                selection = select_jira_seeds(
                    repository,
                    configured_app,
                    configuration=configuration,
                    explicit_keys=explicit_jira_keys,
                )
                counts = {"exact_jira_keys": len(selection.keys)}
                source_instance = _source_instance(app_id, source, credentials.base_url)
                reason = "origin_invalid"
                # This branch has JiraCredentials; keep the union's narrowing explicit.
                jira_auth = jira_credentials()
                assert jira_auth is not None
                client = stack.enter_context(
                    httpx.Client(
                        base_url=jira_auth.base_url,
                        auth=(jira_auth.email, jira_auth.token),
                        headers={"Accept": "application/json"},
                        timeout=30,
                    )
                )
                jira_adapter = JiraAdapter(
                    JiraConfig(
                        work_timezone=configuration.employment_timezone,
                        base_url=jira_auth.base_url,
                        source_instance=source_instance,
                        app_id=app_id,
                        project_keys=configured_app.jira_project_keys,
                        email_key=key,
                        date_from=start,
                        date_to=end,
                        account_id=configuration.identity.jira_account_id,
                        discovered_issue_keys=selection.keys,
                    ),
                    client,
                )
                reason = "identity_unverified"
                own_ids = {jira_adapter.resolved_self_id()}
                adapter = jira_adapter
                scope = {
                    "selection_policy_version": 3,
                    "exact_jira_key_count": len(selection.keys),
                    "jira_seed_selection": cast(JsonValue, selection.as_dict()),
                    "limitations": selection.as_dict()["limitations"],
                    "selection_reasons": [
                        "configured identity participation",
                        "selected exact-key roots",
                    ],
                    "known_selection_bias": "Deleted or inaccessible activity may be unavailable.",
                }
            if source in {"jira", "gitlab"}:
                retained = source_readiness(repository.connection, app_id)
                upstream: list[dict[str, object]] = []
                for item in previous_results or []:
                    previous_source = str(item.get("source"))
                    if previous_source not in ({"git", "gitlab"} if source == "jira" else {"git"}):
                        continue
                    if item.get("status") == "complete":
                        continue
                    expected_instance = item.get("source_instance") or (
                        item.get("target")
                        if previous_source == "git"
                        else _source_instance(app_id, "gitlab", item.get("target"))
                    )
                    known = retained.get(previous_source, {}).get(
                        "last_authoritative_snapshots", []
                    )
                    snapshots = (
                        [
                            snapshot
                            for snapshot in known
                            if isinstance(snapshot, dict)
                            and snapshot.get("source_instance") == expected_instance
                        ]
                        if isinstance(known, list)
                        else []
                    )
                    upstream.append(
                        {
                            "source": previous_source,
                            "run_id": item.get("run_id"),
                            "status": item.get("status"),
                            "snapshots": snapshots,
                            "input_authority": "previous_authority"
                            if snapshots
                            else "no_authoritative_input",
                        }
                    )
                scope["seed_input_authority"] = (
                    cast(JsonValue, upstream) if upstream else "current_authority"
                )
                if upstream:
                    previous_limitations = scope.get("limitations", [])
                    scope["limitations"] = [
                        *(previous_limitations if isinstance(previous_limitations, list) else []),
                        "An upstream import did not complete; discovery uses only explicitly "
                        "retained authority where available.",
                    ]
            started = True
            if source == "jira":
                result = import_snapshot(
                    configured_app,
                    adapter,
                    repository,
                    source=source,
                    source_instance=source_instance,
                    date_from=start,
                    date_to=end,
                    self_actor_ids=own_ids,
                    import_session_id=session_id,
                    finish_session=False,
                    scope_details=scope,
                    configuration=configuration,
                    expected_selector_replacement=approve_selector_replacement,
                )
            else:
                result = import_snapshot(
                    configured_app,
                    adapter,
                    repository,
                    source=source,
                    source_instance=source_instance,
                    date_from=start,
                    date_to=end,
                    self_actor_ids=own_ids,
                    import_session_id=session_id,
                    finish_session=False,
                    scope_details=scope,
                )
        return {
            **_import_summary(result, discovery_counts=counts),
            "source": source,
            "target": target,
            "source_instance": source_instance,
            **({"jira_seed_selection": scope["jira_seed_selection"]} if source == "jira" else {}),
            "seed_input_authority": scope.get("seed_input_authority"),
            "preflight": {"stage": "preflight", "status": "ready", "reason": None},
            "requested_scope": {
                "app_id": app_id,
                "source": source,
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
            },
            **readiness_contract(
                repository.connection,
                app_id,
                source,
                result.status,
                result.completeness,
                source_instance=source_instance,
            ),
        }
    except (WorkTraceError, httpx.HTTPError, ValueError) as exc:
        if started:
            raise
        # Provider exception text can include credentials or private URLs; persist a category only.
        if isinstance(exc, httpx.HTTPError):
            reason = "provider_unavailable"
        return {
            "session_id": session_id,
            "source": source,
            "target": target,
            "source_instance": None,
            "stage": "preflight",
            "status": "not_started",
            "reason": reason,
            "completeness": "unknown",
            "preflight": {"stage": "preflight", "status": "not_started", "reason": reason},
            "requested_scope": {
                "app_id": app_id,
                "source": source,
                "date_from": start.isoformat(),
                "date_to": end.isoformat(),
            },
            **readiness_contract(repository.connection, app_id, source, "not_started", "unknown"),
        }


def _finish_source_session(
    repository: EvidenceRepository,
    session_id: str,
    results: list[dict[str, object]],
    *,
    derived_current: bool = False,
) -> str:
    complete = all(item["status"] == "complete" for item in results)
    overall = (
        "complete"
        if complete
        else "partial"
        if any(item["status"] == "complete" for item in results)
        else "failed"
    )
    for item in results:
        item["derived_data"] = "current" if derived_current else "requires_rebuild"
        if derived_current and item["coverage"] == "no-known-omissions":
            item["agent_review"] = "available"
    repository.finish_import_session(
        session_id, overall, {"sources": cast(list[JsonValue], results)}
    )
    return overall


def _import_one(
    app_id: str,
    source: str,
    identifier: Path | int | None,
    date_from: str | None,
    date_to: str | None,
    config: Path | None,
    *,
    jira_keys: tuple[str, ...] = (),
    approve_selector_replacement: str | None = None,
) -> None:
    configuration, connection, repository = _open(config)
    session_id: str | None = None
    try:
        start, end = _window(configuration, date_from, date_to)
        configured_app = configuration.app(app_id)
        _assert_no_scope_contraction(repository, app_id, start, end)
        if source == "gitlab" and not configured_app.allows_gitlab_project(cast(int, identifier)):
            raise WorkTraceError("GitLab project is not configured for this app")
        session_id = repository.create_import_session(configured_app, start, end)
        result = _run_source_import(
            configuration,
            repository,
            configured_app,
            source,
            identifier,
            start,
            end,
            session_id,
            explicit_jira_keys=jira_keys,
            approve_selector_replacement=approve_selector_replacement,
        )
        _finish_source_session(repository, session_id, [result])
        _emit(result)
        if result["status"] != "complete":
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except Exception:
        if session_id is not None:
            connection.rollback()
            repository.finish_import_session(
                session_id, "failed", {"error": "source_execution_failed"}
            )
        raise
    finally:
        connection.close()


@import_app.command("git")
def import_git(
    app_id: str,
    repo: Path,
    date_from: DateArgument = None,
    date_to: DateArgument = None,
    config: ConfigOption = None,
) -> None:
    _import_one(app_id, "git", repo, date_from, date_to, config)


@import_app.command("jira")
def import_jira(
    app_id: str,
    date_from: DateArgument = None,
    date_to: DateArgument = None,
    config: ConfigOption = None,
    jira_key: Annotated[list[str] | None, typer.Option("--jira-key")] = None,
    approve_selector_replacement: Annotated[
        str | None, typer.Option("--approve-selector-replacement")
    ] = None,
) -> None:
    _import_one(
        app_id,
        "jira",
        None,
        date_from,
        date_to,
        config,
        jira_keys=tuple(jira_key or ()),
        approve_selector_replacement=approve_selector_replacement,
    )


@import_app.command("gitlab")
def import_gitlab(
    app_id: str,
    project_id: int,
    date_from: DateArgument = None,
    date_to: DateArgument = None,
    config: ConfigOption = None,
) -> None:
    _import_one(app_id, "gitlab", project_id, date_from, date_to, config)


@import_app.command("all")
def import_all(
    app_id: str,
    date_from: DateArgument = None,
    date_to: DateArgument = None,
    config: ConfigOption = None,
    jira_key: Annotated[list[str] | None, typer.Option("--jira-key")] = None,
    approve_selector_replacement: Annotated[
        str | None, typer.Option("--approve-selector-replacement")
    ] = None,
) -> None:
    """Import configured sources independently, retaining each preparation result."""
    configuration, connection, repository = _open(config)
    session_id: str | None = None
    results: list[dict[str, object]] = []
    try:
        start, end = _window(configuration, date_from, date_to)
        configured_app = configuration.app(app_id)
        _assert_no_scope_contraction(repository, app_id, start, end)
        session_id = repository.create_import_session(configured_app, start, end)
        sources: list[tuple[str, Path | int | None]] = [
            *(("git", repo) for repo in configured_app.repo_paths),
            *(("gitlab", project) for project in configured_app.gitlab_project_ids),
            *([("jira", None)] if configured_app.jira_project_keys else []),
        ]
        for source, identifier in sources:
            results.append(
                _run_source_import(
                    configuration,
                    repository,
                    configured_app,
                    source,
                    identifier,
                    start,
                    end,
                    session_id,
                    explicit_jira_keys=tuple(jira_key or ()),
                    approve_selector_replacement=approve_selector_replacement,
                    previous_results=results,
                )
            )
            # Keep preparation audit durable even if a later source or rebuild is interrupted.
            with connection:
                connection.execute(
                    "UPDATE import_sessions SET summary_json=? WHERE id=?",
                    (json.dumps({"sources": results}, sort_keys=True), session_id),
                )
        derived_current = bool(identity_policy_status(connection, configuration, app_id)["valid"])
        reference_count = candidate_count = 0
        if derived_current:
            reference_count = rebuild_references(configured_app, repository)
            candidate_count = rebuild_candidates(app_id, repository)
            with connection:
                finish_identity_rebuild(connection, configuration, app_id)
        overall = _finish_source_session(
            repository, session_id, results, derived_current=derived_current
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
        if overall != "complete":
            raise typer.Exit(2)
    except typer.Exit:
        raise
    except Exception:
        if session_id is not None:
            connection.rollback()
            repository.finish_import_session(
                session_id,
                "partial"
                if any(item.get("status") == "complete" for item in results)
                else "failed",
                {"sources": cast(list[JsonValue], results), "error": "source_or_rebuild_failed"},
            )
        raise
    finally:
        connection.close()


@app.command()
def status(app_id: str, config: ConfigOption = None) -> None:
    configuration, connection, _ = _open(config)
    try:
        configuration.app(app_id)
        _emit(
            {
                "app_id": app_id,
                "sources": source_status(connection, app_id),
                "identity": identity_policy_status(connection, configuration, app_id),
            }
        )
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
        if not identity_policy_status(connection, configuration, app_id)["valid"]:
            raise WorkTraceError("identity policy requires repair before rebuilding")
        result: dict[str, int] = {}
        if refs:
            result["references"] = rebuild_references(configured_app, repository)
        if candidates:
            result["candidates"] = rebuild_candidates(app_id, repository)
        if refs and candidates:
            with connection:
                finish_identity_rebuild(connection, configuration, app_id)
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
        _emit(
            {
                "objects": export_app(
                    connection,
                    app_id,
                    destination,
                    identity_state=identity_policy_status(connection, configuration, app_id),
                ),
                "path": str(destination),
            }
        )
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
