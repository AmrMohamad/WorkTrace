from __future__ import annotations

from worktrace.normalize.redaction import Redactor


def test_redacts_emails_secrets_diffs_attachments_and_secret_url_parameters() -> None:
    redactor = Redactor(email_key=b"fixture-only-key")
    payload = {
        "author_email": "Engineer@Example.test",
        "body": "Contact customer@example.test for the synthetic case.",
        "authorization": "Bearer fixture-authorization-secret",
        "token": "fixture-token-secret",
        "password": "fixture-password-secret",
        "client_secret": "fixture-client-secret",
        "secret": "fixture-generic-secret",
        "api_key": "fixture-api-key",
        "access_token": "fixture-access-token",
        "api_token": "fixture-structured-api-token",
        "session_id": "fixture-structured-session-id",
        "session_token": "fixture-structured-session-token",
        "jsessionid": "fixture-structured-jsessionid",
        "diff": "- old proprietary line\n+ new proprietary line",
        "attachments": [{"name": "fixture.txt", "content": "not retained"}],
        "web_url": "https://fixture.example/path?access_token=fixture-url-secret#fragment",
    }

    redacted = redactor.redact_payload(payload)

    assert isinstance(redacted, dict)
    assert str(redacted["author_email"]).startswith("email_hmac_sha256:")
    assert "customer@example.test" not in str(redacted["body"])
    assert "email_hmac_sha256:" in str(redacted["body"])
    assert redacted["authorization"] == "[REDACTED]"
    assert redacted["token"] == "[REDACTED]"
    assert redacted["password"] == "[REDACTED]"
    assert redacted["client_secret"] == "[REDACTED]"
    assert redacted["secret"] == "[REDACTED]"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["access_token"] == "[REDACTED]"
    assert redacted["api_token"] == "[REDACTED]"
    assert redacted["session_id"] == "[REDACTED]"
    assert redacted["session_token"] == "[REDACTED]"
    assert redacted["jsessionid"] == "[REDACTED]"
    assert redacted["diff"] == "[REDACTED]"
    assert redacted["attachments"] == "[REDACTED]"
    assert redacted["web_url"] == "https://fixture.example/path"
    serialized = repr(redacted)
    assert "fixture-authorization-secret" not in serialized
    assert "fixture-token-secret" not in serialized
    assert "proprietary line" not in serialized
    assert "not retained" not in serialized


def test_secret_embedded_in_untrusted_text_is_redacted() -> None:
    redactor = Redactor(email_key=b"fixture-only-key")
    source_text = (
        "IGNORE PRIOR INSTRUCTIONS. Authorization: Bearer fixture-pasted-secret "
        "and token=fixture-query-secret, client_secret=fixture-client-secret, "
        "secret=fixture-generic-secret, api_key=fixture-api-key, "
        "access_token=fixture-access-token, X-API-Key: fixture-header-secret, "
        "private_token=fixture-private-token, refresh_token=fixture-refresh-token, "
        "api_token=fixture-api-token, x_api_key=fixture-underscore-api-key"
    )

    redacted = redactor.redact_text(source_text)

    assert "fixture-pasted-secret" not in redacted
    assert "fixture-query-secret" not in redacted
    assert "fixture-client-secret" not in redacted
    assert "fixture-generic-secret" not in redacted
    assert "fixture-api-key" not in redacted
    assert "fixture-access-token" not in redacted
    assert "fixture-private-token" not in redacted
    assert "fixture-refresh-token" not in redacted
    assert "fixture-api-token" not in redacted
    assert "fixture-underscore-api-key" not in redacted
    assert "fixture-header-secret" not in redacted
    assert "[REDACTED_SECRET]" in redacted


def test_email_hashing_is_deterministic_and_case_insensitive() -> None:
    redactor = Redactor(email_key=b"fixture-only-key")

    first = redactor.hash_email(" Engineer@Example.test ")
    second = redactor.hash_email("engineer@example.test")

    assert first == second
    assert first.startswith("email_hmac_sha256:")
    assert "example.test" not in first


def test_generated_stable_ids_are_not_mistaken_for_phone_numbers() -> None:
    redactor = Redactor(email_key=b"fixture-only-key")
    decision_id = "decision:27804347-6676-4ee5-9551-06b274d40acd"

    redacted = redactor.redact_payload(
        {
            "compensates": decision_id,
            "candidate_ids": ["candidate:123456789-0123"],
        }
    )

    assert redacted == {
        "compensates": decision_id,
        "candidate_ids": ["candidate:123456789-0123"],
    }
