from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from adaptive_restler.models import (
    CandidateRequest,
    CandidateSequence,
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
    TargetConfig,
)
from adaptive_restler.selection.contract import SelectionContext
from adaptive_restler.selection.feedback import (
    FeedbackConfig,
    FeedbackSelector,
    build_state,
)

TARGET = TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=False)
CFG = FeedbackConfig()


def cand(cid, paths, est=None):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=p) for p in paths],
        source="generated",
        estimated_request_count=est or len(paths),
    )


def result(cid, paths, status=ExecutionStatus.COMPLETED, sent=None):
    sent = len(paths) if sent is None else sent
    return ExecutionResult(
        run_id="run-1",
        candidate_id=cid,
        status=status,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=0.1,
        request_count=sent,
        responses=[
            {"step": i, "method": "GET", "path": p, "status_code": 200}
            for i, p in enumerate(paths[:sent])
        ],
        budget_exhausted=False,
    )


def lead(cid, fp, status=FindingStatus.LEAD):
    return Finding(
        finding_id=f"f-{cid}-{fp}",
        run_id="run-1",
        candidate_id=cid,
        category="server_error",
        status=status,
        validation_notes="note",
        fingerprint=fp,
    )


def ctx(results=(), findings=()):
    return SelectionContext(
        target=TARGET, remaining_requests=100, results=tuple(results), findings=tuple(findings)
    )


def priorities(r):
    return {d.candidate_id: d.priority for d in r.decisions}


def test_no_history_orders_by_cost_then_id():
    pool = [cand("c", ["/a", "/b"]), cand("b", ["/b"]), cand("a", ["/a"])]
    r = FeedbackSelector().rank(pool, ctx())
    assert r.ranked_ids == ["a", "b", "c"]
    assert len(set(priorities(r).values())) == 1


def test_unexercised_operations_rank_first_and_reasons_recorded():
    r = FeedbackSelector().rank(
        [cand("x", ["/a"]), cand("y", ["/b"])], ctx(results=[result("done", ["/a"])])
    )
    assert r.ranked_ids == ["y", "x"]
    p = priorities(r)
    assert p["y"] == pytest.approx(0.7) and p["x"] == pytest.approx(0.28)
    rationale = {d.candidate_id: d.rationale for d in r.decisions}
    assert "not yet exercised" in rationale["y"]
    assert "already exercised" in rationale["x"]


def test_productive_operations_outrank_quiet_ones():
    history = [result("r1", ["/a"]), result("r2", ["/c"])]
    r = FeedbackSelector().rank(
        [cand("z", ["/c"]), cand("x", ["/a"])], ctx(history, [lead("r1", "fp1")])
    )
    p = priorities(r)
    assert r.ranked_ids == ["x", "z"]
    assert p["x"] == pytest.approx(0.33) and p["z"] == pytest.approx(0.28)


def test_duplicate_fingerprint_is_not_rewarded_twice():
    history = [result("r1", ["/a"]), result("r2", ["/c"])]
    findings = [lead("r1", "fp1"), lead("r2", "fp1")]
    state = build_state(history, findings, CFG)
    assert state.productive == {"GET /a": 1}
    assert state.quiet == {"GET /c": 1}


def test_rejected_findings_are_not_productive():
    state = build_state(
        [result("r1", ["/a"])], [lead("r1", "fp1", FindingStatus.REJECTED)], CFG
    )
    assert state.productive == {} and state.quiet == {"GET /a": 1}


def test_skipped_and_budget_exhausted_results_are_ignored():
    history = [
        result("s", ["/a"], status=ExecutionStatus.SKIPPED),
        result("b", ["/b"], status=ExecutionStatus.BUDGET_EXHAUSTED),
    ]
    state = build_state(history, [], CFG)
    assert state.covered == frozenset() and state.quiet == {} and state.productive == {}


def test_partial_execution_only_covers_steps_actually_sent():
    state = build_state(
        [result("f", ["/a", "/b"], status=ExecutionStatus.FAILED, sent=1)], [], CFG
    )
    assert state.covered == frozenset({"GET /a"})


def test_counts_are_capped():
    history = [result(f"r{i}", ["/a"]) for i in range(10)]
    state = build_state(history, [], FeedbackConfig(max_count=2))
    assert state.quiet == {"GET /a": 2}


def test_scores_are_clamped_to_unit_range():
    r = FeedbackSelector(FeedbackConfig(base_score=0.0)).rank(
        [cand("x", ["/a"])], ctx(results=[result("done", ["/a"])])
    )
    assert priorities(r)["x"] == 0.0


def test_deterministic_and_independent_of_candidate_order():
    pool = [cand("c", ["/c"]), cand("a", ["/a"]), cand("b", ["/b"])]
    history = [result("r1", ["/a"])]
    first = FeedbackSelector().rank(pool, ctx(history))
    second = FeedbackSelector().rank(list(reversed(pool)), ctx(history))
    assert first.ranked_ids == second.ranked_ids
    assert first.decisions == second.decisions


def test_executed_candidates_are_not_reranked():
    r = FeedbackSelector().rank([cand("a", ["/a"])], ctx(results=[result("a", ["/a"])]))
    assert r.ranked_ids == []


def test_config_validation():
    with pytest.raises(ValidationError):
        FeedbackConfig(novelty_bonus=1.5)
    with pytest.raises(ValidationError):
        FeedbackConfig(unknown=1)
    with pytest.raises(ValidationError):
        FeedbackConfig(max_count=0)