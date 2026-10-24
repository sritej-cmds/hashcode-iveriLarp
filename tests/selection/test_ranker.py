import json
from datetime import datetime, timezone

from adaptive_restler.evaluation.benchmark import run_benchmark
from adaptive_restler.models import (
    BenchmarkConfig,
    CandidateRequest,
    CandidateSequence,
    ExecutionBudget,
    ExecutionResult,
    ExecutionStatus,
    TargetConfig,
)
from adaptive_restler.runner.executor import FakeSender
from adaptive_restler.selection.assessor import ModelAssessmentProvider
from adaptive_restler.selection.config import ModelConfig
from adaptive_restler.selection.contract import RejectionCode, SelectionContext
from adaptive_restler.selection.provider import FakeProvider
from adaptive_restler.selection.ranker import AssessmentRankingSelector, build_assessment_ranker_selector
from adaptive_restler.selection.selector import BenchmarkSelectorAdapter

TARGET = TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=False)
BUDGET = ExecutionBudget(max_requests=10, max_runtime_seconds=60.0, request_timeout_seconds=5.0)
CFG = ModelConfig()


def cand(cid, steps=1):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=f"/{cid}/{i}") for i in range(steps)],
        source="generated",
        estimated_request_count=steps,
    )


def item(cid, p, v):
    return {
        "candidate_id": cid, "security_hypotheses": ["h"], "rationale": "r",
        "predicted_discovery_score": p, "expected_impact_score": v, "confidence": 0.5,
    }


REPLY = json.dumps({"assessments": [item("a", 0.2, 0.5), item("b", 0.9, 0.9), item("c", 0.9, 0.9)]})
POOL = [cand("a"), cand("b", 2), cand("c", 3)]
# priorities: b 0.81/0.2=4.05, c 0.81/0.3=2.7, a 0.1/0.1=1.0


def make(script):
    fake = FakeProvider(script)
    return fake, AssessmentRankingSelector(ModelAssessmentProvider(fake, CFG), CFG, BUDGET)


def ctx(remaining=100, results=()):
    return SelectionContext(target=TARGET, remaining_requests=remaining, results=tuple(results))


def test_ranks_by_priority_with_normalized_priorities():
    _, sel = make([REPLY])
    r = sel.rank(POOL, ctx())
    assert r.ranked_ids == ["b", "c", "a"]
    assert r.decisions[0].priority == 1.0 and r.decisions[1].priority < 1.0


def test_unaffordable_candidate_is_rejected_by_validator():
    _, sel = make([REPLY])
    r = sel.rank(POOL, ctx(remaining=2))
    assert r.ranked_ids == ["b", "a"]
    assert r.rejected[0].rejection_code is RejectionCode.EXCEEDS_REMAINING_BUDGET


def test_assessments_cached_and_executed_candidates_excluded():
    from datetime import timezone as tz
    done = ExecutionResult(
        run_id="run-1", candidate_id="b", status=ExecutionStatus.COMPLETED,
        started_at=datetime(2026, 1, 1, tzinfo=tz.utc), duration_seconds=0.1,
        request_count=2, responses=[], budget_exhausted=False,
    )
    fake, sel = make([REPLY])
    sel.rank(POOL, ctx())
    r = sel.rank(POOL, ctx(results=[done]))
    assert r.ranked_ids == ["c", "a"] and len(fake.calls) == 1


def test_bad_model_output_uses_mock_assessments_and_still_ranks():
    _, sel = make(["not json"])
    r = sel.rank(POOL, ctx())
    assert set(r.ranked_ids) == {"a", "b", "c"} and len(sel.fallback_reasons) == 3


def test_factory_wraps_ranker_as_primary():
    sel = build_assessment_ranker_selector(FakeProvider([REPLY]), CFG, BUDGET)
    assert isinstance(sel.primary, AssessmentRankingSelector)


def test_benchmark_run_follows_assessment_ranking(tmp_path):
    config = BenchmarkConfig(
        benchmark_id="rank1", strategy="llm_ranker", target_config=TARGET,
        execution_budget=BUDGET, repetitions=1, artifact_root=str(tmp_path),
        reset_strategy_id="none",
    )
    fake = FakeProvider([REPLY])
    selector = build_assessment_ranker_selector(fake, CFG, BUDGET)
    adapter = BenchmarkSelectorAdapter(selector, TARGET, BUDGET)
    sender = FakeSender()
    report = run_benchmark(config, candidates=POOL, sender=sender, selector=adapter)
    order = [c["url"].split("/")[-2] for c in sender.calls]
    assert order == ["b", "b", "c", "c", "c", "a"]
    assert report.errors == [] and len(fake.calls) == 1