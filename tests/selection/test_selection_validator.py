from datetime import datetime, timezone

import pytest

from adaptive_restler.models import (
    CandidateRequest,
    CandidateSequence,
    ExecutionResult,
    ExecutionStatus,
    TargetConfig,
)
from adaptive_restler.selection.contract import (
    CandidateStatus,
    RejectionCode,
    SelectionContext,
    SelectionProposal,
)
from adaptive_restler.selection.validator import validate_proposals

TARGET = TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=False)


def cand(cid, steps=1, est=None, auth=None):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=f"/{cid}/{i}") for i in range(steps)],
        source="generated",
        estimated_request_count=est or steps,
        auth_profile_id=auth,
    )


def ctx(remaining=10, results=(), target=TARGET):
    return SelectionContext(target=target, remaining_requests=remaining, results=tuple(results))


def prop(cid, priority=None):
    return SelectionProposal(candidate_id=cid, priority=priority)


def done(cid):
    return ExecutionResult(
        run_id="run-1",
        candidate_id=cid,
        status=ExecutionStatus.COMPLETED,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=0.1,
        request_count=1,
        budget_exhausted=False,
    )


def only(decisions):
    assert len(decisions) == 1
    return decisions[0]


def test_valid_candidate_accepted():
    d = only(validate_proposals([prop("a")], [cand("a")], ctx()))
    assert d.status is CandidateStatus.VALID and d.rejection_code is None


def test_unknown_candidate_rejected():
    d = only(validate_proposals([prop("ghost")], [cand("a")], ctx()))
    assert d.rejection_code is RejectionCode.UNKNOWN_CANDIDATE


def test_duplicate_proposal_second_rejected_first_kept():
    ds = validate_proposals([prop("a"), prop("a")], [cand("a")], ctx())
    assert [d.status for d in ds] == [CandidateStatus.VALID, CandidateStatus.REJECTED]
    assert ds[1].rejection_code is RejectionCode.DUPLICATE_PROPOSAL


def test_already_executed_rejected():
    d = only(validate_proposals([prop("a")], [cand("a")], ctx(results=[done("a")])))
    assert d.rejection_code is RejectionCode.ALREADY_EXECUTED


def test_unauthorized_auth_profile_rejected_and_matching_profile_accepted():
    d = only(validate_proposals([prop("a")], [cand("a", auth="alice")], ctx()))
    assert d.rejection_code is RejectionCode.UNAUTHORIZED_AUTH_PROFILE
    target = TargetConfig(
        base_url="http://localhost:8080", allowed_hosts=["localhost"], auth_profile_id="alice", dry_run=False
    )
    d = only(validate_proposals([prop("a")], [cand("a", auth="alice")], ctx(target=target)))
    assert d.status is CandidateStatus.VALID


def test_target_outside_allowlist_rejected():
    # model_construct bypasses TargetConfig's own checks: simulates a tampered target.
    bad = TargetConfig.model_construct(
        base_url="http://evil.example", allowed_hosts=["localhost"], dry_run=False
    )
    d = only(validate_proposals([prop("a")], [cand("a")], ctx(target=bad)))
    assert d.rejection_code is RejectionCode.TARGET_NOT_ALLOWED
    assert "evil" not in (d.rejection_detail or "")


def test_invalid_allowlist_entry_rejected():
    bad = TargetConfig.model_construct(
        base_url="http://localhost:8080", allowed_hosts=["bad/host"], dry_run=False
    )
    d = only(validate_proposals([prop("a")], [cand("a")], ctx(target=bad)))
    assert d.rejection_code is RejectionCode.INVALID_TARGET_CONFIG


def test_sequence_longer_than_remaining_budget_rejected():
    d = only(validate_proposals([prop("a")], [cand("a", steps=2)], ctx(remaining=1)))
    assert d.rejection_code is RejectionCode.EXCEEDS_REMAINING_BUDGET


def test_stale_low_estimate_cannot_hide_real_cost():
    d = only(validate_proposals([prop("a")], [cand("a", steps=3, est=1)], ctx(remaining=2)))
    assert d.rejection_code is RejectionCode.EXCEEDS_REMAINING_BUDGET


def test_high_estimate_over_budget_rejected_exact_fit_accepted():
    d = only(validate_proposals([prop("a")], [cand("a", steps=1, est=5)], ctx(remaining=4)))
    assert d.rejection_code is RejectionCode.EXCEEDS_REMAINING_BUDGET
    d = only(validate_proposals([prop("a")], [cand("a", steps=2)], ctx(remaining=2)))
    assert d.status is CandidateStatus.VALID


def test_low_priority_is_valid_and_order_is_preserved():
    pool = [cand("a"), cand("b")]
    ds = validate_proposals([prop("b", 0.0), prop("a", 1.0)], pool, ctx())
    assert [d.candidate_id for d in ds] == ["b", "a"]
    assert all(d.status is CandidateStatus.VALID for d in ds)
    assert ds[0].priority == 0.0


def test_empty_proposals_give_empty_decisions():
    assert validate_proposals([], [cand("a")], ctx()) == []


def test_duplicate_ids_in_pool_is_a_caller_error():
    with pytest.raises(ValueError):
        validate_proposals([prop("a")], [cand("a"), cand("a")], ctx())


def test_negative_remaining_requests_rejected():
    with pytest.raises(ValueError):
        ctx(remaining=-1)
