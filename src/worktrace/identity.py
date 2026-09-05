"""CLI identity-policy transitions and offline read readiness.

Writes join the caller's transaction through a savepoint. Public CLI callers own
the commit; no helper can commit unrelated evidence or a staged source page.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from worktrace.candidates.decisions import (
    decision_lineages,
    decision_scope_map,
    decision_stream,
    snapshot_member_ids,
)
from worktrace.config import WorkTraceConfig
from worktrace.errors import ConfigurationError, ScopeViolation
from worktrace.normalize.redaction import Redactor

IDENTITY_POLICY_VERSION = 1


def identity_fingerprint(config: WorkTraceConfig) -> str:
    """Fingerprint only classification inputs; names cannot change identity."""
    identity = config.identity
    value = {
        "version": IDENTITY_POLICY_VERSION,
        "emails": sorted({email.strip().casefold() for email in identity.git_author_emails}),
        "jira_account_id": identity.jira_account_id,
        "gitlab_user_id": identity.gitlab_user_id,
        "gitlab_username": identity.gitlab_username if identity.gitlab_user_id is None else None,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


@contextmanager
def _transition(connection: sqlite3.Connection) -> Iterator[None]:
    name = "identity_" + uuid.uuid4().hex
    connection.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        connection.execute(f"ROLLBACK TO {name}")
        connection.execute(f"RELEASE {name}")
        raise
    else:
        connection.execute(f"RELEASE {name}")


def _populated(connection: sqlite3.Connection) -> bool:
    return any(
        connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is not None
        for table in ("actors", "source_objects", "observations", "human_decisions", "sync_runs")
    )


def _verifier(key: bytes, ledger_id: str) -> str:
    if not key:
        raise ConfigurationError("identity key is missing; restore the matching key")
    return hmac.new(
        key, f"worktrace:identity-key:v1:{ledger_id}".encode(), hashlib.sha256
    ).hexdigest()


def _key_matches(connection: sqlite3.Connection, key: bytes) -> bool:
    if not key:
        raise ConfigurationError("identity key is missing; restore the matching key")
    row = connection.execute("SELECT ledger_id, verifier FROM identity_key_binding").fetchone()
    if row is None:
        return False
    if not hmac.compare_digest(_verifier(key, str(row[0])), str(row[1])):
        raise ConfigurationError(
            "identity key does not match this ledger; restore the matching key"
        )
    return True


def _bind_key(connection: sqlite3.Connection, key: bytes) -> None:
    ledger_id = str(uuid.uuid4())
    connection.execute(
        "INSERT INTO identity_key_binding VALUES (1, ?, ?)",
        (ledger_id, _verifier(key, ledger_id)),
    )


def _accept_policy(connection: sqlite3.Connection, config: WorkTraceConfig, app_id: str) -> None:
    connection.execute(
        "INSERT INTO app_identity_policy(app_id, version, fingerprint) VALUES (?, ?, ?) "
        "ON CONFLICT(app_id) DO UPDATE SET version=excluded.version, "
        "fingerprint=excluded.fingerprint",
        (app_id, IDENTITY_POLICY_VERSION, identity_fingerprint(config)),
    )


def initialize_identity(
    connection: sqlite3.Connection, config: WorkTraceConfig, key: bytes
) -> None:
    """Enroll an empty ledger, never replace a populated ledger's unknown key."""
    with _transition(connection):
        connection.execute("UPDATE apps SET read_revision=read_revision")
        if _key_matches(connection, key):
            return
        if _populated(connection):
            raise ConfigurationError(
                "legacy identity key continuity requires explicit repair proof"
            )
        _bind_key(connection, key)
        for app in config.apps:
            if (
                connection.execute(
                    "SELECT 1 FROM app_identity_policy WHERE app_id=?", (app.id,)
                ).fetchone()
                is None
            ):
                _accept_policy(connection, config, app.id)


def mark_read_state_changed(connection: sqlite3.Connection, app_id: str) -> None:
    """Invalidate app reads in the caller's visible-state transaction."""
    connection.execute("UPDATE apps SET read_revision=read_revision+1 WHERE id=?", (app_id,))


