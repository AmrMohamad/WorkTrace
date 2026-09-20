"""Pipe-only extraction worker; never accepts filesystem paths or credentials."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from worktrace.vault.extract import ExtractionLimits, ExtractionResult, extract_bytes


def main() -> int:
    control_fd = int(os.environ.get("WORKTRACE_CONTROL_FD", "-1"))
    if control_fd < 0:
        raise SystemExit(13)
    with os.fdopen(control_fd, "rb", closefd=True) as control:
        header = json.loads(control.readline().decode("utf-8"))
    if (
        set(header)
        != {
            "schema_version",
            "mime_type",
            "attachment_id",
            "declared_length",
            "limits",
            "profile_sha256",
        }
        or header.get("schema_version") != 1
        or not isinstance(header.get("attachment_id"), str)
        or not isinstance(header.get("profile_sha256"), str)
    ):
        raise SystemExit(13)
    if header.get("declared_length") is not None and not isinstance(header["declared_length"], int):
        raise SystemExit(3)
    limits = ExtractionLimits(**header.get("limits", {}))
    data = sys.stdin.buffer.read(limits.input_bytes + 1)
    declared_length = header.get("declared_length")
    if isinstance(declared_length, int) and len(data) != declared_length:
        result = ExtractionResult("failed", "", 0, 0, exit_code=12, reason="declared_length")
        sys.stdout.write(json.dumps(asdict(result), separators=(",", ":")))
        return 12
    result = extract_bytes(
        data,
        mime_type=header.get("mime_type"),
        filename="",
        limits=limits,
    )
    sys.stdout.write(json.dumps(asdict(result), separators=(",", ":")))
    return {
        "complete": 0,
        "unsupported": 10,
        "limit": 11,
        "failed": 12,
    }.get(result.status, 12)


if __name__ == "__main__":
    raise SystemExit(main())
