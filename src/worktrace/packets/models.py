from __future__ import annotations

from dataclasses import dataclass, field

from worktrace.domain.enums import ClaimStatus, ObservationType


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    observation_id: str
    object_id: str
    app_id: str
    source: str
    source_instance: str
    kind: str
    external_id: str
    title: str | None
    body_text: str | None
    data: dict[str, object]
    completeness: str
    fetched_at: str
    source_updated_at: str | None
    availability: str
    availability_evidence_id: str | None
    availability_reason: str | None
    availability_observed_at: str | None
    is_current: bool
    context_only: bool = False

    @property
    def evidence_id(self) -> str:
        return self.observation_id


@dataclass(frozen=True, slots=True)
class HumanAttestation:
    decision_id: str
    claim: str
    statement: str
    source_note: str | None = None


@dataclass(slots=True)
class ContributionView:
    id: str
    app_id: str
    title: str
    contribution_type: str
    member_ids: set[str]
    context_ids: set[str] = field(default_factory=set)
    decision_evidence_ids: set[str] = field(default_factory=set)
    title_evidence_ids: set[str] = field(default_factory=set)
    title_source_object_id: str | None = None
    attestations: list[HumanAttestation] = field(default_factory=list)
    candidate_id: str | None = None


@dataclass(frozen=True, slots=True)
class QuestionAnswer:
    question_id: str
    question: str
    answer_draft: str | None
    status: ClaimStatus
    observation_types: tuple[ObservationType, ...] = ()
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    missing_information: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.answer_draft and not self.supporting_evidence_ids:
            raise ValueError(
                f"material answer {self.question_id} must cite supporting evidence IDs"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "question_id": self.question_id,
            "question": self.question,
            "answer_draft": self.answer_draft,
            "source_text_is_untrusted": self.answer_draft is not None,
            "answer_draft_content_type": (
                "untrusted_source_summary" if self.answer_draft is not None else None
            ),
            "status": self.status.value,
            "observation_types": [value.value for value in self.observation_types],
            "supporting_evidence_ids": list(self.supporting_evidence_ids),
            "contradicting_evidence_ids": list(self.contradicting_evidence_ids),
            "limitations": list(self.limitations),
            "missing_information": list(self.missing_information),
        }
