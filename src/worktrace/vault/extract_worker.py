"""Pipe-only extraction worker; never accepts filesystem paths or credentials."""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict

from worktrace.vault.extract import ExtractionLimits, extract_bytes


def main() -> int:
    with os.fdopen(3, "rb", closefd=True) as control:
        header = json.loads(control.readline().decode("utf-8"))
    limits = ExtractionLimits(**header.get("limits", {}))
    data = sys.stdin.buffer.read(limits.input_bytes + 1)
    result = extract_bytes(
        data,
        mime_type=header.get("mime_type"),
        filename=str(header.get("filename", "")),
        limits=limits,
    )
    sys.stdout.write(json.dumps(asdict(result), separators=(",", ":")))
    return 0 if result.status in {"complete", "unsupported", "limit"} else result.exit_code or 12


if __name__ == "__main__":
    raise SystemExit(main())
