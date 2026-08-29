from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
FIXTURES = ROOT / "tests" / "fixtures"

MCP_TOOLS = {
    "list_contribution_candidates",
    "get_contribution_summary",
    "build_phase4_packet",
    "list_evidence_gaps",
    "search_evidence",
    "get_evidence_excerpt",
}


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_json_fixtures_parse_and_use_synthetic_source_hosts() -> None:
    paths = sorted(FIXTURES.rglob("*.json"))
    assert paths
    for path in paths:
        _json(path)

    combined = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    assert "robustastudio.com" not in combined.casefold()
    assert "gitlab.com/" not in combined.casefold()
    assert "atlassian.net" not in combined.casefold()
    assert "/Users/" not in combined
    assert "fixture.example" in combined


def test_golden_fixture_has_ten_unique_bounded_cases() -> None:
    payload = _json(FIXTURES / "golden" / "cases.json")
    cases = payload["cases"]

    assert len(cases) == 10
    assert len({case["id"] for case in cases}) == len(cases)
    for case in cases:
        expected = case["expected"]
        assert expected["candidate_found"] is True
        assert expected["expected_participation"]
        assert expected["forbidden_claims"]
        assert expected["expected_gaps"]
        assert all(key.startswith("DEMO-") for key in case["known_records"]["jira_keys"])


def test_docs_record_required_truth_and_phase4_invariants() -> None:
    required_docs = {
        "product-contract.md",
        "evidence-model.md",
        "threat-model.md",
        "source-authority.md",
        "phase4-schema.md",
        "known-limitations.md",
    }
    assert required_docs <= {path.name for path in DOCS.glob("*.md")}

    product = (DOCS / "product-contract.md").read_text(encoding="utf-8")
    evidence = (DOCS / "evidence-model.md").read_text(encoding="utf-8")
    threats = (DOCS / "threat-model.md").read_text(encoding="utf-8")
    packet = (DOCS / "phase4-schema.md").read_text(encoding="utf-8")
    limitations = (DOCS / "known-limitations.md").read_text(encoding="utf-8")

    assert "private, local, single-user" in product
    assert "relationship is not ownership" in product
    assert "Numeric confidence" in evidence
    assert "append-only" in evidence
    assert "untrusted_source_excerpt" in threats
    assert "supporting_evidence_ids" in packet
    assert "contradicting_evidence_ids" in packet
    assert "well_supported" in packet
    assert "measurably_successful" in packet
    assert "Live provider or proprietary repository validation" in limitations


def test_public_contracts_use_the_emitted_v01_vocabulary() -> None:
    evidence = (DOCS / "evidence-model.md").read_text(encoding="utf-8")
    packet = (DOCS / "phase4-schema.md").read_text(encoding="utf-8")

    canonical_roles = {
        "git_tag_author",
        "jira_comment_author",
        "jira_changelog_author",
        "mr_author",
        "mr_reviewer",
    }
    obsolete_roles = {
        "jira_commenter",
        "jira_field_updater",
        "gitlab_mr_author",
        "gitlab_reviewer",
    }
    assert canonical_roles <= set(evidence.split())
    assert obsolete_roles.isdisjoint(evidence.split())

    emitted_release_rungs = {
        "implemented",
        "merged",
        "release_associated",
        "deployed",
        "released_to_users",
        "currently_enabled",
        "measurably_successful",
    }
    obsolete_release_rungs = {
        "implementation_observed",
        "deployment_observed",
        "currently_enabled_or_used",
    }
    assert emitted_release_rungs <= set(packet.split())
    assert obsolete_release_rungs.isdisjoint(packet.split())
    assert "`git_tag_author`" in evidence
    assert "`action.implemented`" in packet
    assert "`action.implementation`" not in packet


def test_agdr_records_package_runtime_and_authority_tradeoffs() -> None:
    package_runtime = (
        DOCS / "agdr" / "AgDR-0003-local-python-runtime-and-evidence-authority.md"
    ).read_text(encoding="utf-8")
    required_decisions = {
        "Python 3.12",
        "Hatchling",
        "Typer",
        "httpx",
        "MCP",
        "SQLite",
        "selection_biased",
        "selection_policy_version",
    }
    assert all(decision in package_runtime for decision in required_decisions)

    evidence_contract = (DOCS / "agdr" / "AgDR-0001-evidence-pipeline-contracts.md").read_text(
        encoding="utf-8"
    )
    assert "No new runtime dependency or service is introduced." not in evidence_contract


def test_codex_mcp_example_is_bounded_and_forwards_no_source_credentials() -> None:
    raw = (DOCS / "codex-mcp.example.toml").read_text(encoding="utf-8")
    config = tomllib.loads(raw)
    server = config["mcp_servers"]["worktrace"]

    assert set(server["enabled_tools"]) == MCP_TOOLS
    assert server["default_tools_approval_mode"] == "auto"
    assert server["tools"]["get_evidence_excerpt"]["approval_mode"] == "prompt"
    assert "WORKTRACE_JIRA" not in raw
    assert "WORKTRACE_GITLAB" not in raw
