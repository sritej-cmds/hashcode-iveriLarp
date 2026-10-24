"""Selection contract (Phase 1).

A selector receives the candidate pool and a ``SelectionContext`` and returns a
``SelectionResult``: every proposal gets a decision, and ``ranked_ids`` lists
only the VALID ones, in order.

- INVALID (``REJECTED``) means a deterministic rule failed (unknown ID, target,
  auth, budget, ...). It is never executed.
- VALID but low priority is simply later in ``ranked_ids``.
- ``priority`` is whatever the proposer supplied. It is NOT a calibrated
  probability of finding a bug and is never used for eligibility.

Candidates are referenced by ``candidate_id`` from the existing pool of
``CandidateSequence`` objects; no request data is accepted from a proposer.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StringConstraints,
    model_validator,
)

from adaptive_restler.models import (
    ExecutionResult,
    Finding,
    Id,
    NonNegFloat,
    Score,
    TargetConfig,
)

__all__ = [
    "CandidateDecision",
    "CandidateStatus",
    "MAX_DETAIL_CHARS",
    "MAX_RATIONALE_CHARS",
    "RejectionCode",
    "SelectionContext",
    "SelectionProposal",
    "SelectionResult",
]

MAX_RATIONALE_CHARS = 500
MAX_DETAIL_CHARS = 300

_Rationale = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_RATIONALE_CHARS)
]
_Detail = Annotated[str, StringConstraints(max_length=MAX_DETAIL_CHARS)]


class _SelectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CandidateStatus(StrEnum):
    VALID = "valid"
    REJECTED = "rejected"


class RejectionCode(StrEnum):
    DUPLICATE_PROPOSAL = "duplicate_proposal"
    ALREADY_EXECUTED = "already_executed"
    UNKNOWN_CANDIDATE = "unknown_candidate"
    UNAUTHORIZED_AUTH_PROFILE = "unauthorized_auth_profile"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    INVALID_TARGET_CONFIG = "invalid_target_config"
    EXCEEDS_REMAINING_BUDGET = "exceeds_remaining_budget"


class SelectionProposal(_SelectionModel):
    """One candidate a selector (or model) wants considered, by ID only."""

    candidate_id: Id
    priority: Score | None = None
    rationale: _Rationale | None = None


class CandidateDecision(_SelectionModel):
    """Deterministic verdict on one proposal."""

    candidate_id: Id
    status: CandidateStatus
    rejection_code: RejectionCode | None = None
    rejection_detail: _Detail | None = None
    priority: Score | None = None
    rationale: _Rationale | None = None

    @model_validator(mode="after")
    def _status_matches_rejection(self) -> "CandidateDecision":
        if self.status is CandidateStatus.REJECTED and self.rejection_code is None:
            raise ValueError("a rejected decision requires rejection_code")
        if self.status is CandidateStatus.VALID and (
            self.rejection_code is not None or self.rejection_detail is not None
        ):
            raise ValueError("a valid decision must not carry a rejection")
        return self


class SelectionResult(_SelectionModel):
    """Ranked, validated output of one selection round plus metadata."""

    selector_name: Id
    ranked_ids: list[Id]
    decisions: list[CandidateDecision]
    fallback_used: StrictBool = False
    fallback_reason: _Detail | None = None
    latency_seconds: NonNegFloat = 0.0

    @model_validator(mode="after")
    def _consistent(self) -> "SelectionResult":
        valid_ids = [d.candidate_id for d in self.decisions if d.status is CandidateStatus.VALID]
        if len(set(valid_ids)) != len(valid_ids):
            raise ValueError("a candidate_id cannot be valid more than once")
        if self.ranked_ids != valid_ids:
            raise ValueError("ranked_ids must equal the valid decisions, in order")
        if self.fallback_used and not self.fallback_reason:
            raise ValueError("fallback_used requires fallback_reason")
        if not self.fallback_used and self.fallback_reason is not None:
            raise ValueError("fallback_reason requires fallback_used")
        return self

    @property
    def rejected(self) -> list[CandidateDecision]:
        return [d for d in self.decisions if d.status is CandidateStatus.REJECTED]


@dataclass(frozen=True)
class SelectionContext:
    """Trusted, runner-side facts used for validation. Never built from model output."""

    target: TargetConfig
    remaining_requests: int
    results: Sequence[ExecutionResult] = ()
    findings: Sequence[Finding] = ()

    def __post_init__(self) -> None:
        if self.remaining_requests < 0:
            raise ValueError("remaining_requests must be >= 0")

    @property
    def executed_candidate_ids(self) -> frozenset[str]:
        return frozenset(r.candidate_id for r in self.results)
