import pytest

from adaptive_restler.models import (
    CandidateAssessment,
    CandidateCost,
    CandidateRequest,
    CandidateSequence,
    PlannerHistory,
    ScoringConfig,
)
from adaptive_restler.planner.scoring import rank_candidates

CFG = ScoringConfig(
    request_budget=10.0, execution_time_budget_seconds=60.0, planner_latency_budget_seconds=60.0
)
HIST = PlannerHistory(policy_version="v1")


def cand(cid, n=1):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=f"/{cid}/{i}") for i in range(n)],
        source="generated",
        estimated_request_count=n,
    )


def assess(cid, p, v):
    return CandidateAssessment(
        candidate_id=cid, security_hypotheses=[], rationale="r",
        predicted_discovery_score=p, expected_impact_score=v, confidence=0.5,
        provider_id="t", prompt_version="v",
    )


def test_priority_values_and_order():
    r = rank_candidates(
        [cand("a", 1), cand("b", 2)], [assess("a", 0.2, 0.5), assess("b", 0.9, 0.9)], [], HIST, CFG
    )
    assert [x.candidate_id for x in r] == ["b", "a"] and [x.rank for x in r] == [1, 2]
    assert r[0].expected_value == pytest.approx(0.81) and r[0].normalized_cost == pytest.approx(0.2)
    assert r[0].priority_score == pytest.approx(4.05, rel=1e-3)
    assert r[1].priority_score == pytest.approx(1.0, rel=1e-3)


def test_cost_record_renormalizes_without_inference():
    cost = CandidateCost(
        candidate_id="a", estimated_request_count=1,
        estimated_execution_seconds=6.0, estimated_planner_seconds=3.0,
    )
    r = rank_candidates([cand("a")], [assess("a", 0.5, 0.5)], [cost], HIST, CFG)
    assert r[0].normalized_cost == pytest.approx(0.08 / 0.9)


def test_cost_record_with_inference_component():
    cfg = CFG.model_copy(update={"inference_cost_budget": 2.0})
    cost = CandidateCost(
        candidate_id="a", estimated_request_count=1, estimated_execution_seconds=6.0,
        estimated_planner_seconds=3.0, estimated_inference_cost=1.0,
    )
    r = rank_candidates([cand("a")], [assess("a", 0.5, 0.5)], [cost], HIST, cfg)
    assert r[0].normalized_cost == pytest.approx(0.13)


def test_missing_assessment_ranks_last_with_zero_value():
    r = rank_candidates([cand("a"), cand("b")], [assess("a", 0.1, 0.1)], [], HIST, CFG)
    assert [x.candidate_id for x in r] == ["a", "b"]
    assert r[1].expected_value == 0.0 and "no assessment" in r[1].reasons[0]


def test_executed_candidate_has_zero_novelty():
    hist = PlannerHistory(policy_version="v1", executed_candidate_ids={"b"})
    r = rank_candidates(
        [cand("a"), cand("b")], [assess("a", 0.1, 0.1), assess("b", 0.9, 0.9)], [], hist, CFG
    )
    assert [x.candidate_id for x in r] == ["a", "b"] and r[1].priority_score == 0.0


def test_ties_break_by_id_and_input_order_does_not_matter():
    pool = [cand("c"), cand("a"), cand("b")]
    ass = [assess(i, 0.5, 0.5) for i in "cab"]
    r1 = rank_candidates(pool, ass, [], HIST, CFG)
    r2 = rank_candidates(list(reversed(pool)), list(reversed(ass)), [], HIST, CFG)
    assert [x.candidate_id for x in r1] == ["a", "b", "c"] == [x.candidate_id for x in r2]


def test_id_mismatches_and_duplicates_rejected():
    with pytest.raises(ValueError):
        rank_candidates([cand("a")], [assess("zzz", 0.5, 0.5)], [], HIST, CFG)
    with pytest.raises(ValueError):
        rank_candidates([cand("a")], [assess("a", 0.5, 0.5)] * 2, [], HIST, CFG)
    with pytest.raises(ValueError):
        rank_candidates([cand("a"), cand("a")], [], [], HIST, CFG)
    bad = CandidateCost(
        candidate_id="zzz", estimated_request_count=1,
        estimated_execution_seconds=1.0, estimated_planner_seconds=1.0,
    )
    with pytest.raises(ValueError):
        rank_candidates([cand("a")], [], [bad], HIST, CFG)


def test_no_usable_cost_component_is_rejected():
    cfg = CFG.model_copy(
        update={"request_weight": 0.0, "execution_time_weight": 1.0,
                "planner_latency_weight": 0.0, "inference_cost_weight": 0.0}
    )
    with pytest.raises(ValueError):
        rank_candidates([cand("a")], [assess("a", 0.5, 0.5)], [], HIST, cfg)