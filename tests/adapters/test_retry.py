from __future__ import annotations

import httpx
import pytest

from worktrace.adapters.retry import RetryPolicy, request_with_retry
from worktrace.errors import (
    InvalidCredentials,
    PermanentSourceError,
    RetryExhausted,
    SourceObjectUnavailable,
)


def test_retry_is_bounded_for_429_and_honors_capped_retry_after() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "999"}, request=request)

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(RetryExhausted, match="3 attempts"),
    ):
        request_with_retry(
            client,
            "GET",
            "/fixed",
            policy=RetryPolicy(max_attempts=3, max_delay_seconds=2),
            sleep=sleeps.append,
        )

    assert calls == 3
    assert sleeps == [2, 2]


def test_credentials_failure_is_immediate_and_sanitized() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="token=do-not-leak", request=request)

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(InvalidCredentials) as error,
    ):
        request_with_retry(client, "GET", "/fixed", sleep=lambda _delay: None)

    assert calls == 1
    assert "do-not-leak" not in str(error.value)


def test_default_retry_uses_five_attempts_and_injectable_full_jitter() -> None:
    calls = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, request=request)

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(RetryExhausted, match="5 attempts"),
    ):
        request_with_retry(
            client,
            "GET",
            "/fixed",
            sleep=sleeps.append,
            random_value=lambda: 0.5,
        )

    assert calls == 5
    assert sleeps == [0.125, 0.25, 0.5, 1.0]


def test_only_exact_object_404_is_classified_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="sensitive provider body", request=request)

    with httpx.Client(
        base_url="https://provider.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(SourceObjectUnavailable, match="exact object"):
            request_with_retry(client, "GET", "/objects/1", exact_object=True)
        with pytest.raises(PermanentSourceError, match="HTTP 404"):
            request_with_retry(client, "GET", "/collections")
