"""Local and explicitly authorized live diagnostics for WorkTrace."""

from __future__ import annotations

import importlib.metadata
import os
import sqlite3
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx

from worktrace.adapters.gitlab import GitLabAdapter
from worktrace.adapters.jira import JiraAdapter
from worktrace.config import WorkTraceConfig, gitlab_credentials, jira_credentials
from worktrace.db.connection import connect_read_only
from worktrace.db.migrations import migrations, user_version
from worktrace.errors import ConfigurationError

CheckStatus = Literal["pass", "warn", "fail", "skipped"]


def _check(
    name: str,
    scope: str,
    status: CheckStatus,
    message: str,
    remediation: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "scope": scope,
        "status": status,
        "message": message,
        "remediation": remediation,
        "ok": status in {"pass", "warn", "skipped"},
    }


def _git(args: list[str], root: Path | None = None) -> subprocess.CompletedProcess[str]:
    command = ["git", "--no-optional-locks"]
    if root is not None:
        command.extend(("-C", str(root)))
    command.extend(args)
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        shell=False,
        env=environment,
    )


def _storage_checks(config: WorkTraceConfig) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    directory = config.data_directory
    if not directory.is_dir():
        result.append(
            _check(
                "storage_writable",
                "local",
                "fail",
                "Configured data directory does not exist.",
                "Run worktrace init.",
            )
        )
    else:
        directory_stat = directory.stat()
        directory_mode = stat.S_IMODE(directory_stat.st_mode)
        directory_status: CheckStatus = (
            "pass"
            if directory_stat.st_uid == os.getuid() and directory_mode & 0o077 == 0
            else "fail"
        )
        result.append(
            _check(
                "storage_permissions",
                "local",
                directory_status,
                "Data directory has private owner-only permissions."
                if directory_status == "pass"
                else "Data directory is accessible beyond its owner.",
                None if directory_status == "pass" else "Set the data directory mode to 0700.",
            )
        )
        try:
            descriptor, path = tempfile.mkstemp(prefix=".worktrace-doctor-", dir=directory)
            os.close(descriptor)
            Path(path).unlink()
        except OSError:
            result.append(
                _check(
                    "storage_writable",
                    "local",
                    "fail",
                    "Configured data directory is not writable.",
                    "Fix ownership and private directory permissions.",
                )
            )
        else:
            result.append(
                _check(
                    "storage_writable",
                    "local",
                    "pass",
                    "Private storage accepted an ephemeral write/delete probe.",
                )
            )

    key = directory / "email-hmac.key"
    if os.environ.get("WORKTRACE_EMAIL_HMAC_KEY"):
        result.append(
            _check(
                "email_hash_key",
                "local",
                "pass",
                "Email HMAC key is supplied by the environment; its value was not read or printed.",
            )
        )
    elif not key.is_file():
        result.append(
            _check(
                "email_hash_key",
                "local",
                "fail",
                "Email HMAC key is missing.",
                "Run worktrace init.",
            )
        )
    else:
        mode = stat.S_IMODE(key.stat().st_mode)
        status: CheckStatus = "pass" if mode & 0o077 == 0 else "fail"
        result.append(
            _check(
                "email_hash_key",
                "local",
                status,
                "Email HMAC key has private permissions."
                if status == "pass"
                else "Email HMAC key is accessible beyond its owner.",
                None if status == "pass" else "Set the key file mode to 0600.",
            )
        )
    return result


def _database_checks(config: WorkTraceConfig) -> list[dict[str, object]]:
    if not config.database_path.is_file():
        return [
            _check(
                "database", "local", "fail", "WorkTrace database is missing.", "Run worktrace init."
            )
        ]
    database_stat = config.database_path.stat()
    database_mode = stat.S_IMODE(database_stat.st_mode)
    permission_status: CheckStatus = (
        "pass" if database_stat.st_uid == os.getuid() and database_mode & 0o077 == 0 else "fail"
    )
    permissions = _check(
        "database_permissions",
        "local",
        permission_status,
        "Database has private owner-only permissions."
        if permission_status == "pass"
        else "Database is accessible beyond its owner.",
        None if permission_status == "pass" else "Set the database file mode to 0600.",
    )
    try:
        connection = connect_read_only(config.database_path)
        try:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            current = user_version(connection)
            expected = len(migrations())
            query_only = int(connection.execute("PRAGMA query_only").fetchone()[0]) == 1
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as exc:
        return [
            permissions,
            _check(
                "database",
                "local",
                "fail",
                f"Database could not be opened read-only: {type(exc).__name__}.",
                "Restore a valid backup or run worktrace init after preserving the current file.",
            ),
        ]
    status: CheckStatus = (
        "pass" if integrity == "ok" and current == expected and query_only else "fail"
    )
    return [
        permissions,
        _check(
            "database",
            "local",
            status,
            (
                f"SQLite quick_check={integrity}; schema={current}/{expected}; "
                f"query_only={query_only}."
            ),
            None
            if status == "pass"
            else "Run worktrace init to apply migrations; restore from backup if integrity failed.",
        ),
    ]