def identity_policy_status(
    connection: sqlite3.Connection, config: WorkTraceConfig, app_id: str
) -> dict[str, Any]:
    config.app(app_id)
    policy = connection.execute(
        "SELECT * FROM app_identity_policy WHERE app_id=?", (app_id,)
    ).fetchone()
    warnings: list[str] = []
    valid = (
        policy is not None
        and int(policy["version"]) == IDENTITY_POLICY_VERSION
        and policy["fingerprint"] == identity_fingerprint(config)
    )
    if not valid:
        warnings.append("identity_policy_unaccepted: personal attribution requires identity repair")
    legacy = connection.execute(
        "SELECT 1 FROM actors a JOIN participations p ON p.actor_id=a.id "
        "JOIN source_objects s ON s.id=p.source_object_id "
        "WHERE s.app_id=? AND a.source!='manual' AND a.identity_policy_version<>? LIMIT 1",
        (app_id, IDENTITY_POLICY_VERSION),
    ).fetchone()
    if legacy is not None:
        valid = False
        warnings.append("identity_policy_legacy: unverified actor flags cannot support self claims")
    rebuild = bool(policy is not None and policy["rebuild_required"])
    if rebuild:
        warnings.append("identity_rebuild_required: rebuild references and candidates")
    targets = [
        str(row[0])
        for row in connection.execute(
            "SELECT DISTINCT target_id FROM identity_rereview WHERE app_id=? ORDER BY target_id",
            (app_id,),
        )
    ]
    if targets:
        warnings.append(
            "identity_requires_rereview: confirmed history was affected by identity repair"
        )
    return {
        "valid": valid,
        "version": IDENTITY_POLICY_VERSION,
        "fingerprint": identity_fingerprint(config),
        "rebuild_required": rebuild,
        "requires_rereview": targets,
        "warnings": warnings,
    }


def prepare_identity_import(
    connection: sqlite3.Connection, config: WorkTraceConfig, key: bytes, app_id: str
) -> None:
    """Resolve accepted policy and key before any source run or provider page."""
    config.app(app_id)
    with _transition(connection):
        connection.execute("UPDATE apps SET read_revision=read_revision WHERE id=?", (app_id,))
        if not _key_matches(connection, key):
            initialize_identity(connection, config, key)
        status = identity_policy_status(connection, config, app_id)
        if not status["valid"]:
            raise ConfigurationError("identity policy requires repair before importing")


def _check_legacy_proof(
    connection: sqlite3.Connection,
    config: WorkTraceConfig,
    key: bytes,
    proof_actor_id: str | None,
    proof_alias_index: int | None,
) -> None:
    aliases = config.identity.git_author_emails
    if (
        proof_actor_id is None
        or proof_alias_index is None
        or not 0 <= proof_alias_index < len(aliases)
    ):
        raise ConfigurationError(
            "legacy enrollment needs a known actor ID and configured alias index"
        )
    row = connection.execute(
        "SELECT email_hash FROM actors WHERE id=? AND source IN ('git','gitlab')",
        (proof_actor_id,),
    ).fetchone()
    if (
        row is None
        or row[0] is None
        or not hmac.compare_digest(
            str(row[0]), Redactor(key).hash_email(aliases[proof_alias_index])
        )
    ):
        raise ConfigurationError("legacy identity proof does not match; restore the matching key")


def _affected_targets(connection: sqlite3.Connection, app_id: str, objects: set[str]) -> list[str]:
    targets: set[str] = set()
    for lineage in decision_lineages(connection, active_only=False):
        if lineage.app_id != app_id:
            continue
        mentioned: set[str] = set()
        for decision in lineage.decisions:
            mentioned.update(snapshot_member_ids(decision.payload))
            member = decision.payload.get("source_object_id")
            if isinstance(member, str):
                mentioned.add(member)
        if mentioned & objects:
            targets.update(lineage.candidate_ids | lineage.contribution_ids)
    # Earlier confirm records may predate immutable creation snapshots.
    scopes = decision_scope_map(connection)
    for decision in decision_stream(connection):
        if scopes.get(decision.id) != app_id or decision.action not in {
            "confirm",
            "confirm_candidate",
        }:
            continue
        members = snapshot_member_ids(decision.payload)
        members.update(
            str(row[0])
            for row in connection.execute(
                "SELECT source_object_id FROM candidate_members WHERE candidate_id=?",
                (decision.target_id,),
            )
        )
        if members & objects:
            targets.add(decision.target_id)
    return sorted(targets)


