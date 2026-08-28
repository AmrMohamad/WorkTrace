from __future__ import annotations

from worktrace.adapters.base import ParticipationRole
from worktrace.normalize import (
    Redactor,
    actor_identity,
    build_record,
    exact_jira_keys,
    normalize_timestamp,
    parse_git_trailers,
)


def test_redaction_happens_before_payload_is_returned() -> None:
    redactor = Redactor(b"test-only-key", max_text_chars=200)

    payload = redactor.redact_payload(
        {
            "email": "Person@Example.com",
            "description": "Contact person@example.com",
            "diff": "complete proprietary diff",
            "url": "https://user:secret@example.com/path?private=1#fragment",
        }
    )

    rendered = repr(payload)
    assert "Person@Example.com" not in rendered
    assert "person@example.com" not in rendered
    assert "complete proprietary diff" not in rendered
    assert "private=1" not in rendered
    assert "user:secret" not in rendered
    assert "email_hmac_sha256:" in rendered


def test_email_hash_is_normalized_stable_and_keyed() -> None:
    first = Redactor(b"first-key")
    second = Redactor(b"second-key")

    assert first.hash_email(" Person@Example.com ") == first.hash_email("person@example.com")
    assert first.hash_email("person@example.com") != second.hash_email("person@example.com")


def test_redaction_bounds_nested_and_repeated_provider_content() -> None:
    redactor = Redactor(b"test-only-key", max_collection_items=2, max_depth=2)

    payload = redactor.redact_payload({"items": [{"nested": "value"}, 2, 3]})

    assert payload == {
        "items": [
            "[TRUNCATED_DEPTH]",
            2,
            "[TRUNCATED_COLLECTION]",
        ]
    }


def test_exact_jira_keys_respect_configured_scope_and_boundaries() -> None:
    text = "ABC-12, abc-12, XABC-13, ABC-0, OTHER-2 and MOB-7"

    assert exact_jira_keys(text, ("ABC", "MOB")) == ("ABC-12", "MOB-7")


def test_git_trailers_preserve_distinct_roles() -> None:
    trailers = parse_git_trailers(
        "Subject\n\nCo-authored-by: Pair Dev <pair@example.com>\n"
        "Reviewed-by: Reviewer <review@example.com>"
    )

    assert [item.role for item in trailers] == [
        ParticipationRole.CO_AUTHOR,
        ParticipationRole.REVIEWER,
    ]


def test_record_identity_and_payload_hash_are_deterministic() -> None:
    redactor = Redactor(b"test-only-key")
    actor = actor_identity(
        source_kind="git",
        source_instance="repo-a",
        redactor=redactor,
        provider_actor_id=None,
        display_name="A Dev",
        email="a@example.com",
    )
    first = build_record(
        source_kind="git",
        source_instance="repo-a",
        object_type="commit",
        external_id="a" * 40,
        app_id="sample_store",
        observed_at="2026-01-01T00:00:00Z",
        source_updated_at="2025-12-31T23:00:00+00:00",
        payload={"subject": "ABC-1"},
        redactor=redactor,
    )
    second = build_record(
        source_kind="git",
        source_instance="repo-a",
        object_type="commit",
        external_id="a" * 40,
        app_id="sample_store",
        observed_at="2026-02-01T00:00:00Z",
        source_updated_at="2025-12-31T23:00:00Z",
        payload={"subject": "ABC-1"},
        redactor=redactor,
    )

    assert first.identity.stable_id == second.identity.stable_id
    assert first.payload_hash == second.payload_hash
    assert actor.email_hash is not None
    assert "a@example.com" not in repr(actor)
    assert normalize_timestamp("2026-01-01T02:00:00+02:00") == "2026-01-01T00:00:00Z"
