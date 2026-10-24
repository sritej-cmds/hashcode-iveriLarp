"""Canonical shared data models (contract: docs/interfaces.md v1.1).

Every shared persisted type is defined exactly once, here. Other modules must
import from this file and must not define competing copies.

Conventions enforced by these models:
- Pydantic v2, unknown fields rejected, validation re-run on assignment.
- Numbers are strictly typed (no "0.5" strings, no bools as numbers) and
  finite where a float is used.
- Timestamps must be timezone-aware and are normalized to UTC.
- Evidence paths are relative and cannot escape the artifact directory.
- Sets and dicts that affect reproducibility serialize in sorted order.
- No model stores credentials. References such as ``auth_profile_id`` are
  identifier-shaped strings only.
"""

import math
import re
from datetime import timezone
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    StrictBool,
    StringConstraints,
    field_serializer,
    field_validator,
    model_validator,
)

__all__ = [
    "BenchmarkConfig",
    "BenchmarkReport",
    "BudgetState",
    "CandidateAssessment",
    "CandidateCost",
    "CandidateOutcomeSummary",
    "CandidateRequest",
    "CandidateSequence",
    "ExecutionBudget",
    "ExecutionResult",
    "ExecutionStatus",
    "ExperimentContext",
    "Finding",
    "FindingStatus",
    "PlannerContext",
    "PlannerHistory",
    "RankedCandidate",
    "ScoringConfig",
    "TargetConfig",
    "ValidationContext",
]

# --------------------------------------------------------------------------
# Reusable constrained types
# --------------------------------------------------------------------------

# Non-empty, no leading/trailing whitespace, no newlines. Used for stable IDs.
Id = Annotated[str, StringConstraints(min_length=1, pattern=r"^\S(?:.*\S)?$")]

# Identifier-shaped reference (e.g. an auth profile id). Deliberately rejects
# anything that looks like "user:password" or "Bearer abc". This is a shape
# check, not secret detection.
RefId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.\-]+$")]

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

Score = Annotated[float, Field(strict=True, ge=0.0, le=1.0, allow_inf_nan=False)]
NonNegFloat = Annotated[float, Field(strict=True, ge=0.0, allow_inf_nan=False)]
PosFloat = Annotated[float, Field(strict=True, gt=0.0, allow_inf_nan=False)]
PosInt = Annotated[int, Field(strict=True, gt=0)]
NonNegInt = Annotated[int, Field(strict=True, ge=0)]
HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]


def _to_utc(value):
    return value.astimezone(timezone.utc)


# Timezone-aware input required (naive datetimes are rejected); stored as UTC.
UtcDatetime = Annotated[AwareDatetime, AfterValidator(_to_utc)]


def _check_relative_path(value: str) -> str:
    if not value:
        raise ValueError("path must not be empty")
    if "\\" in value or "\x00" in value:
        raise ValueError("path must use forward slashes and contain no NUL")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("path must not start with a drive letter")
    posix = PurePosixPath(value)
    if posix.is_absolute():
        raise ValueError("path must be relative to the artifact directory")
    if ".." in posix.parts:
        raise ValueError("path must not contain '..'")
    return value


