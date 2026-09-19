"""Bounded, credential-free extraction for Jira vault originals."""

from __future__ import annotations

import html.parser
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, cast

from defusedxml import ElementTree  # type: ignore[import-untyped]

MAX_INPUT_BYTES = 25 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_PAGES = 1_000
MAX_OUTPUT_CHARS = 1_000_000
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_INFLATED_BYTES = 100 * 1024 * 1024
EXTRACTION_VERSION = "1"


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    input_bytes: int = MAX_INPUT_BYTES
    decompressed_bytes: int = MAX_DECOMPRESSED_BYTES
    pages: int = MAX_PAGES
    output_chars: int = MAX_OUTPUT_CHARS
    timeout_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    status: str
    text: str
    pages: int
    chars: int
    parser_version: str = EXTRACTION_VERSION
    exit_code: int = 0
    reason: str | None = None


DEFAULT_LIMITS = ExtractionLimits()


class _HTMLText(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _bounded_text(text: str, limits: ExtractionLimits) -> ExtractionResult:
    if len(text) > limits.output_chars:
        return ExtractionResult(
            "limit", text[: limits.output_chars], 0, limits.output_chars, reason="output_chars"
        )
    return ExtractionResult("complete" if text else "unsupported", text, 0, len(text))


def _xml_text(data: bytes, limits: ExtractionLimits) -> ExtractionResult:
    if len(data) > limits.decompressed_bytes:
        return ExtractionResult("limit", "", 0, 0, reason="decompressed_bytes")
    try:
        root = ElementTree.fromstring(data)
    except Exception:
        return ExtractionResult("failed", "", 0, 0, reason="malformed_xml")
    return _bounded_text(
        " ".join(value.strip() for value in root.itertext() if value.strip()), limits
    )


def _ooxml_text(data: bytes, limits: ExtractionLimits) -> ExtractionResult:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                return ExtractionResult("limit", "", 0, 0, reason="zip_entries")
            total = 0
            parts: list[str] = []
            for entry in entries:
                if entry.is_dir():
                    continue
                total += entry.file_size
                if total > MAX_ZIP_INFLATED_BYTES:
                    return ExtractionResult("limit", "", 0, 0, reason="zip_inflated_bytes")
                if entry.filename.lower().endswith((".xml", ".rels")):
                    parts.append(_xml_text(archive.read(entry), limits).text)
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError):
        return ExtractionResult("failed", "", 0, 0, reason="malformed_ooxml")
    return _bounded_text("\n".join(part for part in parts if part), limits)


def extract_bytes(
    data: bytes,
    *,
    mime_type: str | None,
    filename: str,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> ExtractionResult:
    if len(data) > limits.input_bytes:
        return ExtractionResult("limit", "", 0, 0, reason="input_bytes")
    mime = (mime_type or "").casefold()
    suffix = Path(filename).suffix.casefold()
    if mime == "application/json" or suffix == ".json":
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return ExtractionResult("failed", "", 0, 0, reason="malformed_json")
        return _bounded_text(json.dumps(parsed, ensure_ascii=True, sort_keys=True), limits)
    if mime in {"text/plain", "text/markdown", "text/csv"} or suffix in {
        ".txt",
        ".md",
        ".markdown",
        ".csv",
    }:
        try:
            return _bounded_text(data.decode("utf-8"), limits)
        except UnicodeDecodeError:
            return ExtractionResult("failed", "", 0, 0, reason="invalid_utf8")
    if mime in {"application/xml", "text/xml"} or suffix == ".xml":
        return _xml_text(data, limits)
    if mime in {"text/html", "application/xhtml+xml"} or suffix in {".html", ".htm"}:
        parser = _HTMLText()
        try:
            parser.feed(data.decode("utf-8"))
        except UnicodeDecodeError:
            return ExtractionResult("failed", "", 0, 0, reason="invalid_utf8")
        return _bounded_text(" ".join(parser.parts), limits)
    if suffix in {".docx", ".xlsx", ".pptx"} or "openxmlformats" in mime:
        return _ooxml_text(data, limits)
    if mime == "application/pdf" or suffix == ".pdf":
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found,unused-ignore]

            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                return ExtractionResult("unsupported", "", 0, 0, reason="encrypted_pdf")
            if len(reader.pages) > limits.pages:
                return ExtractionResult("limit", "", 0, 0, reason="pdf_pages")
            text = "\n".join(
                reader.pages[index].extract_text() or "" for index in range(len(reader.pages))
            )
            result = _bounded_text(text, limits)
            return ExtractionResult(
                result.status, result.text, len(reader.pages), result.chars, reason=result.reason
            )
        except Exception:
            return ExtractionResult("failed", "", 0, 0, reason="malformed_pdf")
    return ExtractionResult("unsupported", "", 0, 0, reason="unsupported_type")


