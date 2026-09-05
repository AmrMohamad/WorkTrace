from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from worktrace.config import AppConfig, IdentityConfig, WorkTraceConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate, migrations
from worktrace.errors import ConfigurationError, ScopeViolation
from worktrace.identity import (
    finish_identity_rebuild,
    identity_fingerprint,
    identity_policy_status,
    initialize_identity,
    prepare_identity_import,
    repair_identities,
)
from worktrace.normalize.redaction import Redactor

KEY = b"synthetic-ledger-key"


def _config(tmp_path: Path) -> WorkTraceConfig:
    app = AppConfig("sample", "Sample", "", "", (), (), (tmp_path,), (), (), (), ())
    return WorkTraceConfig(
        1,
        tmp_path,
        date(2025, 1, 1),
        date(2025, 12, 31),
        IdentityConfig("Person", ("old@example.test",), ("Person",), None, None, None),
        (app, replace(app, id="other")),
        tmp_path / "config.toml",
    )


def _database(tmp_path: Path, *, legacy: bool = False) -> sqlite3.Connection:
    path = tmp_path / "ledger.sqlite3"
    connection = connect(path)
    if legacy:
        for migration in migrations()[:3]:
            connection.executescript(migration.sql)
            connection.execute(f"PRAGMA user_version={migration.version}")
    else:
        migrate(connection, path)
    connection.executemany(
        "INSERT INTO apps(id,name) VALUES (?,?)", [("sample", "Sample"), ("other", "Other")]
    )
    connection.commit()
    return connection


def _actor(
    connection: sqlite3.Connection,
    actor_id: str,
    email: str | None,
    *,
    is_self: bool,
    app_id: str = "sample",
) -> None:
    email_hash = Redactor(KEY).hash_email(email) if email else None
    connection.execute(
        "INSERT OR IGNORE INTO actors(id,source,source_instance,external_actor_id,"
        "display_name,email_hash,is_self) "
        "VALUES (?,'git','shared',?,'Person',?,?)",
        (actor_id, actor_id, email_hash, int(is_self)),
    )
    object_id = f"obj:{app_id}:{actor_id}"
    connection.execute(
        "INSERT INTO source_objects(id,app_id,source,source_instance,kind,external_id) "
        "VALUES (?,?,'git','shared','git_commit',?)",
        (object_id, app_id, actor_id),
    )
    connection.execute(
        "INSERT INTO participations(id,source_object_id,actor_id,role) VALUES (?,?,?,'git_author')",
        (f"part:{app_id}:{actor_id}", object_id, actor_id),
    )
    connection.commit()


def _legacy(tmp_path: Path) -> tuple[sqlite3.Connection, WorkTraceConfig]:
    connection = _database(tmp_path, legacy=True)
    _actor(connection, "actor:old", "old@example.test", is_self=False)
    _actor(connection, "actor:imposter", "different@example.test", is_self=True)
    migrate(connection, tmp_path / "ledger.sqlite3")
    connection.commit()
    return connection, _config(tmp_path)


def _repair(
    connection: sqlite3.Connection, config: WorkTraceConfig, **kwargs: object
) -> dict[str, object]:
    return repair_identities(  # type: ignore[arg-type]
        connection, config, KEY, "sample", proof_actor_id="actor:old", proof_alias_index=0, **kwargs
    )