# Relative to the run-artifact directory; cannot be absolute or contain '..'.
RelativePath = Annotated[str, AfterValidator(_check_relative_path)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# 3.x Candidate and execution models
# --------------------------------------------------------------------------

# Header names containing any of these fragments are rejected: credentials are
# supplied via an auth profile, never inside a candidate.
_SENSITIVE_HEADER_FRAGMENTS = (
    "authorization",
    "cookie",
    "api-key",
    "apikey",
    "token",
    "secret",
    "password",
    "passwd",
)


class CandidateRequest(_Model):
    """One ordered API request inside a candidate sequence (contract 3.1)."""

    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any | None = None
    expected_statuses: list[HttpStatus] = Field(default_factory=list)

    @field_validator("method")
    @classmethod
    def _normalize_method(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]+", value):
            raise ValueError("method must be alphabetic, e.g. GET or POST")
        return value

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("path must start with a single '/'")
        if "://" in value:
            raise ValueError("path must not contain a scheme or hostname")
        if "\\" in value or any(c.isspace() or ord(c) < 32 for c in value):
            raise ValueError("path must not contain whitespace, control characters or backslashes")
        return value

    @field_validator("headers")
    @classmethod
    def _reject_credential_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name in value:
            lowered = name.lower()
            if any(fragment in lowered for fragment in _SENSITIVE_HEADER_FRAGMENTS):
                raise ValueError(
                    f"header {name!r} looks credential-bearing; "
                    "use auth_profile_id instead"
                )
        return value


class CandidateSequence(_Model):
    """An executable, ordered sequence of API requests (contract 3.2).

    ``sequence`` order is significant and preserved. ``tags`` keep the order
    given by the producer.
    """

    candidate_id: Id
    sequence: list[CandidateRequest] = Field(min_length=1)
    source: Literal["restler", "imported", "generated"]
    tags: list[str] = Field(default_factory=list)
    estimated_request_count: PosInt
    auth_profile_id: RefId | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CandidateAssessment(_Model):
    """An LLM or fallback assessment. Not a security finding (contract 3.3)."""

    candidate_id: Id
    security_hypotheses: list[NonEmptyText]
    rationale: NonEmptyText
    predicted_discovery_score: Score
    expected_impact_score: Score
    confidence: Score
    missing_information: list[str] = Field(default_factory=list)
    provider_id: Id
    prompt_version: Id


class CandidateCost(_Model):
    """Cost estimates for the deterministic ranker (contract 3.4).

    ``estimated_inference_cost=None`` means unknown, never zero.
    """

    candidate_id: Id
    estimated_request_count: PosInt
    estimated_execution_seconds: NonNegFloat
    estimated_planner_seconds: NonNegFloat
    estimated_inference_cost: NonNegFloat | None = None


class ExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SKIPPED = "skipped"


class ExecutionResult(_Model):
    """Outcome of one candidate execution attempt (contract 3.6). Runner-produced."""

    run_id: Id
    candidate_id: Id
    status: ExecutionStatus
    started_at: UtcDatetime
    duration_seconds: NonNegFloat
    request_count: NonNegInt
    responses: list[dict[str, Any]] = Field(default_factory=list)
    error_summary: str | None = None
    evidence_paths: list[RelativePath] = Field(default_factory=list)
    budget_exhausted: StrictBool


class FindingStatus(StrEnum):
    LEAD = "lead"
    NEEDS_REVIEW = "needs_review"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class Finding(_Model):
    """A security observation and its validation state (contract 3.8).

    ``fingerprint`` (contract v1.1) is set by the validator and identifies the
    underlying issue across candidates/runs. When ``None``, the planner falls
    back to ``category + candidate_id``.
    """

    finding_id: Id
    run_id: Id
    candidate_id: Id
    category: NonEmptyStr
    status: FindingStatus
    impact_score: Annotated[int, Field(strict=True, ge=0, le=28)] | None = None
    severity: Literal["informational", "low", "medium", "high", "critical"] | None = None
    evidence_paths: list[RelativePath] = Field(default_factory=list)
    validation_notes: str
    fingerprint: Id | None = None

    @model_validator(mode="after")
    def _confirmation_needs_evidence(self) -> "Finding":
        if self.status is FindingStatus.CONFIRMED and not self.evidence_paths:
            raise ValueError("a confirmed finding requires evidence_paths")
        if self.severity is not None and not self.evidence_paths:
            raise ValueError("severity requires supporting evidence_paths")
        return self


# --------------------------------------------------------------------------
# 4.x Run, budget and target models
# --------------------------------------------------------------------------


class ExecutionBudget(_Model):
    """Execution limits for one experiment (contract 4.1)."""

    max_requests: PosInt
    max_runtime_seconds: PosFloat
    request_timeout_seconds: PosFloat


class TargetConfig(_Model):
    """Authorized target (contract 4.2). Built outside the LLM.

    ``allowed_hosts`` are bare hostnames/IPs (no scheme, port or path), stored
    lowercase, de-duplicated and sorted. TLS verification and dry-run both
    default to the safe setting.
    """

    base_url: NonEmptyStr
    allowed_hosts: list[str] = Field(min_length=1)
    auth_profile_id: RefId | None = None
    tls_verification_enabled: StrictBool = True
    dry_run: StrictBool = True

    @field_validator("allowed_hosts")
    @classmethod
    def _normalize_hosts(cls, value: list[str]) -> list[str]:
        cleaned = set()
        for host in value:
            host = host.strip().lower()
            if not re.fullmatch(r"[a-z0-9.\-:]+", host):
                raise ValueError(f"invalid allowed host {host!r}: use bare hostnames only")
            cleaned.add(host)
        return sorted(cleaned)

    @model_validator(mode="after")
    def _base_url_must_be_allowlisted(self) -> "TargetConfig":
        try:
            parts = urlsplit(self.base_url)
            hostname = parts.hostname
            _ = parts.port  # raises ValueError on a malformed port
        except ValueError as exc:
            raise ValueError(f"base_url is not a valid URL: {exc}") from exc
        if parts.scheme not in ("http", "https"):
            raise ValueError("base_url scheme must be http or https")
        if not hostname:
            raise ValueError("base_url must include a hostname")
        if parts.username is not None or parts.password is not None:
            raise ValueError("base_url must not embed credentials")
        if parts.query or parts.fragment:
            raise ValueError("base_url must not include a query or fragment")
        if hostname not in self.allowed_hosts:
            raise ValueError(f"base_url host {hostname!r} is not in allowed_hosts")
        return self


class ExperimentContext(_Model):
    """Shared context for one run (contract 4.3). Holds no credentials."""

    run_id: Id
    target_config: TargetConfig
    execution_budget: ExecutionBudget
    artifact_dir: NonEmptyStr
    benchmark_id: Id
    configuration_version: Id


class RankedCandidate(_Model):
    """Planner ranking output for one candidate (contract 4.4).

    ``rank`` starts at 1. ``reasons`` must be non-empty and secret-free.
    """

    candidate_id: Id
    expected_value: NonNegFloat
    normalized_cost: NonNegFloat
    priority_score: NonNegFloat
    rank: PosInt
    reasons: list[NonEmptyText] = Field(min_length=1)


# --------------------------------------------------------------------------
# 11.x Supporting models
# --------------------------------------------------------------------------


class PlannerContext(_Model):
    """Limits for one assessment pass (contract 11.2). No target/budget control."""

    run_id: Id
    prompt_version: Id
    max_candidates_per_assessment: PosInt
    assessment_timeout_seconds: PosFloat


class ScoringConfig(_Model):
    """Ranking configuration (contract 11.3, extended in v1.1).

    The four weights must be non-negative and sum to 1 (tolerance 1e-6).
    The four ``*_budget`` fields are the denominators of the normalized cost
    formula. ``inference_cost_budget=None`` means the inference-cost component
    is unavailable: the ranker omits it and renormalizes the other weights.
    The weights are experimental defaults, not calibrated values.
    """

    request_weight: NonNegFloat = 0.35
    execution_time_weight: NonNegFloat = 0.35
    planner_latency_weight: NonNegFloat = 0.20
    inference_cost_weight: NonNegFloat = 0.10
    epsilon: PosFloat = 1e-6
    request_budget: PosFloat
    execution_time_budget_seconds: PosFloat
    planner_latency_budget_seconds: PosFloat
    inference_cost_budget: PosFloat | None = None

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> "ScoringConfig":
        total = (
            self.request_weight
            + self.execution_time_weight
            + self.planner_latency_weight
            + self.inference_cost_weight
        )
        if not math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-6):
            raise ValueError(f"weights must sum to 1.0, got {total}")
        return self