def sandbox_available() -> bool:
    return platform.system() == "Darwin" and shutil.which("sandbox-exec") is not None


def run_worker(
    source: BinaryIO,
    *,
    mime_type: str | None,
    filename: str,
    limits: ExtractionLimits = DEFAULT_LIMITS,
    worker_python: str | None = None,
) -> ExtractionResult:
    """Decrypt into a pipe-connected worker; fail closed when sandbox capability is absent."""
    if not sandbox_available():
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="sandbox")
    interpreter = worker_python or sys.executable
    control_read, control_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        environment = {"PATH": os.environ.get("PATH", "")}
        process = subprocess.Popen(
            [interpreter, "-m", "worktrace.vault.extract_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(control_read,),
            env=environment,
            cwd="/",
        )
        os.write(
            control_write,
            json.dumps(
                {
                    "schema_version": 1,
                    "mime_type": mime_type,
                    "filename": filename,
                    "limits": asdict(limits),
                },
                separators=(",", ":"),
            ).encode()
            + b"\n",
        )
        os.close(control_write)
        if process.stdin is None or process.stdout is None:
            raise OSError("extraction worker pipes were not created")
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            process.stdin.write(chunk)
        process.stdin.close()
        output = process.stdout.read()
        process.wait(timeout=limits.timeout_seconds)
        if process.returncode != 0:
            return ExtractionResult(
                "failed", "", 0, 0, exit_code=process.returncode, reason="worker"
            )
        result = json.loads(output.decode("utf-8"))
        return ExtractionResult(**result)
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.wait()
        return ExtractionResult("failed", "", 0, 0, exit_code=14, reason="timeout")
    except (OSError, ValueError, json.JSONDecodeError):
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="worker")
    finally:
        os.close(control_read)
        with suppress(OSError):
            os.close(control_write)


def run_vault_worker(
    source: BinaryIO,
    key: bytes,
    *,
    expected_ciphertext_sha256: str | None,
    mime_type: str | None,
    filename: str,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> ExtractionResult:
    """Verify one WTVA object, then feed verified plaintext directly to the worker pipe."""
    if not sandbox_available():
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="sandbox")
    from worktrace.vault.format import decrypt_vault_object

    interpreter = sys.executable
    control_read, control_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [interpreter, "-m", "worktrace.vault.extract_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(control_read,),
            env={"PATH": os.environ.get("PATH", "")},
            cwd="/",
        )
        os.write(
            control_write,
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "mime_type": mime_type,
                        "filename": filename,
                        "limits": asdict(limits),
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
        )
        os.close(control_write)
        if process.stdin is None or process.stdout is None:
            raise OSError("extraction worker pipes were not created")
        with suppress(Exception):
            decrypt_vault_object(
                source,
                cast(BinaryIO, process.stdin),
                key,
                expected_ciphertext_sha256=expected_ciphertext_sha256,
            )
        process.stdin.close()
        output = process.stdout.read()
        process.wait(timeout=limits.timeout_seconds)
        if process.returncode != 0:
            return ExtractionResult(
                "failed", "", 0, 0, exit_code=process.returncode, reason="worker"
            )
        return ExtractionResult(**json.loads(output.decode("utf-8")))
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.wait()
        return ExtractionResult("failed", "", 0, 0, exit_code=14, reason="timeout")
    except (OSError, ValueError, json.JSONDecodeError):
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="worker")
    finally:
        os.close(control_read)
        with suppress(OSError):
            os.close(control_write)