def _proposal(
    connection: sqlite3.Connection,
    config: WorkTraceConfig,
    key: bytes,
    app_id: str,
    verified_self_ids: dict[str, str],
    verified_gitlab_email_hashes: frozenset[str],
) -> dict[str, Any]:
    hashes = {Redactor(key).hash_email(email) for email in config.identity.git_author_emails}
    actors = list(
        connection.execute(
            "SELECT DISTINCT a.* FROM actors a JOIN participations p ON p.actor_id=a.id "
            "JOIN source_objects s ON s.id=p.source_object_id WHERE s.app_id=? ORDER BY a.id",
            (app_id,),
        )
    )
    changes: list[dict[str, Any]] = []
    unresolved: list[str] = []
    blocked_providers: list[str] = []
    affected_apps: set[str] = {app_id}
    objects: set[str] = set()
    policy = connection.execute(
        "SELECT * FROM app_identity_policy WHERE app_id=?", (app_id,)
    ).fetchone()
    policy_changed = (
        policy is None
        or int(policy["version"]) != IDENTITY_POLICY_VERSION
        or policy["fingerprint"] != identity_fingerprint(config)
    )
    for actor in actors:
        source = str(actor["source"])
        if source == "manual":
            continue
        email_identity = source == "git" or (
            source == "gitlab" and actor["external_actor_id"] == actor["email_hash"]
        )
        if not email_identity:
            # Config alone does not prove which account authenticated to a provider.
            if source in verified_self_ids:
                desired = actor["external_actor_id"] == verified_self_ids[source]
            elif policy_changed or int(actor["identity_policy_version"]) != IDENTITY_POLICY_VERSION:
                unresolved.append(str(actor["id"]))
                blocked_providers.append(str(actor["id"]))
                continue
            else:
                continue
        else:
            allowed = hashes | (verified_gitlab_email_hashes if source == "gitlab" else set())
            desired = actor["email_hash"] is not None and actor["email_hash"] in allowed
            if (
                source == "gitlab"
                and not desired
                and bool(actor["is_self"])
                and int(actor["identity_policy_version"]) == IDENTITY_POLICY_VERSION
                and source not in verified_self_ids
            ):
                # A previously verified provider email may not be a configured Git alias.
                unresolved.append(str(actor["id"]))
                blocked_providers.append(str(actor["id"]))
                continue
            if actor["email_hash"] is None:
                unresolved.append(str(actor["id"]))
        if (
            bool(actor["is_self"]) == desired
            and int(actor["identity_policy_version"]) == IDENTITY_POLICY_VERSION
        ):
            continue
        changes.append(
            {"actor_id": str(actor["id"]), "before": bool(actor["is_self"]), "after": desired}
        )
        for reach in connection.execute(
            "SELECT DISTINCT s.id, s.app_id FROM participations p "
            "JOIN source_objects s ON s.id=p.source_object_id WHERE p.actor_id=?",
            (actor["id"],),
        ):
            objects.add(str(reach[0]))
            affected_apps.add(str(reach[1]))
    confirmed_by_app = {
        affected_app: _affected_targets(connection, affected_app, objects)
        for affected_app in sorted(affected_apps)
    }
    report: dict[str, Any] = {
        "app_id": app_id,
        "policy_version": IDENTITY_POLICY_VERSION,
        "fingerprint": identity_fingerprint(config),
        "changes": changes,
        "promotions": sum(not item["before"] and item["after"] for item in changes),
        "demotions": sum(item["before"] and not item["after"] for item in changes),
        "unresolved_actor_ids": unresolved,
        "provider_verification_required": blocked_providers,
        "verified_sources": sorted(verified_self_ids),
        "affected_apps": sorted(affected_apps),
        "confirmed_targets": sorted(
            {target for targets in confirmed_by_app.values() for target in targets}
        ),
        "confirmed_targets_by_app": confirmed_by_app,
        "cross_app_scope_required": affected_apps != {app_id},
    }
    report["previous_policy"] = dict(policy) if policy is not None else None
    report["proposal_token"] = hashlib.sha256(
        json.dumps(report, sort_keys=True).encode()
    ).hexdigest()
    return report