def _git_checks(config: WorkTraceConfig) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    try:
        version = _git(["--version"])
    except (OSError, subprocess.TimeoutExpired):
        version = None
    if version is None or version.returncode != 0:
        return [
            _check(
                "git_version",
                "local",
                "fail",
                "Git is unavailable.",
                "Install Git and ensure it is on PATH.",
            )
        ]
    result.append(_check("git_version", "local", "pass", version.stdout.strip()[:100]))
    for app in config.apps:
        for root in app.repo_paths:
            scope = f"app:{app.id}:git"
            resolved = _git(["rev-parse", "--show-toplevel"], root)
            exact = resolved.returncode == 0 and Path(resolved.stdout.strip()).resolve() == root
            result.append(
                _check(
                    "git_repository",
                    scope,
                    "pass" if exact else "fail",
                    "Configured path is the exact Git worktree root."
                    if exact
                    else "Configured path is missing, unreadable, or not the exact Git root.",
                    None if exact else "Correct apps.repo_paths in the WorkTrace configuration.",
                )
            )
            if not exact:
                continue
            origin = _git(["config", "--get", "remote.origin.url"], root)
            result.append(
                _check(
                    "git_origin",
                    scope,
                    "pass" if origin.returncode == 0 and bool(origin.stdout.strip()) else "warn",
                    "Origin is configured; its value was not printed."
                    if origin.returncode == 0 and origin.stdout.strip()
                    else "No origin is configured; local Git import remains valid.",
                )
            )
            refs = _git(
                [
                    "for-each-ref",
                    "--count=1",
                    "--sort=-committerdate",
                    "--format=%(committerdate:iso-strict)",
                    "refs/heads",
                    "refs/remotes",
                ],
                root,
            )
            result.append(
                _check(
                    "git_clone_local_freshness",
                    scope,
                    "warn" if refs.returncode != 0 or not refs.stdout.strip() else "pass",
                    "Latest local/ref observation: "
                    + (refs.stdout.strip()[:64] or "unknown")
                    + ". No fetch was performed; remote freshness is unverified.",
                )
            )
    return result


def _dependency_checks() -> list[dict[str, object]]:
    try:
        version = importlib.metadata.version("mcp")
        major = int(version.split(".", 1)[0])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return [
            _check(
                "mcp_dependency",
                "local",
                "fail",
                "The bounded MCP dependency is unavailable.",
                "Run uv sync --locked.",
            )
        ]
    return [
        _check(
            "mcp_dependency",
            "local",
            "pass" if major == 2 else "fail",
            f"mcp {version} is installed; required major version is 2.",
            None if major == 2 else "Run uv sync --locked using the committed lockfile.",
        )
    ]


def _credential_checks(config: WorkTraceConfig) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    needs_jira = any(app.jira_project_keys for app in config.apps)
    needs_gitlab = any(app.gitlab_project_ids for app in config.apps)
    for name, needed, loader in (
        ("jira_credentials", needs_jira, jira_credentials),
        ("gitlab_credentials", needs_gitlab, gitlab_credentials),
    ):
        if not needed:
            result.append(
                _check(name, "local", "skipped", "Provider is not configured for any app.")
            )
            continue
        try:
            credentials = loader()
        except ConfigurationError as exc:
            result.append(
                _check(
                    name,
                    "local",
                    "fail",
                    str(exc),
                    "Set every required credential environment variable.",
                )
            )
            continue
        result.append(
            _check(
                name,
                "local",
                "pass" if credentials is not None else "fail",
                "Required credential variables are present; values were not printed."
                if credentials is not None
                else "Required credential variables are missing.",
                None
                if credentials is not None
                else "Set the documented provider environment variables.",
            )
        )
    return result


