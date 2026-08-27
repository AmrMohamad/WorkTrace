from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from worktrace.errors import ConfigurationError, ScopeViolation
from worktrace.paths import default_config_path, default_data_directory

APP_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _require_table(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a TOML table")
    return value


def _strings(value: object, name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigurationError(f"{name} must be an array of strings")
    return tuple(item.strip() for item in value if item.strip())


def _integers(value: object, name: str) -> tuple[int, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, int) for item in value):
        raise ConfigurationError(f"{name} must be an array of integers")
    return tuple(value)


def _date(value: object, name: str) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ConfigurationError(f"{name} must be an ISO date") from exc
    raise ConfigurationError(f"{name} must be an ISO date")


@dataclass(frozen=True)
class ModuleRule:
    pattern: str
    module: str


@dataclass(frozen=True)
class IdentityConfig:
    display_name: str
    git_author_emails: tuple[str, ...]
    git_author_names: tuple[str, ...]
    jira_account_id: str | None
    gitlab_user_id: int | None
    gitlab_username: str | None


@dataclass(frozen=True)
class AppConfig:
    id: str
    name: str
    market: str
    business_type: str
    jira_project_keys: tuple[str, ...]
    gitlab_project_ids: tuple[int, ...]
    repo_paths: tuple[Path, ...]
    jira_key_patterns: tuple[str, ...]
    production_environments: tuple[str, ...]
    release_tag_patterns: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    module_rules: tuple[ModuleRule, ...] = field(default_factory=tuple)
    jira_custom_fields: dict[str, str] = field(default_factory=dict)

    def assert_repo_scope(self, candidate: Path) -> Path:
        resolved = candidate.expanduser().resolve()
        if resolved not in self.repo_paths:
            raise ScopeViolation(f"repository is not configured for app {self.id}")
        return resolved

    def allows_jira_key(self, key: str) -> bool:
        return any(key.startswith(f"{project}-") for project in self.jira_project_keys)

    def allows_gitlab_project(self, project_id: int) -> bool:
        return project_id in self.gitlab_project_ids


@dataclass(frozen=True)
class WorkTraceConfig:
    schema_version: int
    data_directory: Path
    employment_from: date
    employment_to: date
    identity: IdentityConfig
    apps: tuple[AppConfig, ...]
    config_path: Path

    @property
    def database_path(self) -> Path:
        override = os.environ.get("WORKTRACE_DB_PATH")
        if override:
            return Path(override).expanduser().resolve()
        return self.data_directory / "worktrace.sqlite3"

    def app(self, app_id: str) -> AppConfig:
        for app in self.apps:
            if app.id == app_id:
                return app
        raise ScopeViolation(f"unconfigured app_id: {app_id}")


def _parse_app(raw: object, index: int) -> AppConfig:
    table = _require_table(raw, f"apps[{index}]")
    app_id = str(table.get("id", "")).strip()
    name = str(table.get("name", "")).strip()
    if not APP_ID_RE.fullmatch(app_id):
        raise ConfigurationError(f"apps[{index}].id must be lowercase snake_case")
    if not name:
        raise ConfigurationError(f"apps[{index}].name is required")

    jira_keys = _strings(table.get("jira_project_keys"), f"apps[{index}].jira_project_keys")
    if any(not JIRA_KEY_RE.fullmatch(key) for key in jira_keys):
        raise ConfigurationError(f"apps[{index}].jira_project_keys contains an invalid key")
    gitlab_ids = _integers(table.get("gitlab_project_ids"), f"apps[{index}].gitlab_project_ids")
    repo_paths = tuple(
        Path(path).expanduser().resolve()
        for path in _strings(table.get("repo_paths"), f"apps[{index}].repo_paths")
    )
    if not (jira_keys or gitlab_ids or repo_paths):
        raise ConfigurationError(f"apps[{index}] must configure at least one source")

    patterns = _strings(table.get("jira_key_patterns"), f"apps[{index}].jira_key_patterns")
    if not patterns:
        patterns = tuple(rf"{re.escape(key)}-[0-9]+" for key in jira_keys)
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigurationError(f"invalid Jira key pattern: {pattern}") from exc

    raw_rules = table.get("module_rules", [])
    if not isinstance(raw_rules, list):
        raise ConfigurationError(f"apps[{index}].module_rules must be an array of tables")
    rules: list[ModuleRule] = []
    for rule_index, raw_rule in enumerate(raw_rules):
        rule = _require_table(raw_rule, f"apps[{index}].module_rules[{rule_index}]")
        pattern = str(rule.get("pattern", "")).strip()
        module = str(rule.get("module", "")).strip()
        if not pattern or not module:
            raise ConfigurationError("module rules require pattern and module")
        rules.append(ModuleRule(pattern=pattern, module=module))

    custom_fields_raw = table.get("jira_custom_fields", {})
    custom_fields = _require_table(custom_fields_raw, f"apps[{index}].jira_custom_fields")
    if not all(
        isinstance(key, str) and isinstance(value, str) for key, value in custom_fields.items()
    ):
        raise ConfigurationError("Jira custom field mappings must be strings")

    return AppConfig(
        id=app_id,
        name=name,
        market=str(table.get("market", "")),
        business_type=str(table.get("business_type", "")),
        jira_project_keys=jira_keys,
        gitlab_project_ids=gitlab_ids,
        repo_paths=repo_paths,
        jira_key_patterns=patterns,
        production_environments=_strings(
            table.get("production_environments"), f"apps[{index}].production_environments"
        ),
        release_tag_patterns=_strings(
            table.get("release_tag_patterns"), f"apps[{index}].release_tag_patterns"
        ),
        ignored_paths=_strings(table.get("ignored_paths"), f"apps[{index}].ignored_paths"),
        module_rules=tuple(rules),
        jira_custom_fields={str(key): str(value) for key, value in custom_fields.items()},
    )


def load_config(path: Path | None = None) -> WorkTraceConfig:
    config_path = (path or default_config_path()).expanduser().resolve()
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration not found: {config_path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot parse configuration: {config_path}") from exc

    if raw.get("schema_version") != 1:
        raise ConfigurationError("schema_version must be 1")
    data = _require_table(raw.get("data", {}), "data")
    employment = _require_table(raw.get("employment"), "employment")
    identity_raw = _require_table(raw.get("identity"), "identity")
    apps_raw = raw.get("apps")
    if not isinstance(apps_raw, list) or not apps_raw:
        raise ConfigurationError("at least one [[apps]] table is required")

    apps = tuple(_parse_app(item, index) for index, item in enumerate(apps_raw))
    ids = [app.id for app in apps]
    if len(ids) != len(set(ids)):
        raise ConfigurationError("application IDs must be unique")

    all_repos = [path for app in apps for path in app.repo_paths]
    if len(all_repos) != len(set(all_repos)):
        raise ConfigurationError("a repository may belong to only one application")
    all_gitlab = [project for app in apps for project in app.gitlab_project_ids]
    if len(all_gitlab) != len(set(all_gitlab)):
        raise ConfigurationError("a GitLab project may belong to only one application")
    all_jira = [project for app in apps for project in app.jira_project_keys]
    if len(all_jira) != len(set(all_jira)):
        raise ConfigurationError("a Jira project may belong to only one application")

    employment_from = _date(employment.get("from"), "employment.from")
    employment_to = _date(employment.get("to"), "employment.to")
    if employment_from > employment_to:
        raise ConfigurationError("employment.from must not be after employment.to")

    display_name = str(identity_raw.get("display_name", "")).strip()
    if not display_name:
        raise ConfigurationError("identity.display_name is required")
    gitlab_user_id = identity_raw.get("gitlab_user_id")
    if gitlab_user_id is not None and not isinstance(gitlab_user_id, int):
        raise ConfigurationError("identity.gitlab_user_id must be an integer")

    directory = Path(str(data.get("directory", default_data_directory()))).expanduser().resolve()
    return WorkTraceConfig(
        schema_version=1,
        data_directory=directory,
        employment_from=employment_from,
        employment_to=employment_to,
        identity=IdentityConfig(
            display_name=display_name,
            git_author_emails=_strings(
                identity_raw.get("git_author_emails"), "identity.git_author_emails"
            ),
            git_author_names=_strings(
                identity_raw.get("git_author_names"), "identity.git_author_names"
            ),
            jira_account_id=(
                str(identity_raw["jira_account_id"])
                if identity_raw.get("jira_account_id")
                else None
            ),
            gitlab_user_id=gitlab_user_id,
            gitlab_username=(
                str(identity_raw["gitlab_username"])
                if identity_raw.get("gitlab_username")
                else None
            ),
        ),
        apps=apps,
        config_path=config_path,
    )


@dataclass(frozen=True)
class JiraCredentials:
    base_url: str
    email: str
    token: str


@dataclass(frozen=True)
class GitLabCredentials:
    base_url: str
    token: str


def jira_credentials() -> JiraCredentials | None:
    values = tuple(
        os.environ.get(name)
        for name in (
            "WORKTRACE_JIRA_BASE_URL",
            "WORKTRACE_JIRA_EMAIL",
            "WORKTRACE_JIRA_API_TOKEN",
        )
    )
    if not any(values):
        return None
    if not all(values):
        raise ConfigurationError("all WORKTRACE_JIRA_* credential variables are required")
    base_url, email, token = values
    assert base_url is not None and email is not None and token is not None
    return JiraCredentials(base_url.rstrip("/"), email, token)


def gitlab_credentials() -> GitLabCredentials | None:
    base_url = os.environ.get("WORKTRACE_GITLAB_BASE_URL")
    token = os.environ.get("WORKTRACE_GITLAB_TOKEN")
    if not base_url and not token:
        return None
    if not base_url or not token:
        raise ConfigurationError(
            "WORKTRACE_GITLAB_BASE_URL and WORKTRACE_GITLAB_TOKEN are both required"
        )
    return GitLabCredentials(base_url.rstrip("/"), token)
