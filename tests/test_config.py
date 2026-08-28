from __future__ import annotations

from pathlib import Path

import pytest

from worktrace.config import load_config
from worktrace.errors import ConfigurationError, ScopeViolation


def _config_text(apps: str, data_directory: Path) -> str:
    return f"""
schema_version = 1

[data]
directory = {str(data_directory)!r}

[employment]
from = "2024-01-01"
to = "2026-08-26"

[identity]
display_name = "Fixture Engineer"
git_author_emails = ["fixture@example.test"]
git_author_names = ["Fixture Engineer"]

{apps}
"""


@pytest.mark.parametrize(
    ("source_fields", "message"),
    [
        (
            'jira_project_keys = ["DEMO"]\ngitlab_project_ids = []\nrepo_paths = []',
            "Jira project may belong to only one application",
        ),
        (
            "jira_project_keys = []\ngitlab_project_ids = [101]\nrepo_paths = []",
            "GitLab project may belong to only one application",
        ),
        (
            'jira_project_keys = []\ngitlab_project_ids = []\nrepo_paths = ["{repo}"]',
            "repository may belong to only one application",
        ),
    ],
)
def test_duplicate_cross_app_source_scope_is_rejected(
    tmp_path: Path,
    source_fields: str,
    message: str,
) -> None:
    repo = tmp_path / "configured-repository"
    shared_fields = source_fields.format(repo=repo)
    apps = f"""
[[apps]]
id = "sample_one"
name = "Sample One"
{shared_fields}

[[apps]]
id = "sample_two"
name = "Sample Two"
{shared_fields}
"""
    path = tmp_path / "config.toml"
    path.write_text(_config_text(apps, tmp_path / "data"), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=message):
        load_config(path)


def test_unconfigured_app_and_repository_are_out_of_scope(tmp_path: Path) -> None:
    repository = (tmp_path / "configured-repository").resolve()
    apps = f"""
[[apps]]
id = "sample_store"
name = "Sample Store"
jira_project_keys = ["DEMO"]
gitlab_project_ids = [101]
repo_paths = ["{repository}"]
"""
    path = tmp_path / "config.toml"
    path.write_text(_config_text(apps, tmp_path / "data"), encoding="utf-8")
    config = load_config(path)

    assert config.app("sample_store").assert_repo_scope(repository) == repository
    with pytest.raises(ScopeViolation, match="unconfigured app_id"):
        config.app("not_configured")
    with pytest.raises(ScopeViolation, match="repository is not configured"):
        config.app("sample_store").assert_repo_scope(tmp_path / "outside")
