from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from worktrace.cli import app
from worktrace.db.connection import connect
from worktrace.db.migrations import migrations


def _config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        "\n".join(
            (
                "schema_version = 1",
                "",
                "[data]",
                f"directory = {str(tmp_path / 'data')!r}",
                "",
                "[employment]",
                'from = "2024-01-01"',
                'to = "2026-08-26"',
                "",
                "[identity]",
                'display_name = "Fixture Engineer"',
                'git_author_emails = ["fixture@example.test"]',
                'git_author_names = ["Fixture Engineer"]',
                "",
                "[[apps]]",
                'id = "sample_store"',
                'name = "Sample Store"',
                'jira_project_keys = ["DEMO"]',
                "gitlab_project_ids = []",
                "repo_paths = []",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return config


def _database(tmp_path: Path, *, schema: int, with_collection: bool = False) -> None:
    database = tmp_path / "data" / "worktrace.sqlite3"
    database.parent.mkdir()
    connection = connect(database)
    try:
        for migration in migrations()[:schema]:
            connection.executescript(migration.sql)
            connection.execute(f"PRAGMA user_version={migration.version}")
        if with_collection:
            connection.execute(
                "INSERT INTO jira_archive_sites(id, canonical_origin, hash_algorithm) "
                "VALUES ('jira-site:test', 'https://jira.example.test', 'sha256')"
            )
            connection.execute(
                "INSERT INTO jira_collections "
                "(id,site_id,scope_json,scope_hash,approval_token_hash,policy_version,"
                "config_fingerprint,vault_id,created_at,retired_at) "
                "VALUES ('jcol:test','jira-site:test','{}','scope','token',1,'config',"
                "'vault:test','2026-01-01T00:00:00+00:00',NULL)"
            )
        connection.commit()
    finally:
        connection.close()


def _run_backup(tmp_path: Path) -> object:
    config = _config(tmp_path)
    destination = tmp_path / "backup.sqlite3"
    return CliRunner().invoke(
        app,
        ["backup", "--destination", str(destination), "--config", str(config)],
    )


def test_schema6_db_only_backup_succeeds_without_jira_warning(tmp_path: Path) -> None:
    _database(tmp_path, schema=6)
    result = _run_backup(tmp_path)
    assert result.exit_code == 0, result.stdout
    assert "vault-inclusive portability" not in result.output


def test_schema8_without_collections_succeeds_without_warning(tmp_path: Path) -> None:
    _database(tmp_path, schema=8)
    result = _run_backup(tmp_path)
    assert result.exit_code == 0, result.stdout
    assert "vault-inclusive portability" not in result.output


def test_schema8_collection_emits_db_only_warning(tmp_path: Path) -> None:
    _database(tmp_path, schema=8, with_collection=True)
    result = _run_backup(tmp_path)
    assert result.exit_code == 0, result.stdout
    assert "DB-only backup" in result.output
    assert "vault-inclusive portability" in result.output
