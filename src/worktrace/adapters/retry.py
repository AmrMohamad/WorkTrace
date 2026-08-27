"""Bounded, provider-safe HTTP retry behavior."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from random import random

import httpx

from worktrace.errors import (
    InvalidCredentials,
    PermanentSourceError,
    PermissionDenied,
    RetryExhausted,
    SourceObjectUnavailable,
)

MAX_HTTP_RESPONSE_BYTES = 10_000_000


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 5
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 4.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays must not be negative")

    def delay_for(
        self,
        attempt: int,
        retry_after: str | None = None,
        *,
        random_value: Callable[[], float] = random,
    ) -> float:
        ceiling = min(
            self.base_delay_seconds * (2.0 ** max(attempt - 1, 0)),
            self.max_delay_seconds,
        )
        if retry_after is not None:
            parsed_retry_after = _parse_retry_after(retry_after)
            if parsed_retry_after is not None:
                return min(max(parsed_retry_after, 0.0), self.max_delay_seconds)
        sample = random_value()
        if not 0.0 <= sample <= 1.0:
            raise ValueError("random_value must return a value between zero and one")
        return ceiling * sample


DEFAULT_RETRY_POLICY = RetryPolicy()


def _parse_retry_after(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max((parsed.astimezone(UTC) - datetime.now(UTC)).total_seconds(), 0.0)


def _read_bounded_response(response: httpx.Response) -> httpx.Response:
    declared_length = response.headers.get("Content-Length")
    if declared_length is not None:
        normalized_length = declared_length.strip()
        if normalized_length.isdecimal() and int(normalized_length) > MAX_HTTP_RESPONSE_BYTES:
            raise PermanentSourceError("Provider response exceeded the configured safety bound")

    content = bytearray()
    for chunk in response.iter_bytes():
        if len(content) + len(chunk) > MAX_HTTP_RESPONSE_BYTES:
            raise PermanentSourceError("Provider response exceeded the configured safety bound")
        content.extend(chunk)
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        content=bytes(content),
        request=response.request,
        extensions=response.extensions,
        history=response.history,
        default_encoding=response.default_encoding,
    )


def request_with_retry(
    client: httpx.Client,
    method: str,
    endpoint: str,
    *,
    params: Mapping[str, str | int] | None = None,
    json_body: object | None = None,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    random_value: Callable[[], float] = random,
    exact_object: bool = False,
) -> httpx.Response:
    """Request a fixed endpoint, retrying only timeouts, 429, and 5xx."""

    for attempt in range(1, policy.max_attempts + 1):
        try:
            request = client.build_request(
                method,
                endpoint,
                params=params,
                json=json_body,
            )
            response = client.send(request, stream=True, follow_redirects=False)
            try:
                status = response.status_code
                if 200 <= status < 300:
                    return _read_bounded_response(response)
                if status == 401:
                    raise InvalidCredentials("Provider rejected the configured credentials")
                if status == 403:
                    raise PermissionDenied("Provider denied access to the configured scope")
                if status == 404 and exact_object:
                    raise SourceObjectUnavailable("Provider exact object is unavailable")
                if status == 429 or 500 <= status < 600:
                    if attempt == policy.max_attempts:
                        raise RetryExhausted(
                            f"Provider remained unavailable after {policy.max_attempts} attempts "
                            f"(HTTP {status})"
                        )
                    delay = policy.delay_for(
                        attempt,
                        response.headers.get("Retry-After"),
                        random_value=random_value,
                    )
                    response.close()
                    sleep(delay)
                    continue
                raise PermanentSourceError(f"Provider request failed with HTTP {status}")
            finally:
                response.close()
        except httpx.RequestError:
            if attempt == policy.max_attempts:
                raise RetryExhausted(
                    f"Provider transport failed after {policy.max_attempts} attempts"
                ) from None
            sleep(policy.delay_for(attempt, random_value=random_value))
            continue

    raise AssertionError("bounded retry loop exited unexpectedly")
