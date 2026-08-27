from __future__ import annotations

import gc
import gzip
import tracemalloc
import zlib
from collections.abc import Iterator

import httpx
import pytest

from worktrace.adapters import retry as retry_module
from worktrace.adapters.retry import RetryPolicy, request_with_retry
from worktrace.errors import (
    InvalidCredentials,
    PermanentSourceError,
    RetryExhausted,
    SourceObjectUnavailable,
)


class _TrackingStream(httpx.SyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.iterations = 0
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        self.iterations += 1
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


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


def test_request_negotiates_only_bounded_encodings_without_dropping_client_headers() -> None:
    observed_headers: httpx.Headers | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_headers
        observed_headers = request.headers
        return httpx.Response(200, json={"ok": True}, request=request)

    with httpx.Client(
        base_url="https://provider.example",
        headers={
            "Authorization": "Bearer fixture-token",
            "X-Provider-Header": "preserved",
            "Accept-Encoding": "br, zstd",
        },
        transport=httpx.MockTransport(handler),
    ) as client:
        response = request_with_retry(client, "GET", "/fixed")

    assert response.json() == {"ok": True}
    assert observed_headers is not None
    assert observed_headers["Accept-Encoding"] == "gzip, deflate"
    assert observed_headers["Authorization"] == "Bearer fixture-token"
    assert observed_headers["X-Provider-Header"] == "preserved"


def test_oversized_content_length_is_rejected_before_stream_iteration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retry_module, "MAX_HTTP_RESPONSE_BYTES", 16)
    stream = _TrackingStream([b"body must not be read"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "17"},
            stream=stream,
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(PermanentSourceError, match="safety bound"),
    ):
        request_with_retry(client, "GET", "/fixed")

    assert stream.iterations == 0
    assert stream.closed is True


def test_decoded_chunked_body_is_aborted_when_it_crosses_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retry_module, "MAX_HTTP_RESPONSE_BYTES", 64)
    stream = _TrackingStream([gzip.compress(b"x" * 512)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Transfer-Encoding": "chunked"},
            stream=stream,
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(PermanentSourceError, match="safety bound"),
    ):
        request_with_retry(client, "GET", "/fixed")

    assert stream.iterations == 1
    assert stream.closed is True


def test_compressed_encoded_body_is_aborted_when_it_crosses_the_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(retry_module, "MAX_HTTP_RESPONSE_BYTES", 16)
    stream = _TrackingStream([gzip.compress(b"")])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Transfer-Encoding": "chunked"},
            stream=stream,
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(PermanentSourceError, match="safety bound"),
    ):
        request_with_retry(client, "GET", "/fixed")

    assert stream.iterations == 1
    assert stream.closed is True


def test_compressed_overflow_does_not_materialize_the_full_decoded_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limit = 64 * 1024
    decoded_size = 8 * 1024 * 1024
    monkeypatch.setattr(retry_module, "MAX_HTTP_RESPONSE_BYTES", limit)
    stream = _TrackingStream([gzip.compress(b"x" * decoded_size)])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip", "Transfer-Encoding": "chunked"},
            stream=stream,
            request=request,
        )

    with httpx.Client(
        base_url="https://provider.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        gc.collect()
        tracemalloc.start()
        try:
            with pytest.raises(PermanentSourceError, match="safety bound"):
                request_with_retry(client, "GET", "/fixed")
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert peak_bytes < decoded_size // 4
    assert stream.closed is True


def _raw_deflate(content: bytes) -> bytes:
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    return compressor.compress(content) + compressor.flush()


@pytest.mark.parametrize(
    ("encoding", "encoded"),
    [
        ("gzip", gzip.compress(b'{"ok":true}')),
        ("deflate", zlib.compress(b'{"ok":true}')),
        ("deflate", _raw_deflate(b'{"ok":true}')),
    ],
)
def test_supported_compressed_responses_remain_decoded(encoding: str, encoded: bytes) -> None:
    stream = _TrackingStream([encoded])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": encoding},
            stream=stream,
            request=request,
        )

    with httpx.Client(
        base_url="https://provider.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        response = request_with_retry(client, "GET", "/fixed")

    assert response.json() == {"ok": True}
    assert stream.closed is True


@pytest.mark.parametrize(
    ("encoding", "encoded"),
    [
        ("gzip", gzip.compress(b'{"ok":true}')),
        ("deflate", zlib.compress(b'{"ok":true}')),
        ("deflate", _raw_deflate(b'{"ok":true}')),
    ],
)
def test_fragmented_compressed_responses_decode_from_every_early_byte_boundary(
    encoding: str,
    encoded: bytes,
) -> None:
    for split_at in range(1, min(len(encoded), 8)):
        stream = _TrackingStream([encoded[:split_at], encoded[split_at:]])

        def handler(
            request: httpx.Request,
            stream: _TrackingStream = stream,
            encoding: str = encoding,
        ) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Encoding": encoding},
                stream=stream,
                request=request,
            )

        with httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client:
            response = request_with_retry(client, "GET", "/fixed")

        assert response.json() == {"ok": True}
        assert stream.closed is True


@pytest.mark.parametrize(
    ("encoding", "encoded"),
    [
        ("gzip", gzip.compress(b'{"ok":true}') + b"trailing data"),
        ("gzip", gzip.compress(b'{"ok":true}') + gzip.compress(b"second member")),
        ("gzip", gzip.compress(b'{"ok":true}')[:-4]),
        ("deflate", b"\x00"),
    ],
)
def test_malformed_or_concatenated_compressed_response_is_permanent(
    encoding: str,
    encoded: bytes,
) -> None:
    stream = _TrackingStream([encoded])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Content-Encoding": encoding},
            stream=stream,
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(PermanentSourceError, match="could not be decoded"),
    ):
        request_with_retry(client, "GET", "/fixed")

    assert calls == 1
    assert stream.closed is True


@pytest.mark.parametrize("content_encoding", ["br", "gzip, deflate"])
def test_unsupported_or_multiple_content_encoding_is_rejected_before_read(
    content_encoding: str,
) -> None:
    stream = _TrackingStream([b"body must not be read"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": content_encoding},
            stream=stream,
            request=request,
        )

    with (
        httpx.Client(
            base_url="https://provider.example",
            transport=httpx.MockTransport(handler),
        ) as client,
        pytest.raises(PermanentSourceError, match="unsupported content encoding"),
    ):
        request_with_retry(client, "GET", "/fixed")

    assert stream.iterations == 0
    assert stream.closed is True


def test_transient_response_is_closed_before_retry_backoff() -> None:
    transient_stream = _TrackingStream([b"retry later"])
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, stream=transient_stream, request=request)
        return httpx.Response(200, json={"ok": True}, request=request)

    def assert_closed_before_sleep(_: float) -> None:
        assert transient_stream.closed is True

    with httpx.Client(
        base_url="https://provider.example",
        transport=httpx.MockTransport(handler),
    ) as client:
        response = request_with_retry(
            client,
            "GET",
            "/retry",
            policy=RetryPolicy(max_attempts=2, base_delay_seconds=0),
            sleep=assert_closed_before_sleep,
        )

    assert response.json() == {"ok": True}
    assert calls == 2