def repair_identities(
    connection: sqlite3.Connection,
    config: WorkTraceConfig,
    key: bytes,
    app_id: str,
    *,
    apply: bool = False,
    proof_actor_id: str | None = None,
    proof_alias_index: int | None = None,
    expected_proposal: str | None = None,
    verified_self_ids: dict[str, str] | None = None,
    verified_gitlab_email_hashes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Report exact-identity reconciliation, or apply within the caller's commit."""
    config.app(app_id)
    verified = verified_self_ids or {}
    if any(source not in {"jira", "gitlab"} for source in verified):
        raise ConfigurationError("unsupported verified identity source")
    if "jira" in verified and verified["jira"] != config.identity.jira_account_id:
        raise ConfigurationError("verified Jira identity differs from configured account")
    if "gitlab" in verified:
        numeric = verified["gitlab"]
        configured = config.identity.gitlab_user_id
        if (
            not numeric.isascii()
            or not numeric.isdecimal()
            or int(numeric) < 1
            or (configured is not None and str(configured) != numeric)
            or (configured is None and not config.identity.gitlab_username)
        ):
            raise ConfigurationError("verified GitLab identity differs from configured account")
    if verified_gitlab_email_hashes and "gitlab" not in verified:
        raise ConfigurationError("verified GitLab email requires verified account identity")
    with _transition(connection):
        if apply:
            # Reserve the writer before reading a proposal; stale WAL snapshots fail safely.
            connection.execute("UPDATE apps SET read_revision=read_revision WHERE id=?", (app_id,))
        bound = _key_matches(connection, key)
        if not bound:
            _check_legacy_proof(connection, config, key, proof_actor_id, proof_alias_index)
        report = _proposal(connection, config, key, app_id, verified, verified_gitlab_email_hashes)
        if expected_proposal is not None and expected_proposal != report["proposal_token"]:
            raise ConfigurationError("identity proposal changed; repeat the dry run")
        report["applied"] = False
        if not apply:
            return report
        if report["cross_app_scope_required"]:
            raise ScopeViolation(
                "identity repair reaches other apps; expanded scope requires review"
            )
        if not bound:
            _bind_key(connection, key)
        for change in report["changes"]:
            connection.execute(
                "UPDATE actors SET is_self=?, identity_policy_version=? WHERE id=?",
                (int(change["after"]), IDENTITY_POLICY_VERSION, change["actor_id"]),
            )
        _accept_policy(connection, config, app_id)
        if report["provider_verification_required"]:
            connection.execute("UPDATE app_identity_policy SET version=0 WHERE app_id=?", (app_id,))
        connection.execute(
            "UPDATE app_identity_policy SET rebuild_required=1 WHERE app_id=?", (app_id,)
        )
        mark_read_state_changed(connection, app_id)
        report["applied"] = True
        repair_id = "identity-repair:" + str(uuid.uuid4())
        connection.execute(
            "INSERT INTO identity_repair_audit VALUES (?, ?, ?, ?)",
            (repair_id, app_id, datetime.now(UTC).isoformat(), json.dumps(report, sort_keys=True)),
        )
        for target in report["confirmed_targets"]:
            connection.execute(
                "INSERT INTO identity_rereview VALUES (?, ?, ?)", (app_id, target, repair_id)
            )
        report["repair_id"] = repair_id
        return report


def finish_identity_rebuild(
    connection: sqlite3.Connection, config: WorkTraceConfig, app_id: str
) -> None:
    """Only clear derived-state invalidation after both canonical rebuilds succeed."""
    if not identity_policy_status(connection, config, app_id)["valid"]:
        raise ConfigurationError("identity policy requires repair before rebuilding")
    connection.execute(
        "UPDATE app_identity_policy SET rebuild_required=0 WHERE app_id=?", (app_id,)
    )
    mark_read_state_changed(connection, app_id)
