import pytest
from pydantic import ValidationError

from adaptive_restler.selection.contract import (
    CandidateDecision,
    CandidateStatus,
    RejectionCode,
    SelectionProposal,
    SelectionResult,
)


def valid(cid):
    return CandidateDecision(candidate_id=cid, status=CandidateStatus.VALID)


def test_valid_decision_accepted():
    d = valid("a")
    assert d.rejection_code is None


def test_rejected_decision_requires_code():
    with pytest.raises(ValidationError):
        CandidateDecision(candidate_id="a", status=CandidateStatus.REJECTED)


def test_valid_decision_cannot_carry_rejection():
    with pytest.raises(ValidationError):
        CandidateDecision(
            candidate_id="a",
            status=CandidateStatus.VALID,
            rejection_code=RejectionCode.UNKNOWN_CANDIDATE,
        )


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_proposal_priority_must_be_in_unit_range(bad):
    with pytest.raises(ValidationError):
        SelectionProposal(candidate_id="a", priority=bad)


def test_proposal_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        SelectionProposal(candidate_id="a", method="GET")


def test_proposal_rejects_bad_ids_and_oversized_rationale():
    with pytest.raises(ValidationError):
        SelectionProposal(candidate_id=" a")
    with pytest.raises(ValidationError):
        SelectionProposal(candidate_id="a", rationale="x" * 501)


def test_result_ranked_ids_must_match_valid_decisions():
    with pytest.raises(ValidationError):
        SelectionResult(selector_name="s", ranked_ids=["a"], decisions=[])
    with pytest.raises(ValidationError):
        SelectionResult(selector_name="s", ranked_ids=["b", "a"], decisions=[valid("a"), valid("b")])


def test_result_valid_id_cannot_repeat():
    with pytest.raises(ValidationError):
        SelectionResult(selector_name="s", ranked_ids=["a", "a"], decisions=[valid("a"), valid("a")])


def test_fallback_fields_must_agree():
    with pytest.raises(ValidationError):
        SelectionResult(selector_name="s", ranked_ids=[], decisions=[], fallback_used=True)
    with pytest.raises(ValidationError):
        SelectionResult(selector_name="s", ranked_ids=[], decisions=[], fallback_reason="x")


def test_result_rejected_property_and_json_roundtrip():
    rej = CandidateDecision(
        candidate_id="x", status=CandidateStatus.REJECTED, rejection_code=RejectionCode.UNKNOWN_CANDIDATE
    )
    r = SelectionResult(selector_name="s", ranked_ids=["a"], decisions=[valid("a"), rej])
    assert r.rejected == [rej]
    assert SelectionResult.model_validate_json(r.model_dump_json()) == r
