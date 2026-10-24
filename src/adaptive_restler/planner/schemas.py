"""Validation of raw assessment provider output (contract 5.1, Section 6).

A real LLM provider returns text. This module turns that text into validated
``CandidateAssessment`` objects, item by item, and reports everything it
rejected. It never raises for bad *provider* output; callers decide what to do
with ``missing_candidate_ids`` (the documented fallback, decided separately).

Expected provider JSON::

    {"assessments": [
        {"candidate_id": "...",
         "security_hypotheses": ["..."],
         "rationale": "...",
         "predicted_discovery_score": 0.0-1.0,
         "expected_impact_score": 0.0-1.0,
         "confidence": 0.0-1.0,
         "missing_information": ["..."]}
    ]}

``provider_id`` and ``prompt_version`` are NOT accepted from the provider
output; they are attached by the caller. Unknown fields (for example
``severity`` or ``confirmed``) cause the item to be rejected, because an
assessment must never assign severity or confirm a vulnerability.

Issue details are sanitized: they contain field locations and error types
only, never the provider's raw text.
"""

import json
from dataclasses import dataclass, field
from typing import Annotated, Sequence

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
)

from adaptive_restler.models import CandidateAssessment, Id, Score

__all__ = [
    "DEFAULT_MAX_RESPONSE_CHARS",
    "ParseIssue",
    "ParseResult",
    "RawAssessment",
    "parse_provider_response",
]

DEFAULT_MAX_RESPONSE_CHARS = 200_000
MAX_HYPOTHESES = 10
MAX_HYPOTHESIS_CHARS = 500
MAX_RATIONALE_CHARS = 2_000
MAX_MISSING_INFO_ITEMS = 10
_MAX_DETAIL_CHARS = 300

_Hypothesis = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_HYPOTHESIS_CHARS)
]
_Rationale = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_RATIONALE_CHARS)
]
_MissingInfo = Annotated[str, StringConstraints(max_length=MAX_HYPOTHESIS_CHARS)]

_ID_ADAPTER = TypeAdapter(Id)


class RawAssessment(BaseModel):
    """One assessment as emitted by a provider (no provider_id/prompt_version)."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: Id
    security_hypotheses: list[_Hypothesis] = Field(max_length=MAX_HYPOTHESES)
    rationale: _Rationale
    predicted_discovery_score: Score
    expected_impact_score: Score
    confidence: Score
    missing_information: list[_MissingInfo] = Field(
        default_factory=list, max_length=MAX_MISSING_INFO_ITEMS
    )

    def to_assessment(self, provider_id: str, prompt_version: str) -> CandidateAssessment:
        return CandidateAssessment(
            **self.model_dump(), provider_id=provider_id, prompt_version=prompt_version
        )


@dataclass(frozen=True)
class ParseIssue:
    """A sanitized description of something that was rejected."""

    candidate_id: str | None  # only set when it matches a submitted candidate
    code: str
    detail: str


@dataclass(frozen=True)
class ParseResult:
    """Outcome of parsing one provider response.

    ``assessments`` follow the order of ``expected_candidate_ids``.
    ``missing_candidate_ids`` lists every expected candidate with no valid
    assessment, in the same order. These need the documented fallback.
    """

    assessments: list[CandidateAssessment] = field(default_factory=list)
    missing_candidate_ids: list[str] = field(default_factory=list)
    issues: list[ParseIssue] = field(default_factory=list)


def _clip(text: str, limit: int = _MAX_DETAIL_CHARS) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned[:limit]


def _describe(exc: ValidationError) -> str:
    """Field locations and error types only; never the offending values."""
    parts = []
    for err in exc.errors()[:5]:
        # Only known field names are echoed; a provider-chosen key name is not.
        first = str(err["loc"][0]) if err["loc"] else "<item>"
        name = first if first in RawAssessment.model_fields else "<unknown_field>"
        parts.append(f"{name}: {err['type']}")
    return _clip("; ".join(parts))


def parse_provider_response(
    raw: str | bytes,
    *,
    expected_candidate_ids: Sequence[str],
    provider_id: str,
    prompt_version: str,
    max_response_chars: int = DEFAULT_MAX_RESPONSE_CHARS,
) -> ParseResult:
    """Validate provider JSON into assessments for the submitted candidates.

    Raises ``ValueError`` only for caller bugs (duplicate expected IDs, an
    invalid ``provider_id``/``prompt_version``). Bad provider output never
    raises; it is reported in ``issues``.

    Rules:
    - Responses larger than ``max_response_chars`` are rejected unparsed.
    - Items are validated independently; one bad item does not discard others.
    - Items for unknown candidate IDs are ignored and reported.
    - A candidate ID that appears more than once is ambiguous: every item for
      it is dropped and the candidate is reported missing.
    """
    expected = list(expected_candidate_ids)
    expected_set = set(expected)
    if len(expected_set) != len(expected):
        raise ValueError("expected_candidate_ids must be unique")
    _ID_ADAPTER.validate_python(provider_id)
    _ID_ADAPTER.validate_python(prompt_version)

    def fail(code: str, detail: str) -> ParseResult:
        return ParseResult([], list(expected), [ParseIssue(None, code, detail)])

    if len(raw) > max_response_chars:
        return fail("response_too_large", f"{len(raw)} exceeds limit {max_response_chars}")
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return fail("invalid_json", "response is not valid JSON")
    if not isinstance(payload, dict) or not isinstance(payload.get("assessments"), list):
        return fail("invalid_structure", "expected an object with an 'assessments' list")

    issues: list[ParseIssue] = []
    seen_counts: dict[str, int] = {}
    valid: dict[str, RawAssessment] = {}

    for index, item in enumerate(payload["assessments"]):
        if not isinstance(item, dict):
            issues.append(ParseIssue(None, "invalid_item", f"item {index} is not an object"))
            continue
        candidate_id = item.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id not in expected_set:
            issues.append(
                ParseIssue(None, "unknown_candidate_id", f"item {index} has an unexpected candidate_id")
            )
            continue
        seen_counts[candidate_id] = seen_counts.get(candidate_id, 0) + 1
        try:
            valid[candidate_id] = RawAssessment.model_validate(item)
        except ValidationError as exc:
            issues.append(ParseIssue(candidate_id, "invalid_item", _describe(exc)))

    for candidate_id in expected:
        if seen_counts.get(candidate_id, 0) > 1:
            valid.pop(candidate_id, None)
            issues.append(
                ParseIssue(candidate_id, "duplicate_candidate_id", "multiple items for one candidate")
            )

    assessments = [
        valid[cid].to_assessment(provider_id, prompt_version) for cid in expected if cid in valid
    ]
    missing = [cid for cid in expected if cid not in valid]
    return ParseResult(assessments, missing, issues)
