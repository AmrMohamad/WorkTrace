from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from worktrace.candidates.decisions import append_decision
from worktrace.config import AppConfig
from worktrace.db.connection import connect
from worktrace.db.migrations import migrate
from worktrace.db.repository import EvidenceRepository
from worktrace.domain.enums import Completeness
from worktrace.domain.models import (
    ActorObservation,
    NormalizedObject,
    ParticipationObservation,
    PendingReference,
    SourceIdentity,
)
from worktrace.normalize.redaction import Redactor
from worktrace.services import add_manual_evidence, export_app

RAW_EMAIL = "raw.person@example.test"
BEARER_SECRET = "fixture-bearer-secret"
TOKEN_SECRET = "fixture-token-secret"
PASSWORD_SECRET = "fixture-password-secret"
PEM_SECRET = "fixture-pem-secret"
PHONE_SECRET = "+1 (555) 123-4567"
SESSION_SECRET = "fixture-session-secret"
DIFF_SECRET = "fixture-diff-secret"
PATCH_SECRET = "fixture-patch-secret"
ATTACHMENT_SECRET = "fixture-attachment-secret"
CLIENT_SECRET = "fixture-client-secret"
GENERIC_SECRET = "fixture-generic-secret"
API_KEY_SECRET = "fixture-api-key"
ACCESS_TOKEN_SECRET = "fixture-access-token"
HEADER_SECRET = "fixture-header-secret"
UNDERSCORE_API_KEY_SECRET = "fixture-underscore-api-key"
PRIVATE_TOKEN_SECRET = "fixture-private-token"
REFRESH_TOKEN_SECRET = "fixture-refresh-token"
API_TOKEN_SECRET = "fixture-api-token"
STRUCTURED_SESSION_ID = "fixture-structured-session-id"
STRUCTURED_SESSION_TOKEN = "fixture-structured-session-token"
STRUCTURED_JSESSIONID = "fixture-structured-jsessionid"

UNTRUSTED_TEXT = f"""
Contact {RAW_EMAIL}.
Authorization: Bearer {BEARER_SECRET}
token={TOKEN_SECRET}
password={PASSWORD_SECRET}
-----BEGIN PRIVATE KEY-----
{PEM_SECRET}
-----END PRIVATE KEY-----
Call {PHONE_SECRET}.
session_id={SESSION_SECRET}
client_secret={CLIENT_SECRET}
secret={GENERIC_SECRET}
api_key={API_KEY_SECRET}
access_token={ACCESS_TOKEN_SECRET}
X-API-Key: {HEADER_SECRET}
x_api_key={UNDERSCORE_API_KEY_SECRET}
private_token={PRIVATE_TOKEN_SECRET}
refresh_token={REFRESH_TOKEN_SECRET}
api_token={API_TOKEN_SECRET}
""".strip()

FORBIDDEN = (
    RAW_EMAIL,
    BEARER_SECRET,
    TOKEN_SECRET,
    PASSWORD_SECRET,
    PEM_SECRET,
    PHONE_SECRET,
    SESSION_SECRET,
    DIFF_SECRET,
    PATCH_SECRET,
    ATTACHMENT_SECRET,
    CLIENT_SECRET,
    GENERIC_SECRET,
    API_KEY_SECRET,
    ACCESS_TOKEN_SECRET,
    HEADER_SECRET,
    UNDERSCORE_API_KEY_SECRET,
    PRIVATE_TOKEN_SECRET,
    REFRESH_TOKEN_SECRET,
    API_TOKEN_SECRET,
    STRUCTURED_SESSION_ID,
    STRUCTURED_SESSION_TOKEN,
    STRUCTURED_JSESSIONID,
)


def _app() -> AppConfig:
    return AppConfig(
        id="sample_store",
        name="Sample Store",
        market="XX",
        business_type="fixture",
        jira_project_keys=("DEMO",),
        gitlab_project_ids=(),
        repo_paths=(),
        jira_key_patterns=(r"DEMO-[0-9]+",),
        production_environments=(),
        release_tag_patterns=(),
        ignored_paths=(),
    )


