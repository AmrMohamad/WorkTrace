"""Bounded, credential-free extraction for Jira vault originals."""

from __future__ import annotations

import hashlib
import html.parser
import io
import json
import os
import platform
import resource
import shutil
import subprocess
import sys
import tempfile
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


def _set_worker_limits() -> None:
    maximum = 512 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (maximum, maximum))


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
                if entry.filename.lower().endswith(".xml") and not entry.filename.lower().endswith(
                    ("/externallinks.xml", "customxml/item1.xml")
                ):
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
    return platform.system() == "Darwin" and Path("/usr/bin/sandbox-exec").is_file()


def _sandbox_profile() -> tuple[str, str]:
    interpreter = Path(sys.executable).resolve()
    module_root = Path(__file__).resolve().parents[2]
    read_roots = {interpreter.parent, module_root, Path(sys.prefix).resolve()}
    read_rules = "".join(f'(allow file-read* (subpath "{root}"))' for root in sorted(read_roots))
    profile = (
        "(version 1)(deny default)(deny network*)(deny process-fork)(deny process-exec)"
        f'{read_rules}(allow file-read* (literal "/dev/null"))'
    )
    return profile, hashlib.sha256(profile.encode()).hexdigest()


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
    workdir: str | None = None
    try:
        profile, profile_hash = _sandbox_profile()
        environment = {
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONUTF8": "1",
            "WORKTRACE_CONTROL_FD": str(control_read),
        }
        command = [
            "/usr/bin/sandbox-exec",
            "-p",
            profile,
            interpreter,
            "-m",
            "worktrace.vault.extract_worker",
        ]
        workdir = tempfile.mkdtemp(prefix="worktrace-extract-")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(control_read,),
            env=environment,
            cwd=workdir,
            preexec_fn=_set_worker_limits,
        )
        os.write(
            control_write,
            json.dumps(
                {
                    "schema_version": 1,
                    "mime_type": mime_type,
                    "attachment_id": "worker-input",
                    "declared_length": None,
                    "limits": asdict(limits),
                    "profile_sha256": profile_hash,
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
        if process.returncode not in {0, 10, 11}:
            return ExtractionResult(
                "failed", "", 0, 0, exit_code=process.returncode, reason="worker"
            )
        result = json.loads(output.decode("utf-8"))
        parsed = ExtractionResult(**result)
        expected_exit = {"complete": 0, "unsupported": 10, "limit": 11}.get(parsed.status)
        if expected_exit is None or process.returncode != expected_exit:
            return ExtractionResult("failed", "", 0, 0, exit_code=12, reason="protocol")
        return parsed
    except subprocess.TimeoutExpired:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return ExtractionResult("failed", "", 0, 0, exit_code=14, reason="timeout")
    except (OSError, ValueError, json.JSONDecodeError):
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="worker")
    finally:
        os.close(control_read)
        with suppress(OSError):
            os.close(control_write)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)


def run_vault_worker(
    source: BinaryIO,
    key: bytes,
    *,
    expected_ciphertext_sha256: str | None,
    attachment_id: str,
    declared_length: int | None,
    mime_type: str | None,
    limits: ExtractionLimits = DEFAULT_LIMITS,
) -> ExtractionResult:
    """Verify one WTVA object, then feed verified plaintext directly to the worker pipe."""
    if not sandbox_available():
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="sandbox")
    from worktrace.vault.format import decrypt_vault_object, verify_vault_object

    interpreter = sys.executable
    control_read, control_write = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    workdir: str | None = None
    try:
        profile, profile_hash = _sandbox_profile()
        workdir = tempfile.mkdtemp(prefix="worktrace-extract-")
        process = subprocess.Popen(
            [
                "/usr/bin/sandbox-exec",
                "-p",
                profile,
                interpreter,
                "-m",
                "worktrace.vault.extract_worker",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            close_fds=True,
            pass_fds=(control_read,),
            env={
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "PYTHONUTF8": "1",
                "WORKTRACE_CONTROL_FD": str(control_read),
            },
            cwd=workdir,
            preexec_fn=_set_worker_limits,
        )
        os.write(
            control_write,
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "mime_type": mime_type,
                        "attachment_id": attachment_id,
                        "declared_length": declared_length,
                        "limits": asdict(limits),
                        "profile_sha256": profile_hash,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            ).encode(),
        )
        os.close(control_write)
        if process.stdin is None or process.stdout is None:
            raise OSError("extraction worker pipes were not created")
        try:
            verify_vault_object(
                source,
                key,
                expected_ciphertext_sha256=expected_ciphertext_sha256,
            )
            source.seek(0)
            decrypt_vault_object(
                source,
                cast(BinaryIO, process.stdin),
                key,
                expected_ciphertext_sha256=expected_ciphertext_sha256,
                stage_plaintext=False,
            )
        except Exception:
            process.stdin.close()
            process.terminate()
            process.wait(timeout=1)
            return ExtractionResult("failed", "", 0, 0, exit_code=12, reason="vault_verification")
        process.stdin.close()
        output = process.stdout.read()
        process.wait(timeout=limits.timeout_seconds)
        if process.returncode not in {0, 10, 11}:
            return ExtractionResult(
                "failed", "", 0, 0, exit_code=process.returncode, reason="worker"
            )
        parsed = ExtractionResult(**json.loads(output.decode("utf-8")))
        expected_exit = {"complete": 0, "unsupported": 10, "limit": 11}.get(parsed.status)
        if expected_exit is None or process.returncode != expected_exit:
            return ExtractionResult("failed", "", 0, 0, exit_code=12, reason="protocol")
        return parsed
    except subprocess.TimeoutExpired:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        return ExtractionResult("failed", "", 0, 0, exit_code=14, reason="timeout")
    except (OSError, ValueError, json.JSONDecodeError):
        return ExtractionResult("extraction_unavailable", "", 0, 0, exit_code=13, reason="worker")
    finally:
        os.close(control_read)
        with suppress(OSError):
            os.close(control_write)
        if workdir is not None:
            shutil.rmtree(workdir, ignore_errors=True)
