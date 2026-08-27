from __future__ import annotations

from enum import StrEnum


class Completeness(StrEnum):
    COMPLETE = "complete_for_scope"
    PARTIAL = "partial"
    SELECTION_BIASED = "selection_biased"
    SOURCE_UNAVAILABLE = "source_unavailable"
    UNKNOWN = "unknown"


class ClaimStatus(StrEnum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    HUMAN_ATTESTED = "human_attested"
    CONTRADICTED = "contradicted"
    UNRESOLVED = "unresolved"
    UNKNOWN = "unknown"


class ObservationType(StrEnum):
    SOURCE_ASSERTED = "source_asserted"
    REPOSITORY_OBSERVED = "repository_observed"
    DERIVED = "derived"
    HUMAN_ATTESTED = "human_attested"
    UNKNOWN = "unknown"


class Authority(StrEnum):
    AUTHORITATIVE = "authoritative"
    SUPPORTING = "supporting"
    CONTEXTUAL = "contextual"
    INAPPROPRIATE = "inappropriate"
