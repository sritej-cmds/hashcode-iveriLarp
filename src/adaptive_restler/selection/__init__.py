"""Sequence selection: contract, deterministic validation and selectors.

Models (and later LLM stages) only *propose and order* candidates. Eligibility
is decided by ``validator.validate_proposals`` in plain code, and
``execute_candidate`` still enforces target, auth and budget as the last gate.
"""

from adaptive_restler.selection.contract import (
    CandidateDecision,
    CandidateStatus,
    RejectionCode,
    SelectionContext,
    SelectionProposal,
    SelectionResult,
)
from adaptive_restler.selection.selector import (
    BenchmarkSelectorAdapter,
    DeterministicSelector,
    FallbackSelector,
    NoEligibleCandidateError,
    SequenceSelector,
    build_result,
)
from adaptive_restler.selection.validator import validate_proposals

__all__ = [
    "BenchmarkSelectorAdapter",
    "CandidateDecision",
    "CandidateStatus",
    "DeterministicSelector",
    "FallbackSelector",
    "NoEligibleCandidateError",
    "RejectionCode",
    "SelectionContext",
    "SelectionProposal",
    "SelectionResult",
    "SequenceSelector",
    "build_result",
    "validate_proposals",
]