def _open_repository(
    tmp_path: Path,
) -> tuple[sqlite3.Connection, EvidenceRepository, Path, Redactor]:
    database_path = tmp_path / "worktrace.sqlite3"
    connection = connect(database_path)
    migrate(connection, database_path)
    connection.execute(
        "INSERT INTO apps(id, name, market, business_type) "
        "VALUES ('sample_store', 'Sample Store', '', '')"
    )
    connection.commit()
    redactor = Redactor(email_key=b"fixture-only-key")
    return connection, EvidenceRepository(connection, redactor), database_path, redactor


def _raw_object() -> NormalizedObject:
    actor = ActorObservation(
        source="manual",
        source_instance="fixture-source",
        external_actor_id=RAW_EMAIL,
        display_name=f"Fixture Actor {RAW_EMAIL}",
        email_hash=RAW_EMAIL,
        is_self=False,
    )
    return NormalizedObject(
        identity=SourceIdentity(
            source="manual",
            source_instance="fixture-source",
            kind="manual_evidence",
            external_id="fixture-object-1",
        ),
        app_id="sample_store",
        title=UNTRUSTED_TEXT,
        body_text=UNTRUSTED_TEXT,
        source_updated_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
        actors=(actor,),
        participations=(ParticipationObservation(RAW_EMAIL, "reporter"),),
        pending_references=(),
        data={
            "description": UNTRUSTED_TEXT,
            "email": RAW_EMAIL,
            "diff": DIFF_SECRET,
            "patch": PATCH_SECRET,
            "attachments": [{"content": ATTACHMENT_SECRET}],
            "api_token": API_TOKEN_SECRET,
            "session_id": STRUCTURED_SESSION_ID,
            "session_token": STRUCTURED_SESSION_TOKEN,
            "jsessionid": STRUCTURED_JSESSIONID,
        },
        completeness=Completeness.COMPLETE,
    )


def _assert_forbidden_absent(content: bytes) -> None:
    for value in FORBIDDEN:
        assert value.encode() not in content


def test_repository_manual_evidence_and_decisions_redact_before_sqlite_and_export(
    tmp_path: Path,
) -> None:
    connection, repository, database_path, redactor = _open_repository(tmp_path)
    export_path = tmp_path / "private-export.json"
    try:
        run_id = repository.start_sync_run(
            "sample_store", "manual", "fixture-source", {"mode": "fixture"}
        )
        repository.store_page(run_id, [_raw_object()])
        repository.finish_sync_run(run_id, "complete", "complete_for_scope")

        add_manual_evidence(
            repository,
            _app(),
            title=UNTRUSTED_TEXT,
            body=UNTRUSTED_TEXT,
            evidence_type="manual_attestation",
        )
        object_id = str(
            connection.execute(
                "SELECT id FROM source_objects WHERE external_id='fixture-object-1'"
            ).fetchone()[0]
        )
        append_decision(
            connection,
            "attest",
            object_id,
            {
                "statement": UNTRUSTED_TEXT,
                "diff": DIFF_SECRET,
                "patch": PATCH_SECRET,
                "attachment": ATTACHMENT_SECRET,
            },
            redactor=redactor,
        )
        export_app(connection, "sample_store", export_path)
        connection.commit()

        actor = connection.execute(
            "SELECT external_actor_id, display_name, email_hash FROM actors"
        ).fetchone()
        assert RAW_EMAIL not in "\n".join(str(value) for value in actor)
        assert str(actor["email_hash"]).startswith("email_hmac_sha256:")
        observations = "\n".join(
            "\n".join(str(value) for value in row)
            for row in connection.execute("SELECT title, body_text, data_json FROM observations")
        )
        decisions = "\n".join(
            str(row[0]) for row in connection.execute("SELECT payload_json FROM human_decisions")
        )
        _assert_forbidden_absent(observations.encode())
        _assert_forbidden_absent(decisions.encode())
        assert "[REDACTED]" in observations
        assert "[REDACTED_SECRET]" in observations
    finally:
        connection.close()

    _assert_forbidden_absent(database_path.read_bytes())
    _assert_forbidden_absent(export_path.read_bytes())