def _live_checks(config: WorkTraceConfig) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    try:
        jira = jira_credentials()
    except ConfigurationError as exc:
        result.append(
            _check(
                "jira_live",
                "live:jira",
                "fail",
                f"Jira live validation failed: {type(exc).__name__}.",
                "Set every required Jira credential environment variable.",
            )
        )
        jira = None
    if jira is not None:
        try:
            JiraAdapter._origin(jira.base_url)
            with httpx.Client(
                base_url=jira.base_url,
                auth=(jira.email, jira.token),
                timeout=15,
                headers={"Accept": "application/json"},
            ) as client:
                identity = client.get("/rest/api/3/myself")
                identity.raise_for_status()
                actual = str(identity.json().get("accountId", ""))
                expected = config.identity.jira_account_id or ""
                result.append(
                    _check(
                        "jira_identity",
                        "live:jira",
                        "pass" if actual and actual == expected else "fail",
                        "Jira identity matches configured account ID."
                        if actual and actual == expected
                        else "Jira identity does not match configured account ID.",
                        None
                        if actual and actual == expected
                        else "Correct identity.jira_account_id or credentials.",
                    )
                )
                for app in config.apps:
                    for key in app.jira_project_keys:
                        response = client.get(f"/rest/api/3/project/{quote(key, safe='')}")
                        result.append(
                            _check(
                                "jira_project_visibility",
                                f"live:app:{app.id}:jira:{key}",
                                "pass" if response.status_code == 200 else "fail",
                                "Configured Jira project is visible."
                                if response.status_code == 200
                                else (
                                    "Configured Jira project visibility failed with HTTP "
                                    f"{response.status_code}."
                                ),
                                None
                                if response.status_code == 200
                                else "Verify project scope and account permissions.",
                            )
                        )
        except (ConfigurationError, httpx.HTTPError, ValueError) as exc:
            result.append(
                _check(
                    "jira_live",
                    "live:jira",
                    "fail",
                    f"Jira live validation failed: {type(exc).__name__}.",
                    "Check provider availability, credentials, and configured origin.",
                )
            )
    try:
        gitlab = gitlab_credentials()
    except ConfigurationError as exc:
        result.append(
            _check(
                "gitlab_live",
                "live:gitlab",
                "fail",
                f"GitLab live validation failed: {type(exc).__name__}.",
                "Set every required GitLab credential environment variable.",
            )
        )
        gitlab = None
    if gitlab is not None:
        try:
            GitLabAdapter._origin(gitlab.base_url)
            with httpx.Client(
                base_url=gitlab.base_url,
                headers={"PRIVATE-TOKEN": gitlab.token, "Accept": "application/json"},
                timeout=15,
            ) as client:
                identity = client.get("/api/v4/user")
                identity.raise_for_status()
                document = identity.json()
                expected_id = config.identity.gitlab_user_id
                expected_name = config.identity.gitlab_username
                matches = (expected_id is None or document.get("id") == expected_id) and (
                    expected_name is None
                    or str(document.get("username", "")).casefold() == expected_name.casefold()
                )
                result.append(
                    _check(
                        "gitlab_identity",
                        "live:gitlab",
                        "pass" if matches else "fail",
                        "GitLab identity matches configured user."
                        if matches
                        else "GitLab identity does not match configured user.",
                        None
                        if matches
                        else "Correct identity.gitlab_user_id/username or credentials.",
                    )
                )
                for app in config.apps:
                    for project_id in app.gitlab_project_ids:
                        response = client.get(f"/api/v4/projects/{project_id}")
                        result.append(
                            _check(
                                "gitlab_project_visibility",
                                f"live:app:{app.id}:gitlab:{project_id}",
                                "pass" if response.status_code == 200 else "fail",
                                "Configured GitLab project is visible."
                                if response.status_code == 200
                                else (
                                    "Configured GitLab project visibility failed with HTTP "
                                    f"{response.status_code}."
                                ),
                                None
                                if response.status_code == 200
                                else "Verify project scope and token permissions.",
                            )
                        )
        except (ConfigurationError, httpx.HTTPError, ValueError) as exc:
            result.append(
                _check(
                    "gitlab_live",
                    "live:gitlab",
                    "fail",
                    f"GitLab live validation failed: {type(exc).__name__}.",
                    "Check provider availability, credentials, and configured origin.",
                )
            )
    return result


def run_doctor(config: WorkTraceConfig, *, live: bool) -> dict[str, object]:
    checks = [
        _check(
            "configuration",
            "local",
            "pass",
            "Configuration parsed and source mappings are internally scoped.",
        ),
        *_storage_checks(config),
        *_database_checks(config),
        *_git_checks(config),
        *_dependency_checks(),
        *_credential_checks(config),
    ]
    if live:
        checks.extend(_live_checks(config))
    else:
        checks.append(
            _check(
                "live_providers",
                "live",
                "skipped",
                "Authenticated provider validation was not requested; use doctor --live.",
            )
        )
    return {"ok": not any(check["status"] == "fail" for check in checks), "checks": checks}
