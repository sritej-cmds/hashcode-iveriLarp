from datetime import datetime, timezone

import pytest

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
from adaptive_restler.selection import (
    BenchmarkSelectorAdapter,
    CandidateDecision,
    CandidateStatus,
    DeterministicSelector,
    FallbackSelector,
    NoEligibleCandidateError,
    SelectionContext,
    SelectionResult,
)

TARGET = TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=False)
BUDGET = ExecutionBudget(max_requests=10, max_runtime_seconds=60.0, request_timeout_seconds=5.0)


def cand(cid, steps=1):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=f"/{cid}/{i}") for i in range(steps)],
        source="generated",
        estimated_request_count=steps,
    )


def ctx(remaining=10):
    return SelectionContext(target=TARGET, remaining_requests=remaining)


def done(cid, n):
    return ExecutionResult(
        run_id="run-1",
        candidate_id=cid,
        status=ExecutionStatus.COMPLETED,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=0.1,
        request_count=n,
        budget_exhausted=False,
    )


# ---- deterministic selector -------------------------------------------------


def test_deterministic_orders_cheapest_first_ties_by_id():
    pool = [cand("c", 3), cand("b", 1), cand("a", 1), cand("d", 2)]
    r = DeterministicSelector().rank(pool, ctx())
    assert r.ranked_ids == ["a", "b", "d", "c"]
    assert not r.fallback_used


def test_deterministic_is_stable_and_independent_of_input_order():
    pool = [cand("c", 3), cand("b", 1), cand("a", 1)]
    first = DeterministicSelector().rank(pool, ctx())
    second = DeterministicSelector().rank(list(reversed(pool)), ctx())
    assert first.ranked_ids == second.ranked_ids
    assert first.decisions == second.decisions


def test_deterministic_ranks_only_supplied_ids_and_does_not_mutate_input():
    pool = [cand("b", 2), cand("a", 1)]
    snapshot = list(pool)
    r = DeterministicSelector().rank(pool, ctx())
    assert set(r.ranked_ids) <= {"a", "b"}
    assert pool == snapshot


def test_unaffordable_candidates_are_rejected_not_ranked():
    r = DeterministicSelector().rank([cand("a", 1), cand("big", 5)], ctx(remaining=2))
    assert r.ranked_ids == ["a"]
    assert [d.candidate_id for d in r.rejected] == ["big"]


def test_empty_pool_gives_empty_result():
    r = DeterministicSelector().rank([], ctx())
    assert r.ranked_ids == [] and r.decisions == []


# ---- fallback ---------------------------------------------------------------


class Raising:
    name = "raising"

    def rank(self, candidates, context):
        raise RuntimeError("secret-token-123")


class Ghost:
    """Returns an ID that is not in the pool, as a misbehaving model might."""

    name = "ghost"

    def rank(self, candidates, context):
        return SelectionResult(
            selector_name="ghost",
            ranked_ids=["ghost-1"],
            decisions=[CandidateDecision(candidate_id="ghost-1", status=CandidateStatus.VALID)],
        )


class Mixed:
    name = "mixed"

    def rank(self, candidates, context):
        ids = ["ghost-1", "a"]
        return SelectionResult(
            selector_name="mixed",
            ranked_ids=ids,
            decisions=[CandidateDecision(candidate_id=i, status=CandidateStatus.VALID) for i in ids],
        )


class Spy(DeterministicSelector):
    calls = 0

    def rank(self, candidates, context):
        Spy.calls += 1
        return super().rank(candidates, context)


def test_fallback_on_primary_exception_without_leaking_message():
    r = FallbackSelector(Raising(), DeterministicSelector()).rank([cand("a")], ctx())
    assert r.fallback_used and r.ranked_ids == ["a"]
    assert r.fallback_reason == "primary_raised:RuntimeError"
    assert "secret" not in r.model_dump_json()


def test_fallback_when_primary_returns_only_ineligible_ids():
    r = FallbackSelector(Ghost(), DeterministicSelector()).rank([cand("a")], ctx())
    assert r.fallback_used and r.ranked_ids == ["a"]
    assert r.fallback_reason == "primary_returned_no_valid_candidates"


def test_primary_ineligible_ids_are_dropped_without_fallback_when_some_valid():
    Spy.calls = 0
    r = FallbackSelector(Mixed(), Spy()).rank([cand("a")], ctx())
    assert r.ranked_ids == ["a"] and not r.fallback_used
    assert Spy.calls == 0
    assert [d.candidate_id for d in r.rejected] == ["ghost-1"]


def test_good_primary_does_not_trigger_fallback():
    Spy.calls = 0
    r = FallbackSelector(DeterministicSelector(), Spy()).rank([cand("a")], ctx())
    assert not r.fallback_used and Spy.calls == 0


# ---- benchmark adapter ------------------------------------------------------


def adapter():
    return BenchmarkSelectorAdapter(DeterministicSelector(), TARGET, BUDGET)


def test_adapter_returns_first_ranked_candidate_and_records_history():
    a = adapter()
    remaining = [cand("big", 3), cand("small", 1)]
    assert a(remaining, [], []).candidate_id == "small"
    assert len(a.history) == 1


def test_adapter_derives_remaining_budget_from_results():
    a = adapter()  # max_requests=10; 9 already used -> 1 left
    remaining = [cand("two", 2), cand("one", 1)]
    assert a(remaining, [done("x", 9)], []).candidate_id == "one"


def test_adapter_raises_when_nothing_is_eligible():
    with pytest.raises(NoEligibleCandidateError):
        adapter()([cand("two", 2)], [done("x", 9)], [])


# ---- integration through the existing run_benchmark -------------------------


def bench_config(root, max_requests):
    budget = ExecutionBudget(
        max_requests=max_requests, max_runtime_seconds=60.0, request_timeout_seconds=5.0
    )
    config = BenchmarkConfig(
        benchmark_id="sel1",
        strategy="llm_ranker",
        target_config=TARGET,
        execution_budget=budget,
        repetitions=1,
        artifact_root=str(root),
        reset_strategy_id="none",
    )
    return config, budget


def test_run_benchmark_executes_in_selector_order(tmp_path):
    config, budget = bench_config(tmp_path, max_requests=10)
    sender = FakeSender()
    selector = BenchmarkSelectorAdapter(DeterministicSelector(), TARGET, budget)
    report = run_benchmark(
        config,
        candidates=[cand("big", 3), cand("small", 1), cand("mid", 2)],
        sender=sender,
        selector=selector,
    )
    assert report.errors == []
    urls = [c["url"] for c in sender.calls]
    assert urls[0].endswith("/small/0")
    assert urls[1].endswith("/mid/0")
    assert len(urls) == 6


def test_ineligible_candidate_never_reaches_the_sender(tmp_path):
    config, budget = bench_config(tmp_path, max_requests=4)
    sender = FakeSender()
    selector = BenchmarkSelectorAdapter(DeterministicSelector(), TARGET, budget)
    report = run_benchmark(
        config,
        candidates=[cand("big", 3), cand("small", 1), cand("mid", 2)],
        sender=sender,
        selector=selector,
    )
    assert not any("/big/" in c["url"] for c in sender.calls)
    assert len(sender.calls) == 3
    # Current harness behavior: an unselectable pool ends the run as "incomplete".
    assert any("NoEligibleCandidateError" in e for e in report.errors)