def test_provider_identity_and_pending_references_are_pseudonymized_before_ids(
    tmp_path: Path,
) -> None:
    connection, repository, database_path, _ = _open_repository(tmp_path)
    provider_secret = "token=fixture-release-secret"
    release = NormalizedObject(
        identity=SourceIdentity(
            source="gitlab",
            source_instance="gitlab.example.test",
            kind="gitlab_release",
            external_id=provider_secret,
            canonical_url=(
                "https://gitlab.example.test/releases/token=fixture-release-secret"
                "?private_token=fixture-release-secret"
            ),
        ),
        app_id="sample_store",
        title="Synthetic release",
        body_text=None,
        source_updated_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
        actors=(),
        participations=(),
        pending_references=(
            PendingReference(
                target_source="git",
                target_kind="git_tag",
                target_external_id=provider_secret,
                relationship_type="release_uses_tag",
                extraction_method="structured",
                exact_value=provider_secret,
            ),
        ),
        data={"tag_name": provider_secret},
        completeness=Completeness.COMPLETE,
    )
    try:
        object_ids = []
        for index in range(2):
            run_id = repository.start_sync_run(
                "sample_store",
                "gitlab",
                "gitlab.example.test",
                {"selection_policy_version": 2, "run": index},
            )
            repository.store_page(run_id, [release])
            repository.finish_sync_run(run_id, "complete", "complete_for_scope")
            object_ids.append(
                str(
                    connection.execute(
                        "SELECT id FROM source_objects WHERE kind='gitlab_release'"
                    ).fetchone()[0]
                )
            )

        assert object_ids[0] == object_ids[1]
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM source_objects WHERE kind='gitlab_release'"
            ).fetchone()[0]
            == 1
        )
        persisted = connection.execute(
            "SELECT external_id, canonical_url FROM source_objects WHERE kind='gitlab_release'"
        ).fetchone()
        assert str(persisted["external_id"]).startswith("provider_hmac_sha256:")
        assert "fixture-release-secret" not in str(persisted["external_id"])
        assert "private_token" not in str(persisted["canonical_url"])
        pending = "\n".join(
            str(row[0]) for row in connection.execute("SELECT data_json FROM observations")
        )
        assert "fixture-release-secret" not in pending
        assert "provider_hmac_sha256:" in pending
    finally:
        connection.close()

    assert b"fixture-release-secret" not in database_path.read_bytes()


def test_current_verified_actor_classification_can_clear_a_removed_self_alias(
    tmp_path: Path,
) -> None:
    connection, repository, _, _ = _open_repository(tmp_path)

    def observed_commit(external_id: str, *, is_self: bool) -> NormalizedObject:
        actor = ActorObservation(
            source="git",
            source_instance="fixture-repository",
            external_actor_id="stable-provider-actor",
            display_name="Fixture Engineer",
            is_self=is_self,
        )
        return NormalizedObject(
            identity=SourceIdentity(
                source="git",
                source_instance="fixture-repository",
                kind="git_commit",
                external_id=external_id,
            ),
            app_id="sample_store",
            title=f"Synthetic commit {external_id}",
            body_text=None,
            source_updated_at=datetime(2026, 1, 10, 10, 0, tzinfo=UTC),
            actors=(actor,),
            participations=(ParticipationObservation("stable-provider-actor", "git_author"),),
            pending_references=(),
            data={"sha": external_id},
            completeness=Completeness.COMPLETE,
        )

    try:
        first = repository.start_sync_run(
            "sample_store", "git", "fixture-repository", {"mode": "fixture"}
        )
        repository.store_page(first, [observed_commit("a" * 40, is_self=True)])
        repository.finish_sync_run(first, "complete", "complete_for_scope")
        assert (
            connection.execute(
                "SELECT is_self FROM actors WHERE external_actor_id='stable-provider-actor'"
            ).fetchone()[0]
            == 1
        )

        corrected = repository.start_sync_run(
            "sample_store", "git", "fixture-repository", {"mode": "fixture"}
        )
        repository.store_page(corrected, [observed_commit("b" * 40, is_self=False)])
        repository.finish_sync_run(corrected, "complete", "complete_for_scope")

        assert (
            connection.execute(
                "SELECT is_self FROM actors WHERE external_actor_id='stable-provider-actor'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM participations WHERE actor_id IN "
                "(SELECT id FROM actors WHERE external_actor_id='stable-provider-actor')"
            ).fetchone()[0]
            == 2
        )
    finally:
        connection.close()
