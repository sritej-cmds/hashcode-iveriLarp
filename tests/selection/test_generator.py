import json
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
from adaptive_restler.selection.config import ModelConfig
from adaptive_restler.selection.contract import CandidateStatus, RejectionCode, SelectionContext
from adaptive_restler.selection.feedback import FeedbackSelector
from adaptive_restler.selection.generator import (
    ContextLimitError,
    ModelCandidateGenerator,
    build_generator_selector,
    parse_proposals,
)
from adaptive_restler.selection.provider import FakeProvider, ProviderError, ProviderTimeout
from adaptive_restler.selection.selector import BenchmarkSelectorAdapter

TARGET = TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=False)
CFG = ModelConfig()


def cand(cid, steps=1, headers=None, body=None, method="GET"):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[
            CandidateRequest(method=method, path=f"/{cid}/{i}", headers=headers or {}, body=body)
            for i in range(steps)
        ],
        source="generated",
        estimated_request_count=steps,
    )


def ctx(remaining=100, results=()):
    return SelectionContext(target=TARGET, remaining_requests=remaining, results=tuple(results))


def props(*items):
    return json.dumps({"proposals": list(items)})


def p(cid, priority=None, rationale=None):
    d = {"candidate_id": cid}
    if priority is not None:
        d["priority"] = priority
    if rationale is not None:
        d["rationale"] = rationale
    return d


def gen(script=None, **cfg):
    provider = FakeProvider(script)
    return ModelCandidateGenerator(provider, ModelConfig(**cfg)), provider


POOL = [cand("a"), cand("b", 2), cand("c", 3)]


def test_valid_response_keeps_model_order_priority_and_rationale():
    g, provider = gen([props(p("c", 0.9, "why"), p("a", 0.4))])
    r = g.rank(POOL, ctx())
    assert r.ranked_ids == ["c", "a"]
    assert r.decisions[0].priority == 0.9 and r.decisions[0].rationale == "why"
    call = provider.calls[0]
    assert call["model"] == CFG.small_model
    assert call["max_output_tokens"] == CFG.max_output_tokens
    assert call["timeout_seconds"] == CFG.llm_timeout_seconds


def test_unknown_id_is_rejected_and_valid_kept():
    g, _ = gen([props(p("ghost"), p("a"))])
    r = g.rank(POOL, ctx())
    assert r.ranked_ids == ["a"]
    assert r.rejected[0].rejection_code is RejectionCode.UNKNOWN_CANDIDATE


def test_duplicate_proposal_second_rejected():
    g, _ = gen([props(p("a"), p("a"))])
    r = g.rank(POOL, ctx())
    assert r.ranked_ids == ["a"]
    assert r.rejected[0].rejection_code is RejectionCode.DUPLICATE_PROPOSAL


def test_empty_proposals_give_empty_result():
    g, _ = gen([props()])
    assert g.rank(POOL, ctx()).ranked_ids == []


def test_invalid_json_gives_empty_result_then_fallback_recovers():
    g, _ = gen(["not json"])
    assert g.rank(POOL, ctx()).ranked_ids == []
    assert g.records[-1].issues == ("invalid_json",)
    sel = build_generator_selector(FakeProvider(["not json"]), CFG)
    r = sel.rank(POOL, ctx())
    assert r.fallback_used and r.ranked_ids == ["a", "b", "c"]
    assert r.fallback_reason == "primary_returned_no_valid_candidates"


@pytest.mark.parametrize(
    "raw,code",
    [
        ("", "invalid_json"),
        ("[]", "invalid_structure"),
        ("{}", "invalid_structure"),
        ('{"proposals": {}}', "invalid_structure"),
        pytest.param("x" * 200_001, "response_too_large", id="response_too_large"),
    ],
)
def test_parse_rejects_malformed_output(raw, code):
    assert parse_proposals(raw, 10) == ([], [code])


def test_bad_items_dropped_good_kept():
    raw = props({"candidate_id": "a", "method": "GET"}, p("b", 5.0), "text", p("c", 0.5))
    proposals, issues = parse_proposals(raw, 10)
    assert [x.candidate_id for x in proposals] == ["c"]
    assert issues == ["invalid_item"] * 3


