"""Model-independent selector interface, deterministic selector, fallback.

The runner/benchmark depends only on ``SequenceSelector`` (or the
``BenchmarkSelectorAdapter`` callable); never on a model or provider.
"""

import time
from collections.abc import Sequence
from typing import Protocol

from adaptive_restler.models import (
    CandidateSequence,
    ExecutionBudget,
    ExecutionResult,
    Finding,
    TargetConfig,
)
from adaptive_restler.selection.contract import (
    CandidateStatus,
    SelectionContext,
    SelectionProposal,
    SelectionResult,
)
from adaptive_restler.selection.validator import validate_proposals

__all__ = [
    "BenchmarkSelectorAdapter",
    "DeterministicSelector",
    "FallbackSelector",
    "NoEligibleCandidateError",
    "SequenceSelector",
    "build_result",
]


class SequenceSelector(Protocol):
    """Anything that can rank a candidate pool. Must not execute or touch targets."""

    name: str

    def rank(
        self, candidates: Sequence[CandidateSequence], context: SelectionContext
    ) -> SelectionResult: ...


class NoEligibleCandidateError(Exception):
    """No candidate in the pool passed validation."""


def build_result(
    name: str,
    proposals: Sequence[SelectionProposal],
    pool: Sequence[CandidateSequence],
    context: SelectionContext,
    *,
    latency_seconds: float = 0.0,
) -> SelectionResult:
    """Validate proposals and package them. The single path to a SelectionResult."""
    decisions = validate_proposals(proposals, pool, context)
    ranked = [d.candidate_id for d in decisions if d.status is CandidateStatus.VALID]
    return SelectionResult(
        selector_name=name,
        ranked_ids=ranked,
        decisions=decisions,
        latency_seconds=latency_seconds,
    )


class DeterministicSelector:
    """Cheapest estimated sequence first, ties by candidate_id.

    Same ordering as the benchmark's ``heuristic`` strategy. No randomness, no
    network, independent of input order.
    """

    name = "deterministic_cheapest_first"

    def rank(
        self, candidates: Sequence[CandidateSequence], context: SelectionContext
    ) -> SelectionResult:
        start = time.perf_counter()
        ordered = sorted(candidates, key=lambda c: (c.estimated_request_count, c.candidate_id))
        proposals = [SelectionProposal(candidate_id=c.candidate_id) for c in ordered]
        return build_result(
            self.name,
            proposals,
            candidates,
            context,
            latency_seconds=time.perf_counter() - start,
        )


class FallbackSelector:
    """Use ``primary``; fall back to ``fallback`` on failure or no usable output.

    The primary's result is re-validated here, so even a buggy selector cannot
    leak an ineligible ID. Exception text is never recorded (only its type).
    This wrapper cannot pre-empt a hung synchronous call; model timeouts belong
    in the provider layer.
    """

    def __init__(self, primary: SequenceSelector, fallback: SequenceSelector) -> None:
        self.primary = primary
        self.fallback = fallback
        self.name = f"{primary.name}+fallback"

    def rank(
        self, candidates: Sequence[CandidateSequence], context: SelectionContext
    ) -> SelectionResult:
        start = time.perf_counter()
        reason: str | None = None
        primary_rejections = []
        safe: SelectionResult | None = None

        try:
            raw = self.primary.rank(candidates, context)
        except Exception as exc:  # message deliberately not recorded
            reason = f"primary_raised:{type(exc).__name__}"
        else:
            proposals = [
                SelectionProposal(candidate_id=d.candidate_id, priority=d.priority, rationale=d.rationale)
                for d in raw.decisions
                if d.status is CandidateStatus.VALID
            ]
            safe = build_result(raw.selector_name, proposals, candidates, context)
            primary_rejections = raw.rejected
            if not safe.ranked_ids and candidates:
                reason = "primary_returned_no_valid_candidates"

        if reason is None and safe is not None:
            return SelectionResult(
                selector_name=safe.selector_name,
                ranked_ids=safe.ranked_ids,
                decisions=primary_rejections + safe.decisions,
                latency_seconds=time.perf_counter() - start,
            )

        fb = self.fallback.rank(candidates, context)
        return SelectionResult(
            selector_name=fb.selector_name,
            ranked_ids=fb.ranked_ids,
            decisions=fb.decisions,
            fallback_used=True,
            fallback_reason=reason,
            latency_seconds=time.perf_counter() - start,
        )


class BenchmarkSelectorAdapter:
    """Adapts a ``SequenceSelector`` to the benchmark's ``Selector`` callable.

    Signature: ``(remaining, results, findings) -> CandidateSequence``.
    Remaining requests are derived from the results (the executor counts each
    dispatched request in ``request_count``). Raises ``NoEligibleCandidateError``
    when nothing validates; it never returns an unvalidated candidate.
    ``history`` collects every SelectionResult for run records; the instance is
    reused across repetitions, so history spans them.
    """

    def __init__(
        self, selector: SequenceSelector, target: TargetConfig, budget: ExecutionBudget
    ) -> None:
        self.selector = selector
        self.target = target
        self.budget = budget
        self.history: list[SelectionResult] = []

    def __call__(
        self,
        remaining: list[CandidateSequence],
        results: list[ExecutionResult],
        findings: list[Finding],
    ) -> CandidateSequence:
        used = sum(r.request_count for r in results)
        context = SelectionContext(
            target=self.target,
            remaining_requests=max(0, self.budget.max_requests - used),
            results=tuple(results),
            findings=tuple(findings),
        )
        result = self.selector.rank(remaining, context)
        self.history.append(result)
        by_id = {c.candidate_id: c for c in remaining}
        for cid in result.ranked_ids:
            if cid in by_id:
                return by_id[cid]
        raise NoEligibleCandidateError("no candidate passed validation")
