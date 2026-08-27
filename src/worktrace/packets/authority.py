from __future__ import annotations

from collections.abc import Iterable, Sequence

from worktrace.domain.enums import Authority, ClaimStatus, ObservationType
from worktrace.packets.models import EvidenceRecord, HumanAttestation, QuestionAnswer


def is_merge_request_kind(kind: str) -> bool:
    normalized = kind.casefold()
    return "merge_request" in normalized or "mr" in normalized.split("_")


def claim_authority(record: EvidenceRecord, claim: str) -> Authority:
    """Return authority for a narrow claim, never for general ownership or impact."""

    source = record.source.casefold()
    kind = record.kind.casefold()
    if claim == "git_authorship" and source == "git" and "commit" in kind:
        return Authority.AUTHORITATIVE
    if claim == "merged" and source == "gitlab" and is_merge_request_kind(kind):
        return Authority.AUTHORITATIVE
    if claim == "jira_record" and source == "jira":
        return Authority.AUTHORITATIVE
    if claim == "deployment" and source == "gitlab" and "deployment" in kind:
        return Authority.AUTHORITATIVE
    if claim in {"ownership", "released_to_users", "measured_success"}:
        return Authority.INAPPROPRIATE
    return Authority.SUPPORTING


def evidence_ids(records: Iterable[EvidenceRecord]) -> tuple[str, ...]:
    return tuple(sorted({record.evidence_id for record in records}))


def unknown_answer(
    question_id: str,
    question: str,
    missing: str,
    *,
    contradictions: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> QuestionAnswer:
    return QuestionAnswer(
        question_id=question_id,
        question=question,
        answer_draft=None,
        status=ClaimStatus.UNRESOLVED if contradictions else ClaimStatus.UNKNOWN,
        contradicting_evidence_ids=tuple(sorted(set(contradictions))),
        limitations=tuple(limitations),
        missing_information=(missing,),
    )


def supported_answer(
    question_id: str,
    question: str,
    statement: str,
    records: Sequence[EvidenceRecord],
    *,
    status: ClaimStatus = ClaimStatus.SUPPORTED,
    observation_types: Sequence[ObservationType] = (ObservationType.SOURCE_ASSERTED,),
    contradictions: Sequence[str] = (),
    limitations: Sequence[str] = (),
) -> QuestionAnswer:
    return QuestionAnswer(
        question_id=question_id,
        question=question,
        answer_draft=statement,
        status=ClaimStatus.CONTRADICTED if contradictions else status,
        observation_types=tuple(observation_types),
        supporting_evidence_ids=evidence_ids(records),
        contradicting_evidence_ids=tuple(sorted(set(contradictions))),
        limitations=tuple(limitations),
    )


def attested_answer(
    question_id: str,
    question: str,
    attestation: HumanAttestation,
    *,
    contradictions: Sequence[str] = (),
) -> QuestionAnswer:
    limitations = ["This statement is a local-user attestation, not provider-observed fact."]
    if attestation.source_note:
        limitations.append(
            "A source note exists but its underlying external record was not imported."
        )
    return QuestionAnswer(
        question_id=question_id,
        question=question,
        answer_draft=attestation.statement,
        status=ClaimStatus.CONTRADICTED if contradictions else ClaimStatus.HUMAN_ATTESTED,
        observation_types=(ObservationType.HUMAN_ATTESTED,),
        supporting_evidence_ids=(attestation.decision_id,),
        contradicting_evidence_ids=tuple(sorted(set(contradictions))),
        limitations=tuple(limitations),
    )


def find_attestation(
    attestations: Iterable[HumanAttestation], claims: set[str]
) -> HumanAttestation | None:
    normalized = {claim.casefold() for claim in claims}
    for attestation in reversed(tuple(attestations)):
        if attestation.claim.casefold() in normalized:
            return attestation
    return None
