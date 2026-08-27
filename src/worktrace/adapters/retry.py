"""Bounded, provider-safe HTTP retry behavior."""

from __future__ import annotations

import time
import zlib
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
_DECODE_OUTPUT_CHUNK_BYTES = 64 * 1024
_ACCEPT_ENCODING = "gzip, deflate"
_SUPPORTED_CONTENT_ENCODINGS = frozenset({"identity", "gzip", "deflate"})


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


class _BoundedZlibDecoder:
    def __init__(self, window_bits: int) -> None:
        self._decompressor = zlib.decompressobj(window_bits)

    @property
    def unconsumed_input(self) -> bytes:
        return self._decompressor.unconsumed_tail

    @property
    def has_trailing_input(self) -> bool:
        return bool(self._decompressor.unused_data)

    @property
    def is_complete(self) -> bool:
        return self._decompressor.eof

    def decode(self, data: bytes, *, max_output: int) -> bytes:
        try:
            return self._decompressor.decompress(data, max_output)
        except zlib.error:
            raise PermanentSourceError("Provider response could not be decoded") from None


def _content_encoding(response: httpx.Response) -> str:
    encodings = [
        value.strip().lower()
        for value in response.headers.get_list("Content-Encoding", split_commas=True)
        if value.strip()
    ]
    if not encodings:
        return "identity"
    if len(encodings) != 1 or encodings[0] not in _SUPPORTED_CONTENT_ENCODINGS:
        raise PermanentSourceError("Provider response used an unsupported content encoding")
    return encodings[0]


def _is_zlib_wrapped_deflate(prefix: bytes) -> bool:
    """Recognize a complete zlib CMF/FLG header without decoding provider data."""

    if len(prefix) != 2:
        raise ValueError("deflate detection requires exactly two bytes")
    cmf, flg = prefix
    return (cmf & 0x0F) == zlib.DEFLATED and (cmf >> 4) <= 7 and ((cmf << 8) + flg) % 31 == 0


def _response_decoder(
    encoding: str, *, deflate_prefix: bytes | None = None
) -> _BoundedZlibDecoder | None:
    if encoding == "gzip":
        return _BoundedZlibDecoder(zlib.MAX_WBITS | 16)
    if encoding == "deflate":
        if deflate_prefix is None or len(deflate_prefix) < 2:
            raise ValueError("deflate decoder requires a two-byte prefix")
        window_bits = (
            zlib.MAX_WBITS if _is_zlib_wrapped_deflate(deflate_prefix[:2]) else -zlib.MAX_WBITS
        )
        return _BoundedZlibDecoder(window_bits)
    return None


def _append_bounded(content: bytearray, chunk: bytes) -> None:
    if len(content) + len(chunk) > MAX_HTTP_RESPONSE_BYTES:
        raise PermanentSourceError("Provider response exceeded the configured safety bound")
    content.extend(chunk)


def _decode_bounded(
    content: bytearray,
    decoder: _BoundedZlibDecoder,
    raw_chunk: bytes,
) -> None:
    pending = raw_chunk
    while True:
        remaining = MAX_HTTP_RESPONSE_BYTES - len(content)
        max_output = min(_DECODE_OUTPUT_CHUNK_BYTES, remaining + 1)
        decoded_chunk = decoder.decode(pending, max_output=max_output)
        if decoder.has_trailing_input:
            raise PermanentSourceError("Provider response could not be decoded")
        _append_bounded(content, decoded_chunk)
        pending = decoder.unconsumed_input
        if pending:
            continue
        if len(decoded_chunk) == max_output:
            pending = b""
            continue
        return


def _read_bounded_response(response: httpx.Response) -> httpx.Response:
    declared_length = response.headers.get("Content-Length")
    if declared_length is not None:
        normalized_length = declared_length.strip()
        if normalized_length.isdecimal() and int(normalized_length) > MAX_HTTP_RESPONSE_BYTES:
            raise PermanentSourceError("Provider response exceeded the configured safety bound")

    encoding = _content_encoding(response)
    decoder = _response_decoder(encoding) if encoding == "gzip" else None
    content = bytearray()
    if response.is_stream_consumed:
        _append_bounded(content, response.content)
    else:
        encoded_bytes = 0
        deflate_prefix = bytearray()
        for raw_chunk in response.iter_raw(chunk_size=_DECODE_OUTPUT_CHUNK_BYTES):
            encoded_bytes += len(raw_chunk)
            if encoded_bytes > MAX_HTTP_RESPONSE_BYTES:
                raise PermanentSourceError("Provider response exceeded the configured safety bound")
            if decoder is None:
                if encoding == "deflate":
                    deflate_prefix.extend(raw_chunk)
                    if len(deflate_prefix) < 2:
                        continue
                    decoder = _response_decoder("deflate", deflate_prefix=bytes(deflate_prefix[:2]))
                    raw_chunk = bytes(deflate_prefix)
                    deflate_prefix.clear()
                else:
                    _append_bounded(content, raw_chunk)
                    continue
            if decoder is not None:
                _decode_bounded(content, decoder, raw_chunk)
        if decoder is not None and (not decoder.is_complete or decoder.has_trailing_input):
            raise PermanentSourceError("Provider response could not be decoded")
        if encoding == "deflate" and decoder is None:
            raise PermanentSourceError("Provider response could not be decoded")

    decoded_headers = response.headers.copy()
    if encoding != "identity":
        for header in ("Content-Encoding", "Content-Length", "Transfer-Encoding"):
            decoded_headers.pop(header, None)
    return httpx.Response(
        response.status_code,
        headers=decoded_headers,
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
                headers={"Accept-Encoding": _ACCEPT_ENCODING},
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