def test_provider_timeout_and_error_propagate_and_fallback_reports_type():
    g, _ = gen([ProviderTimeout("t")])
    with pytest.raises(ProviderTimeout):
        g.rank(POOL, ctx())
    r = build_generator_selector(FakeProvider([ProviderTimeout("t")]), CFG).rank(POOL, ctx())
    assert r.fallback_used and r.fallback_reason == "primary_raised:ProviderTimeout"
    r = build_generator_selector(FakeProvider([ProviderError("down")]), CFG).rank(POOL, ctx())
    assert r.fallback_reason == "primary_raised:ProviderError"


def test_context_limit_raises_and_fallback_reports_it():
    cfg = ModelConfig(max_prompt_chars=50)
    with pytest.raises(ContextLimitError):
        ModelCandidateGenerator(FakeProvider(), cfg).rank(POOL, ctx())
    r = build_generator_selector(FakeProvider(), cfg).rank(POOL, ctx())
    assert r.fallback_reason == "primary_raised:ContextLimitError"


def test_candidate_limit_bounds_what_the_model_sees():
    g, provider = gen([props()], candidate_limit=2)
    g.rank(POOL, ctx())
    prompt = provider.calls[0]["prompt"]
    assert "- a |" in prompt and "- b |" in prompt and "- c |" not in prompt
    assert g.records[0].candidates_shown == 2


def test_prompt_never_contains_headers_bodies_or_queries():
    pool = [cand("a", headers={"X-Trace": "trace-abc"}, body={"k": "SECRETBODY"}, method="POST")]
    g, provider = gen([props()])
    g.rank(pool, ctx())
    prompt = provider.calls[0]["prompt"]
    assert "POST /a/0" in prompt
    assert "trace-abc" not in prompt and "SECRETBODY" not in prompt


def test_exercised_operations_appear_in_prompt():
    done = ExecutionResult(
        run_id="run-1", candidate_id="x", status=ExecutionStatus.COMPLETED,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc), duration_seconds=0.1,
        request_count=1, responses=[{"step": 0, "method": "GET", "path": "/seen"}],
        budget_exhausted=False,
    )
    g, provider = gen([props()])
    g.rank(POOL, ctx(results=[done]))
    assert "GET /seen" in provider.calls[0]["prompt"]


def test_cache_hit_skips_provider_and_different_pool_misses():
    g, provider = gen([props(p("a")), props(p("a"))], cache_enabled=True)
    g.rank(POOL, ctx())
    g.rank(POOL, ctx())
    assert len(provider.calls) == 1 and g.records[-1].cache_hit is True
    g.rank(POOL + [cand("d")], ctx())
    assert len(provider.calls) == 2


def test_cache_disabled_calls_provider_every_time():
    g, provider = gen([props(p("a")), props(p("a"))])
    g.rank(POOL, ctx())
    g.rank(POOL, ctx())
    assert len(provider.calls) == 2


def test_empty_pool_makes_no_model_call():
    g, provider = gen()
    assert g.rank([], ctx()).ranked_ids == []
    assert provider.calls == []


def test_tokens_and_latency_recorded():
    g, _ = gen([props(p("a"))])
    g.rank(POOL, ctx())
    rec = g.records[0]
    assert (rec.prompt_tokens, rec.completion_tokens) == (10, 5) and rec.proposals_parsed == 1


def test_factory_honors_fallback_strategy():
    sel = build_generator_selector(FakeProvider(), ModelConfig(fallback_strategy="feedback"))
    assert isinstance(sel.fallback, FeedbackSelector)


def test_benchmark_run_follows_model_then_falls_back_and_never_runs_unknown_ids(tmp_path):
    budget = ExecutionBudget(max_requests=10, max_runtime_seconds=60.0, request_timeout_seconds=5.0)
    config = BenchmarkConfig(
        benchmark_id="gen1", strategy="llm_ranker", target_config=TARGET,
        execution_budget=budget, repetitions=1, artifact_root=str(tmp_path),
        reset_strategy_id="none",
    )
    selector = build_generator_selector(FakeProvider([props(p("c"), p("ghost"), p("a"))]), CFG)
    adapter = BenchmarkSelectorAdapter(selector, TARGET, budget)
    sender = FakeSender()
    report = run_benchmark(config, candidates=POOL, sender=sender, selector=adapter)
    order = [c["url"].split("/")[-2] for c in sender.calls]
    assert order == ["c", "c", "c", "a", "b", "b"]
    assert report.errors == []
    assert adapter.history[1].fallback_used