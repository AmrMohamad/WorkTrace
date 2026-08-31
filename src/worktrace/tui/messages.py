from __future__ import annotations

from enum import StrEnum

from textual.message import Message

from worktrace.errors import DatabaseError, NotFound, ScopeViolation
from worktrace.read_models.candidates import CandidateGenerationChanged, CandidatePage
from worktrace.read_workspace import (
    ApplicationSummary,
    ContributionReview,
    DatabaseBusy,
    DatabaseUpgradeRequired,
    DatabaseVersionUnsupported,
)


class FailureKind(StrEnum):
    BUSY = "busy"
    GENERATION_CHANGED = "generation_changed"
    NOT_FOUND = "not_found"
    OUT_OF_SCOPE = "out_of_scope"
    UPGRADE_REQUIRED = "upgrade_required"
    UNSUPPORTED_NEWER = "unsupported_newer"
    DATABASE = "database"
    UNEXPECTED = "unexpected"


def failure_kind(error: Exception) -> FailureKind:
    if isinstance(error, DatabaseBusy):
        return FailureKind.BUSY
    if isinstance(error, CandidateGenerationChanged):
        return FailureKind.GENERATION_CHANGED
    if isinstance(error, NotFound):
        return FailureKind.NOT_FOUND
    if isinstance(error, ScopeViolation):
        return FailureKind.OUT_OF_SCOPE
    if isinstance(error, DatabaseUpgradeRequired):
        return FailureKind.UPGRADE_REQUIRED
    if isinstance(error, DatabaseVersionUnsupported):
        return FailureKind.UNSUPPORTED_NEWER
    if isinstance(error, DatabaseError):
        return FailureKind.DATABASE
    return FailureKind.UNEXPECTED


class ApplicationsLoaded(Message):
    def __init__(self, request_id: int, applications: tuple[ApplicationSummary, ...]) -> None:
        super().__init__()
        self.request_id = request_id
        self.applications = applications


class SourceStatusLoaded(Message):
    def __init__(self, request_id: int, status: dict[str, object]) -> None:
        super().__init__()
        self.request_id = request_id
        self.status = status


class CandidatePageLoaded(Message):
    def __init__(self, request_id: int, page: CandidatePage) -> None:
        super().__init__()
        self.request_id = request_id
        self.page = page


class ContributionReviewLoaded(Message):
    def __init__(self, request_id: int, review: ContributionReview) -> None:
        super().__init__()
        self.request_id = request_id
        self.review = review


class EvidenceExcerptLoaded(Message):
    def __init__(self, request_id: int, excerpt: dict[str, object]) -> None:
        super().__init__()
        self.request_id = request_id
        self.excerpt = excerpt


class ReadFailed(Message):
    def __init__(self, operation: str, request_id: int, kind: FailureKind) -> None:
        super().__init__()
        self.operation = operation
        self.request_id = request_id
        self.kind = kind
