from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from fnmatch import fnmatchcase

from worktrace.domain.enums import ClaimStatus
from worktrace.packets.authority import find_attestation, is_merge_request_kind
from worktrace.packets.models import EvidenceRecord, HumanAttestation
from worktrace.participation import ParticipationCategory


def _nonempty(data: dict[str, object], *keys: str) -> bool:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}, False):
            return True
    return False


def _attested_rung(
    attestations: Sequence[HumanAttestation], claims: set[str]
) -> dict[str, object] | None:
    attestation = find_attestation(attestations, claims)
    if attestation is None:
        return None
    return {
        "status": ClaimStatus.HUMAN_ATTESTED.value,
        "statement": attestation.statement,
        "source_text_is_untrusted": True,
        "supporting_evidence_ids": [attestation.decision_id],
        "limitations": ["This release state is a local-user attestation."],
    }


def _observed_rung(
    records: Iterable[EvidenceRecord], statement: str, *, limitation: str | None = None
) -> dict[str, object]:
    material = tuple(records)
    if not material:
        return {
            "status": ClaimStatus.UNKNOWN.value,
            "statement": None,
            "source_text_is_untrusted": False,
            "supporting_evidence_ids": [],
            "limitations": [limitation] if limitation else [],
        }
    limitations = [limitation] if limitation else []
    return {
        "status": ClaimStatus.SUPPORTED.value,
        "statement": statement,
        "source_text_is_untrusted": False,
        "supporting_evidence_ids": sorted({record.evidence_id for record in material}),
        "limitations": limitations,
    }


def build_release_ladder(
    records: Sequence[EvidenceRecord],
    participation: dict[str, object],
    attestations: Sequence[HumanAttestation],
    production_environments: Sequence[str],
    release_tag_patterns: Sequence[str],
) -> dict[str, object]:
    raw_self = participation.get("self_participations", [])
    self_participations = raw_self if isinstance(raw_self, list) else []
    implementation_object_ids: set[str] = set()
    implementation_evidence_ids: set[str] = set()
    for item in self_participations:
        if not isinstance(item, dict):
            continue
        categories = item.get("categories")
        if isinstance(categories, list) and ParticipationCategory.IMPLEMENTED.value in categories:
            implementation_object_ids.add(str(item["object_id"]))
            supporting = item.get("claim_supporting_evidence_ids", [])
            if isinstance(supporting, list):
                implementation_evidence_ids.update(str(value) for value in supporting)
    implementation = [
        record
        for record in records
        if (
            record.object_id in implementation_object_ids
            or record.evidence_id in implementation_evidence_ids
        )
        and (
            "commit" in record.kind.casefold()
            or is_merge_request_kind(record.kind)
            or "changed_path" in record.kind.casefold()
        )
        and not record.context_only
    ]
    merged = [
        record
        for record in records
        if not record.context_only
        and record.source.casefold() == "gitlab"
        and is_merge_request_kind(record.kind)
        and (
            str(record.data.get("state", "")).casefold() == "merged"
            or _nonempty(record.data, "merged_at", "merge_commit_sha", "squash_commit_sha")
        )
    ]
    release_associated = [
        record
        for record in records
        if not record.context_only
        and (
            _nonempty(record.data, "fixVersions", "fix_versions")
            or "release" in record.kind.casefold()
            or _matches_release_tag(record.data, release_tag_patterns)
        )
    ]
    production_names = {value.casefold() for value in production_environments}
    deployed = [
        record
        for record in records
        if not record.context_only
        and record.source.casefold() == "gitlab"
        and (
            (
                "deployment" in record.kind.casefold()
                and str(record.data.get("status", "")).casefold() == "success"
                and _environment_name(record.data).casefold() in production_names
            )
            or _nonempty(record.data, "first_deployed_to_production_at")
        )
    ]
    manual_metric = [
        record
        for record in records
        if not record.context_only
        and record.source.casefold() == "manual"
        and str(record.data.get("kind", record.kind)).casefold()
        in {"metric", "production_metric", "measured_outcome"}
    ]

    released_to_users = _attested_rung(
        attestations,
        {"released_to_users", "app_store_release", "mobile_user_release"},
    ) or _observed_rung(
        (),
        "",
        limitation="Deployment or merge evidence does not prove availability to mobile users.",
    )
    currently_enabled = _attested_rung(
        attestations, {"currently_enabled", "currently_used", "current_use"}
    ) or _observed_rung(
        (), "", limitation="Current enablement requires current evidence or attestation."
    )
    measurable = _attested_rung(
        attestations, {"measurably_successful", "measured_outcome", "impact"}
    ) or _observed_rung(
        manual_metric,
        "Manual evidence records a measured outcome.",
        limitation="The metric is user-supplied unless a telemetry source is added later.",
    )

    return {
        "implemented": _attested_rung(
            attestations,
            {
                "implemented",
                "implementation",
                "implementation_authorship",
                "personal_implementation",
            },
        )
        or _observed_rung(
            implementation,
            "Repository evidence records implementation authorship participation.",
            limitation="Implementation participation does not establish feature ownership.",
        ),
        "merged": _observed_rung(
            merged,
            "GitLab recorded a merged merge request.",
            limitation="Merged does not mean deployed or released to users.",
        ),
        "release_associated": _observed_rung(
            release_associated,
            "Evidence records an association with a tag, fix version, or release record.",
            limitation="Release association does not prove deployment.",
        ),
        "deployed": _observed_rung(
            deployed,
            "GitLab recorded a successful deployment to a configured production environment.",
            limitation="Deployment does not prove App Store availability or feature enablement.",
        ),
        "released_to_users": released_to_users,
        "currently_enabled": currently_enabled,
        "measurably_successful": measurable,
    }


def _environment_name(data: dict[str, object]) -> str:
    scalar = data.get("environment_name")
    if isinstance(scalar, str):
        return scalar
    environment = data.get("environment")
    if isinstance(environment, dict) and isinstance(environment.get("name"), str):
        return str(environment["name"])
    return environment if isinstance(environment, str) else ""


def _matches_release_tag(data: dict[str, object], patterns: Sequence[str]) -> bool:
    values: list[str] = []
    for key in ("release_tag", "tag", "tag_name"):
        value = data.get(key)
        if isinstance(value, str) and value:
            values.append(value)
    if str(data.get("ref_kind", "")).casefold() == "tag":
        ref_name = data.get("ref_name")
        if isinstance(ref_name, str):
            values.append(ref_name.removeprefix("refs/tags/"))
    return any(
        fnmatchcase(value, pattern) or re.fullmatch(pattern, value) is not None
        for value in values
        for pattern in patterns
    )