class CandidateOutcomeSummary(_Model):
    """Per-candidate execution history (contract 11.4).

    Skipped and never-executed candidates must not be recorded as failed
    attempts, so ``successful + failed <= execution_attempts`` is allowed.
    Fingerprints are stored de-duplicated and sorted.
    """

    execution_attempts: NonNegInt = 0
    successful_attempts: NonNegInt = 0
    failed_attempts: NonNegInt = 0
    confirmed_finding_fingerprints: list[Id] = Field(default_factory=list)
    last_status: ExecutionStatus | None = None

    @field_validator("confirmed_finding_fingerprints")
    @classmethod
    def _sort_unique(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @model_validator(mode="after")
    def _attempts_consistent(self) -> "CandidateOutcomeSummary":
        if self.successful_attempts + self.failed_attempts > self.execution_attempts:
            raise ValueError("successful + failed attempts exceed execution_attempts")
        return self


class PlannerHistory(_Model):
    """Planner feedback state (contract 11.4).

    In-memory sets/dicts serialize as sorted lists / key-sorted dicts so
    persisted output is stable.
    """

    executed_candidate_ids: set[Id] = Field(default_factory=set)
    outcomes_by_candidate: dict[Id, CandidateOutcomeSummary] = Field(default_factory=dict)
    confirmed_finding_fingerprints: set[Id] = Field(default_factory=set)
    policy_version: Id

    @field_serializer("executed_candidate_ids", "confirmed_finding_fingerprints")
    def _serialize_sets(self, value: set[str]) -> list[str]:
        return sorted(value)

    @field_serializer("outcomes_by_candidate", mode="wrap")
    def _serialize_outcomes(self, value: Any, handler: SerializerFunctionWrapHandler) -> Any:
        dumped = handler(value)
        return dict(sorted(dumped.items()))


class BudgetState(_Model):
    """Runner-owned mutable budget state (contract 11.5).

    Only the runner mutates this. Assignment is re-validated, so a negative
    ``requests_used`` is rejected even on mutation.
    """

    run_id: Id
    requests_used: NonNegInt = 0
    started_at: UtcDatetime
    stop_reason: str | None = None


class ValidationContext(_Model):
    """Context for finding validation (contract 11.6)."""

    run_id: Id
    artifact_dir: NonEmptyStr
    benchmark_id: Id
    expected_authorization_policy: NonEmptyStr
    test_identity_refs: list[RefId] = Field(default_factory=list)


class BenchmarkConfig(_Model):
    """Benchmark configuration (contract 11.7)."""

    benchmark_id: Id
    strategy: Literal["restler_baseline", "random", "heuristic", "llm_ranker", "llm_feedback"]
    target_config: TargetConfig
    execution_budget: ExecutionBudget
    repetitions: PosInt
    artifact_root: NonEmptyStr
    reset_strategy_id: Id


class BenchmarkReport(_Model):
    """Benchmark report (contract 11.8)."""

    benchmark_id: Id
    run_ids: list[Id]
    strategy: NonEmptyStr
    metrics: dict[str, float | int | str | None]
    errors: list[str]
    configuration_reference: NonEmptyStr

    @field_validator("metrics")
    @classmethod
    def _metrics_finite(cls, value: dict[str, Any]) -> dict[str, Any]:
        for name, metric in value.items():
            if isinstance(metric, float) and not math.isfinite(metric):
                raise ValueError(f"metric {name!r} must be finite")
        return value