def test_populated_legacy_repair_preserves_ids_and_records_rereview(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    try:
        payload = {
            "app_id": "sample",
            "contribution_id": "contribution:old",
            "members": ["obj:sample:actor:imposter"],
        }
        connection.execute(
            "INSERT INTO human_decisions(id,action,target_id,payload_json,created_at) VALUES "
            "('decision:confirm','confirm_candidate','candidate:gone',?,'2025-01-01')",
            (json.dumps(payload),),
        )
        connection.commit()
        before = list(connection.execute("SELECT id FROM source_objects ORDER BY id"))
        proposal = _repair(connection, config)
        assert proposal["promotions"] == proposal["demotions"] == 1
        assert proposal["confirmed_targets"] == ["candidate:gone", "contribution:old"]
        assert connection.execute("SELECT count(*) FROM identity_key_binding").fetchone()[0] == 0
        with connection:
            result = _repair(
                connection, config, apply=True, expected_proposal=proposal["proposal_token"]
            )
        assert result["applied"] is True
        assert list(connection.execute("SELECT id FROM source_objects ORDER BY id")) == before
        assert connection.execute("SELECT count(*) FROM human_decisions").fetchone()[0] == 1
        assert dict(connection.execute("SELECT id,is_self FROM actors")) == {
            "actor:old": 1,
            "actor:imposter": 0,
        }
        status = identity_policy_status(connection, config, "sample")
        assert status["valid"] and status["rebuild_required"]
        assert status["requires_rereview"] == ["candidate:gone", "contribution:old"]
        audit = connection.execute("SELECT report_json FROM identity_repair_audit").fetchone()[0]
        assert "@example.test" not in audit
        assert KEY.decode() not in audit
        with connection:
            finish_identity_rebuild(connection, config, "sample")
        assert not identity_policy_status(connection, config, "sample")["rebuild_required"]
        assert identity_policy_status(connection, config, "sample")["requires_rereview"]
        assert (
            connection.execute("SELECT read_revision FROM apps WHERE id='sample'").fetchone()[0]
            == 2
        )
    finally:
        connection.close()


@pytest.mark.parametrize("key", [b"", b"wrong-but-present"])
def test_wrong_or_missing_key_cannot_enroll_legacy(tmp_path: Path, key: bytes) -> None:
    connection, config = _legacy(tmp_path)
    try:
        with pytest.raises(ConfigurationError):
            repair_identities(
                connection,
                config,
                key,
                "sample",
                apply=True,
                proof_actor_id="actor:old",
                proof_alias_index=0,
            )
        assert connection.execute("SELECT count(*) FROM identity_key_binding").fetchone()[0] == 0
        assert (
            connection.execute("SELECT is_self FROM actors WHERE id='actor:imposter'").fetchone()[0]
            == 1
        )
    finally:
        connection.close()


def test_legacy_needs_explicit_matching_alias_proof(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    try:
        for kwargs in (
            {},
            {"proof_actor_id": "actor:imposter", "proof_alias_index": 0},
            {"proof_actor_id": "actor:old", "proof_alias_index": 1},
        ):
            with pytest.raises(ConfigurationError):
                repair_identities(connection, config, KEY, "sample", apply=True, **kwargs)
        with pytest.raises(ConfigurationError, match="continuity"):
            initialize_identity(connection, config, KEY)
        with pytest.raises(ConfigurationError):
            prepare_identity_import(connection, config, KEY, "sample")
    finally:
        connection.close()


def test_empty_ledger_binding_detects_override_and_policy_changes(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    config = _config(tmp_path)
    try:
        with connection:
            initialize_identity(connection, config, KEY)
        binding = tuple(connection.execute("SELECT * FROM identity_key_binding").fetchone())
        initialize_identity(connection, config, KEY)
        prepare_identity_import(connection, config, KEY, "sample")
        with pytest.raises(ConfigurationError, match="does not match"):
            prepare_identity_import(connection, config, b"override-key", "sample")
        changed = replace(
            config, identity=replace(config.identity, git_author_emails=("new@example.test",))
        )
        initialize_identity(connection, changed, KEY)
        with pytest.raises(ConfigurationError, match="requires repair"):
            prepare_identity_import(connection, changed, KEY, "sample")
        assert not identity_policy_status(connection, changed, "sample")["valid"]
        assert tuple(connection.execute("SELECT * FROM identity_key_binding").fetchone()) == binding
        renamed = replace(
            config,
            identity=replace(
                config.identity, display_name="Renamed", git_author_names=("Renamed",)
            ),
        )
        assert identity_fingerprint(renamed) == identity_fingerprint(config)
    finally:
        connection.close()


def test_actual_shared_actor_reachability_refuses_cross_app_apply(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    try:
        _actor(connection, "actor:imposter", "different@example.test", is_self=True, app_id="other")
        proposal = _repair(connection, config)
        assert proposal["affected_apps"] == ["other", "sample"]
        with pytest.raises(ScopeViolation, match="other apps"):
            _repair(connection, config, apply=True)
        assert connection.execute("SELECT count(*) FROM identity_repair_audit").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM identity_key_binding").fetchone()[0] == 0
    finally:
        connection.close()


def test_stale_dry_run_is_rejected_before_changes(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    try:
        proposal = _repair(connection, config)
        _actor(connection, "actor:new", None, is_self=True)
        with pytest.raises(ConfigurationError, match="proposal changed"):
            _repair(connection, config, apply=True, expected_proposal=proposal["proposal_token"])
        assert connection.execute("SELECT count(*) FROM identity_key_binding").fetchone()[0] == 0
    finally:
        connection.close()


def test_audit_failure_rolls_back_entire_repair_but_preserves_caller_changes(
    tmp_path: Path,
) -> None:
    connection, config = _legacy(tmp_path)
    try:
        connection.execute("UPDATE apps SET name='Caller edit' WHERE id='sample'")
        connection.execute("DROP TABLE identity_repair_audit")
        with pytest.raises(sqlite3.OperationalError):
            _repair(connection, config, apply=True)
        assert (
            connection.execute("SELECT name FROM apps WHERE id='sample'").fetchone()[0]
            == "Caller edit"
        )
        assert (
            connection.execute("SELECT is_self FROM actors WHERE id='actor:imposter'").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM identity_key_binding").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM app_identity_policy").fetchone()[0] == 0
        assert (
            connection.execute("SELECT read_revision FROM apps WHERE id='sample'").fetchone()[0]
            == 0
        )
    finally:
        connection.rollback()
        connection.close()


def test_missing_policy_never_accepts_unbound_typed_rows(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    try:
        connection.execute("UPDATE actors SET identity_policy_version=1")
        assert not identity_policy_status(connection, config, "sample")["valid"]
        with pytest.raises(ConfigurationError):
            finish_identity_rebuild(connection, config, "sample")
    finally:
        connection.close()


def test_provider_ids_require_authenticated_verification_before_policy_acceptance(
    tmp_path: Path,
) -> None:
    connection, config = _legacy(tmp_path)
    config = replace(config, identity=replace(config.identity, jira_account_id="account:actual"))
    try:
        _actor(connection, "actor:jira", None, is_self=True)
        connection.execute(
            "UPDATE actors SET source='jira',external_actor_id='account:other' "
            "WHERE id='actor:jira'"
        )
        connection.commit()
        with connection:
            report = _repair(connection, config, apply=True)
        assert report["provider_verification_required"] == ["actor:jira"]
        assert (
            connection.execute("SELECT is_self FROM actors WHERE id='actor:jira'").fetchone()[0]
            == 1
        )
        assert not identity_policy_status(connection, config, "sample")["valid"]
        with pytest.raises(ConfigurationError):
            prepare_identity_import(connection, config, KEY, "sample")
        with connection:
            report = _repair(
                connection, config, apply=True, verified_self_ids={"jira": "account:actual"}
            )
        assert report["demotions"] == 1
        assert identity_policy_status(connection, config, "sample")["valid"]
        assert (
            connection.execute("SELECT is_self FROM actors WHERE id='actor:jira'").fetchone()[0]
            == 0
        )
    finally:
        connection.close()


def test_changed_config_cannot_reaccept_old_verified_provider_flags(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    config = replace(config, identity=replace(config.identity, jira_account_id="account:actual"))
    try:
        _actor(connection, "actor:jira", None, is_self=True)
        connection.execute(
            "UPDATE actors SET source='jira',external_actor_id='account:actual',"
            "identity_policy_version=1 "
            "WHERE id='actor:jira'"
        )
        with connection:
            _repair(connection, config, apply=True, verified_self_ids={"jira": "account:actual"})
        changed = replace(config, identity=replace(config.identity, jira_account_id="account:new"))
        with connection:
            report = _repair(connection, changed, apply=True)
        assert report["provider_verification_required"] == ["actor:jira"]
        assert not identity_policy_status(connection, changed, "sample")["valid"]
        with connection:
            repeated = _repair(connection, changed, apply=True)
        assert repeated["provider_verification_required"] == ["actor:jira"]
        assert not identity_policy_status(connection, changed, "sample")["valid"]
    finally:
        connection.close()


def test_verified_gitlab_email_and_numeric_id_are_distinct(tmp_path: Path) -> None:
    connection, config = _legacy(tmp_path)
    config = replace(config, identity=replace(config.identity, gitlab_username="configured-user"))
    try:
        _actor(connection, "actor:gitlab", "provider@example.test", is_self=True)
        connection.execute(
            "UPDATE actors SET source='gitlab',external_actor_id=email_hash,"
            "identity_policy_version=1 "
            "WHERE id='actor:gitlab'"
        )
        _actor(connection, "actor:numeric", "old@example.test", is_self=True)
        connection.execute(
            "UPDATE actors SET source='gitlab',external_actor_id='99' WHERE id='actor:numeric'"
        )
        connection.commit()
        with connection:
            report = _repair(connection, config, apply=True)
        assert set(report["provider_verification_required"]) == {"actor:gitlab", "actor:numeric"}
        with connection:
            report = _repair(
                connection,
                config,
                apply=True,
                verified_self_ids={"gitlab": "42"},
                verified_gitlab_email_hashes=frozenset(
                    {Redactor(KEY).hash_email("provider@example.test")}
                ),
            )
        assert report["demotions"] == 1
        flags = dict(connection.execute("SELECT id,is_self FROM actors"))
        assert flags["actor:gitlab"] == 1
        assert flags["actor:numeric"] == 0
        assert identity_policy_status(connection, config, "sample")["valid"]
    finally:
        connection.close()


@pytest.mark.parametrize(
    "verified", [{"jira": "wrong"}, {"gitlab": "0"}, {"gitlab": "42"}, {"git": "42"}]
)
def test_verified_provider_must_match_configured_identity(
    tmp_path: Path, verified: dict[str, str]
) -> None:
    connection, config = _legacy(tmp_path)
    try:
        with pytest.raises(ConfigurationError):
            _repair(connection, config, apply=True, verified_self_ids=verified)
        assert connection.execute("SELECT count(*) FROM identity_key_binding").fetchone()[0] == 0
    finally:
        connection.close()


def test_migration_key_binding_cannot_be_replaced_by_initialization(tmp_path: Path) -> None:
    connection = _database(tmp_path)
    config = _config(tmp_path)
    try:
        connection.execute(
            "INSERT INTO app_identity_policy VALUES ('sample',1,'accepted-original-config',0)"
        )
        with connection:
            initialize_identity(connection, config, KEY)
        assert not identity_policy_status(connection, config, "sample")["valid"]
        assert identity_policy_status(connection, config, "other")["valid"]
    finally:
        connection.close()
