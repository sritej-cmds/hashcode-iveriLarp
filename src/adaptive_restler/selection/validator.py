"""Deterministic candidate validation. Independent of any LLM.

Checks mirror what ``execute_candidate`` enforces (auth profile, target
allowlist, budget) so ineligible candidates are rejected *before* a slot is
spent on them. The executor remains the final gate.

Not yet covered (no OpenAPI spec or operation IDs exist in this repository):
operation-ID existence, parameter-schema checks and resource-producer
grounding. Those are deferred until that data is available.

Rejection details are fixed strings: they never echo URLs or exception text.
"""

from collections.abc import Sequence

from adaptive_restler.models import CandidateSequence
from adaptive_restler.runner.target_guard import TargetNotAllowedError, check_target_url
from adaptive_restler.selection.contract import (
    CandidateDecision,
    CandidateStatus,
    RejectionCode,
    SelectionContext,
    SelectionProposal,
)

__all__ = ["validate_proposals"]


def _reject(proposal: SelectionProposal, code: RejectionCode, detail: str) -> CandidateDecision:
    return CandidateDecision(
        candidate_id=proposal.candidate_id,
        status=CandidateStatus.REJECTED,
        rejection_code=code,
        rejection_detail=detail,
        priority=proposal.priority,
        rationale=proposal.rationale,
    )


def _check_candidate(
    candidate: CandidateSequence, context: SelectionContext
) -> tuple[RejectionCode, str] | None:
    target = context.target
    profile = candidate.auth_profile_id
    if profile is not None and profile != target.auth_profile_id:
        return RejectionCode.UNAUTHORIZED_AUTH_PROFILE, "auth profile is not authorized for this target"

    base = target.base_url.rstrip("/")
    try:
        for request in candidate.sequence:
            check_target_url(base + request.path, target.allowed_hosts)
    except TargetNotAllowedError:
        return RejectionCode.TARGET_NOT_ALLOWED, "a request URL is outside the target allowlist"
    except ValueError:
        return RejectionCode.INVALID_TARGET_CONFIG, "target configuration is invalid"

    # The executor dispatches one request per step, so the real cost can exceed
    # a stale estimated_request_count. Use the larger of the two.
    needed = max(candidate.estimated_request_count, len(candidate.sequence))
    if needed > context.remaining_requests:
        return (
            RejectionCode.EXCEEDS_REMAINING_BUDGET,
            f"needs {needed} requests, {context.remaining_requests} remaining",
        )
    return None


def validate_proposals(
    proposals: Sequence[SelectionProposal],
    pool: Sequence[CandidateSequence],
    context: SelectionContext,
) -> list[CandidateDecision]:
    """Return one decision per proposal, in proposal order.

    Raises ``ValueError`` only for a caller bug (duplicate IDs in ``pool``).
    """
    by_id: dict[str, CandidateSequence] = {}
    for candidate in pool:
        if candidate.candidate_id in by_id:
            raise ValueError("candidate pool contains a duplicate candidate_id")
        by_id[candidate.candidate_id] = candidate

    executed = context.executed_candidate_ids
    seen: set[str] = set()
    decisions: list[CandidateDecision] = []

    for proposal in proposals:
        cid = proposal.candidate_id
        if cid in seen:
            decisions.append(_reject(proposal, RejectionCode.DUPLICATE_PROPOSAL, "proposed more than once"))
            continue
        seen.add(cid)
        if cid in executed:
            decisions.append(_reject(proposal, RejectionCode.ALREADY_EXECUTED, "candidate already executed"))
            continue
        candidate = by_id.get(cid)
        if candidate is None:
            decisions.append(_reject(proposal, RejectionCode.UNKNOWN_CANDIDATE, "candidate is not in the supplied pool"))
            continue
        problem = _check_candidate(candidate, context)
        if problem is not None:
            decisions.append(_reject(proposal, *problem))
            continue
        decisions.append(
            CandidateDecision(
                candidate_id=cid,
                status=CandidateStatus.VALID,
                priority=proposal.priority,
                rationale=proposal.rationale,
            )
        )
    return decisions
