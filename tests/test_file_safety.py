from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

import worktrace.db.migrations as migration_module
import worktrace.services as services_module
from worktrace.cli import app
from worktrace.db.connection import connect
from worktrace.db.migrations import backup_database, migrate
from worktrace.services import export_app


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _database(tmp_path: Path) -> tuple[object, Path]:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    migrate(connection, database_path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) "
        "VALUES ('sample_store', 'Sample Store', '', '')"
    )
    connection.commit()
    return connection, database_path


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        f"""
schema_version = 1

[data]
directory = {str(tmp_path / "data")!r}

[employment]
from = "2024-01-01"
to = "2026-08-26"

[identity]
display_name = "Fixture Engineer"
git_author_emails = ["fixture@example.test"]
git_author_names = ["Fixture Engineer"]

[[apps]]
id = "sample_store"
name = "Sample Store"
jira_project_keys = ["DEMO"]
gitlab_project_ids = []
repo_paths = []
""",
        encoding="utf-8",
    )
    return path


def test_export_and_backup_are_mode_0600_before_content_is_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, database_path = _database(tmp_path)
    export_path = tmp_path / "nested" / "export.json"
    backup_path = tmp_path / "nested" / "custom.backup"
    export_observation: list[tuple[int, int]] = []
    backup_observation: list[tuple[int, int]] = []
    original_dump = services_module.json.dump
    original_copy = migration_module.shutil.copyfileobj

    def inspect_dump(value: object, output: object, **kwargs: object) -> None:
        descriptor = output.fileno()  # type: ignore[attr-defined]
        export_observation.append(
            (stat.S_IMODE(os.fstat(descriptor).st_mode), os.fstat(descriptor).st_size)
        )
        original_dump(value, output, **kwargs)  # type: ignore[arg-type]

    def inspect_copy(source: object, target: object) -> None:
        descriptor = target.fileno()  # type: ignore[attr-defined]
        backup_observation.append(
            (stat.S_IMODE(os.fstat(descriptor).st_mode), os.fstat(descriptor).st_size)
        )
        original_copy(source, target)  # type: ignore[arg-type]

    monkeypatch.setattr(services_module.json, "dump", inspect_dump)
    monkeypatch.setattr(migration_module.shutil, "copyfileobj", inspect_copy)
    try:
        assert export_app(connection, "sample_store", export_path) == 0
        assert backup_database(database_path, backup_path) == backup_path
    finally:
        connection.close()

    assert export_observation == [(0o600, 0)]
    assert backup_observation == [(0o600, 0)]
    assert _mode(export_path) == 0o600
    assert _mode(backup_path) == 0o600


@pytest.mark.parametrize("operation", ["export", "backup"])
@pytest.mark.parametrize("destination_kind", ["existing", "symlink"])
def test_export_and_backup_never_overwrite_existing_or_symlink_targets(
    tmp_path: Path,
    operation: str,
    destination_kind: str,
) -> None:
    connection, database_path = _database(tmp_path)
    destination = tmp_path / f"{operation}.out"
    protected = tmp_path / f"{operation}.protected"
    original = b"do-not-overwrite"
    if destination_kind == "existing":
        destination.write_bytes(original)
        protected_path = destination
    else:
        protected.write_bytes(original)
        destination.symlink_to(protected)
        protected_path = protected

    try:
        with pytest.raises(FileExistsError):
            if operation == "export":
                export_app(connection, "sample_store", destination)
            else:
                backup_database(database_path, destination)
    finally:
        connection.close()

    assert protected_path.read_bytes() == original
    if destination_kind == "symlink":
        assert destination.is_symlink()


def test_purge_removes_managed_database_sidecars_but_reports_external_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("WORKTRACE_DB_PATH", raising=False)
    config = _config(tmp_path)
    runner = CliRunner()
    initialized = runner.invoke(app, ["init", "--config", str(config)])
    assert initialized.exit_code == 0, initialized.stdout

    data = tmp_path / "data"
    database = data / "worktrace.sqlite3"
    key = data / "email-hmac.key"
    wal = data / "worktrace.sqlite3-wal"
    shm = data / "worktrace.sqlite3-shm"
    managed_backup = data / "managed.backup"
    external_export = tmp_path / "external-export.json"
    custom_backup = tmp_path / "custom.backup"
    wal.write_bytes(b"synthetic-wal")
    shm.write_bytes(b"synthetic-shm")
    managed_backup.write_bytes(b"synthetic-managed-backup")
    external_export.write_bytes(b"synthetic-external-export")
    custom_backup.write_bytes(b"synthetic-custom-backup")

    purged = runner.invoke(app, ["purge", "--yes", "--config", str(config)])
    assert purged.exit_code == 0, purged.stdout
    result = json.loads(purged.stdout)

    assert result["recoverable"] is True
    assert "Exports and custom backups outside" in result["retained"]
    assert set(result["removed"]) == {
        str(database),
        str(wal),
        str(shm),
        str(key),
        str(managed_backup),
    }
    assert not database.exists()
    assert not wal.exists()
    assert not shm.exists()
    assert not key.exists()
    assert not managed_backup.exists()
    assert external_export.read_bytes() == b"synthetic-external-export"
    assert custom_backup.read_bytes() == b"synthetic-custom-backup"
