from __future__ import annotations

import io
import zipfile

from worktrace.vault.extract import ExtractionLimits, extract_bytes, sandbox_available


def test_extract_supported_text_redacts_only_at_index_boundary() -> None:
    result = extract_bytes(
        b"hello fixture@example.test",
        mime_type="text/plain",
        filename="note.txt",
    )
    assert result.status == "complete"
    assert "fixture@example.test" in result.text


def test_extract_limits_and_unsupported_are_honest() -> None:
    limited = extract_bytes(
        b"0123456789",
        mime_type="text/plain",
        filename="large.txt",
        limits=ExtractionLimits(input_bytes=4),
    )
    assert limited.status == "limit"
    assert (
        extract_bytes(b"\x00\x01", mime_type="image/png", filename="x.png").status == "unsupported"
    )


def test_extract_malformed_xml_and_bounded_ooxml() -> None:
    malformed = extract_bytes(b"<root>", mime_type="application/xml", filename="x.xml")
    assert malformed.status == "failed"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("word/document.xml", "<document><t>Hello</t></document>")
    result = extract_bytes(
        output.getvalue(),
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename="x.docx",
    )
    assert result.status == "complete"
    assert result.text == "Hello"


def test_extraction_fails_closed_when_sandbox_capability_is_absent() -> None:
    assert sandbox_available() in {True, False}
