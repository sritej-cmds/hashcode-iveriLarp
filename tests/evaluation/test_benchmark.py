import json

from adaptive_restler.evaluation import benchmark as bm
from adaptive_restler.evaluation.benchmark import run_benchmark
from adaptive_restler.models import (
    BenchmarkConfig,
    CandidateRequest,
    CandidateSequence,
    ExecutionBudget,
    TargetConfig,
)
from adaptive_restler.runner.executor import FakeSender


def cand(cid, steps=3):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=f"/{cid}/{i}") for i in range(steps)],
        source="generated",
        estimated_request_count=steps,
    )


def config(root, strategy="restler_baseline", reps=2, max_requests=10, dry_run=False, reset="none"):
    return BenchmarkConfig(
        benchmark_id="bench1",
        strategy=strategy,
        target_config=TargetConfig(
            base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=dry_run
        ),
        execution_budget=ExecutionBudget(
            max_requests=max_requests, max_runtime_seconds=60.0, request_timeout_seconds=5.0
        ),
        repetitions=reps,
        artifact_root=str(root),
        reset_strategy_id=reset,
    )


def paths(sender):
    return [c["url"].replace("http://localhost:8080", "") for c in sender.calls]


def test_full_run_aggregates(tmp_path):
    sender = FakeSender()
    report = run_benchmark(config(tmp_path), candidates=[cand("a"), cand("b")], sender=sender)
    m = report.metrics
    assert report.run_ids == ["bench1-r001", "bench1-r002"]
    assert m["runs_loaded"] == 2 and m["attempts_total"] == 4
    assert m["requests_total"] == 12 == len(sender.calls)
    assert report.errors == []
    assert (tmp_path / "bench1" / "report.md").exists()


def test_same_budget_applied_to_every_run(tmp_path):
    report = run_benchmark(
        config(tmp_path, max_requests=4),
        candidates=[cand("a"), cand("b"), cand("c")],
        sender=FakeSender(),
    )
    assert report.metrics["requests_total"] == 8
    assert report.metrics["attempts_budget_exhausted"] == 2
    assert report.metrics["attempts_total"] == 4


def test_5xx_becomes_lead_not_confirmed(tmp_path):
    sender = FakeSender([{"status_code": 500}] * 100)
    report = run_benchmark(config(tmp_path), candidates=[cand("a"), cand("b")], sender=sender)
    # 2 repetitions x 2 candidates; uniqueness is per run, then summed.
    assert report.metrics["leads_unique"] == 4
    assert report.metrics["confirmed_unique"] == 0


def test_dry_run_default_sends_nothing(tmp_path):
    sender = FakeSender()
    report = run_benchmark(config(tmp_path, dry_run=True), candidates=[cand("a")], sender=sender)
    assert sender.calls == []
    assert report.metrics["attempts_skipped"] == 2
    assert report.metrics["requests_total"] == 0


def test_reset_called_before_each_run(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setitem(bm._RESET_STRATEGIES, "t-count", lambda: calls.append(1))
    run_benchmark(config(tmp_path, reset="t-count"), candidates=[cand("a")], sender=FakeSender())
    assert len(calls) == 2


def test_unknown_reset_strategy_runs_nothing(tmp_path):
    sender = FakeSender()
    report = run_benchmark(config(tmp_path, reset="nope"), candidates=[cand("a")], sender=sender)
    assert sender.calls == []
    assert any("unknown reset" in e for e in report.errors)
    assert report.metrics["runs_loaded"] == 0 and report.metrics["runs_unloadable"] == 2


def test_reset_failure_is_recorded_and_next_run_continues(tmp_path, monkeypatch):
    state = {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("secret-token-123")

    monkeypatch.setitem(bm._RESET_STRATEGIES, "t-flaky", flaky)
    report = run_benchmark(config(tmp_path, reset="t-flaky"), candidates=[cand("a")], sender=FakeSender())
    assert any("reset failed" in e for e in report.errors)
    assert "secret-token-123" not in " ".join(report.errors)
    assert report.metrics["runs_loaded"] == 1 and report.metrics["runs_unloadable"] == 1


def test_llm_strategy_without_selector_is_an_error(tmp_path):
    sender = FakeSender()
    report = run_benchmark(config(tmp_path, strategy="llm_ranker"), candidates=[cand("a")], sender=sender)
    assert sender.calls == []
    assert any("no selector" in e for e in report.errors)


def test_injected_selector_is_used(tmp_path):
    sender = FakeSender()
    run_benchmark(
        config(tmp_path, strategy="llm_ranker", reps=1),
        candidates=[cand("a"), cand("b")],
        sender=sender,
        selector=lambda remaining, results, findings: remaining[-1],
    )
    assert paths(sender)[0] == "/b/0"


def test_random_is_deterministic_per_repetition(tmp_path):
    cands = [cand(c, 1) for c in "abcde"]
    s1, s2 = FakeSender(), FakeSender()
    run_benchmark(config(tmp_path / "x", strategy="random", reps=1), candidates=cands, sender=s1)
    run_benchmark(config(tmp_path / "y", strategy="random", reps=1), candidates=cands, sender=s2)
    assert paths(s1) == paths(s2)


def test_heuristic_runs_cheapest_first(tmp_path):
    sender = FakeSender()
    run_benchmark(
        config(tmp_path, strategy="heuristic", reps=1),
        candidates=[cand("big", 5), cand("small", 1)],
        sender=sender,
    )
    assert paths(sender)[0] == "/small/0"


def test_selector_crash_preserves_partial_results(tmp_path):
    calls = {"n": 0}

    def crashing(remaining, results, findings):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return remaining[0]

    report = run_benchmark(
        config(tmp_path, strategy="llm_ranker", reps=1),
        candidates=[cand("a"), cand("b")],
        sender=FakeSender(),
        selector=crashing,
    )
    assert any("incomplete" in e for e in report.errors)
    assert report.metrics["runs_loaded"] == 1
    assert report.metrics["attempts_total"] == 1


def test_missing_or_duplicate_candidates_are_errors(tmp_path):
    assert any("no candidates" in e for e in run_benchmark(config(tmp_path)).errors)
    dup = run_benchmark(config(tmp_path), candidates=[cand("a"), cand("a")], sender=FakeSender())
    assert any("duplicate" in e for e in dup.errors)


def test_environment_and_config_recorded(tmp_path):
    run_benchmark(config(tmp_path, reps=1), candidates=[cand("a")], sender=FakeSender())
    env = json.loads((tmp_path / "bench1" / "environment.json").read_text(encoding="utf-8"))
    assert {"python", "platform", "pydantic"} <= set(env)
    assert (tmp_path / "bench1" / "config.json").exists()