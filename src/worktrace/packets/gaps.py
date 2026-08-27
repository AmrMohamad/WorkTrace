from __future__ import annotations

import sqlite3

from worktrace.config import WorkTraceConfig


def build_gap_report(packet: dict[str, object]) -> dict[str, object]:
    unknown_questions: list[dict[str, object]] = []
    sections = packet.get("sections", {})
    if isinstance(sections, dict):
        for section_name, raw_questions in sections.items():
            if not isinstance(raw_questions, list):
                continue
            for raw in raw_questions:
                if not isinstance(raw, dict):
                    continue
                if raw.get("status") not in {"unknown", "unresolved", "contradicted"}:
                    continue
                unknown_questions.append(
                    {
                        "section": str(section_name),
                        "question_id": raw.get("question_id"),
                        "question": raw.get("question"),
                        "status": raw.get("status"),
                        "missing_information": raw.get("missing_information", []),
                        "contradicting_evidence_ids": raw.get("contradicting_evidence_ids", []),
                    }
                )

    source_status = packet.get("source_status", {})
    unavailable_sources: list[str] = []
    if isinstance(source_status, dict):
        for source, status in source_status.items():
            if not isinstance(status, dict):
                continue
            if status.get("complete") is not True or status.get("stale") is True:
                unavailable_sources.append(str(source))

    suggestions: list[str] = []
    if "jira" in unavailable_sources:
        suggestions.append("Refresh authorized Jira evidence for business context and history.")
    if "gitlab" in unavailable_sources:
        suggestions.append(
            "Refresh authorized GitLab evidence for review, merge, and deployment state."
        )
    if "git" in unavailable_sources:
        suggestions.append(
            "Refresh the configured local clone manually, then run a new Git snapshot."
        )
    if any(item.get("question_id") == "identity.ownership" for item in unknown_questions):
        suggestions.append(
            "Add a narrowly worded ownership attestation after reviewing contributors."
        )
    if any(str(item.get("question_id", "")).startswith("result.") for item in unknown_questions):
        suggestions.append("Add manual result evidence only when its source can be named.")

    contribution = packet.get("contribution")
    contribution_id = contribution.get("id") if isinstance(contribution, dict) else None
    return {
        "contribution_id": contribution_id,
        "as_of": packet.get("as_of"),
        "unknown_questions": unknown_questions,
        "contradictions": packet.get("contradictions", []),
        "incomplete_or_stale_sources": sorted(unavailable_sources),
        "suggested_follow_up": suggestions,
        "limitations": [
            "Suggestions identify evidence locations; WorkTrace does not follow URLs or "
            "import sources through MCP."
        ],
    }


def list_evidence_gaps(
    connection: sqlite3.Connection,
    contribution_id: str,
    config: WorkTraceConfig,
) -> dict[str, object]:
    """Build packet gaps through the shared read model without opening another database."""

    from worktrace.packets.builder import PacketBuilder

    return PacketBuilder(connection, config).evidence_gaps(contribution_id)
