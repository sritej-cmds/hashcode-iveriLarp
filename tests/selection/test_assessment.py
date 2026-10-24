import json

import pytest

from adaptive_restler.models import CandidateRequest, CandidateSequence, PlannerContext
from adaptive_restler.selection.assessment import (
    assess_candidates,
    assess_candidates_with_report,
)
from adaptive_restler.selection.assessor import (
    PROMPT_VERSION,
    ModelAssessmentProvider,
    build_assessment_prompt,
    planner_context_from_config,
)
from adaptive_restler.selection.config import ModelConfig
from adaptive_restler.selection.generator import ContextLimitError
from adaptive_restler.selection.provider import FakeProvider, ProviderError, ProviderTimeout

CFG = ModelConfig()


def cand(cid, steps=1, tags=(), headers=None, body=None, method="GET"):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[
            CandidateRequest(method=method, path=f"/{cid}/{i}", headers=headers or {}, body=body)
            for i in range(steps)
        ],
        source="generated",
        tags=list(tags),
        estimated_request_count=steps,
    )


def ctx(size=10, timeout=60.0):
    return PlannerContext(
        run_id="run-1",
        prompt_version="assess-v1",
        max_candidates_per_assessment=size,
        assessment_timeout_seconds=timeout,
    )


def item(cid, **over):
    d = {
        "candidate_id": cid,
        "security_hypotheses": ["check object access"],
        "rationale": "r",
        "predicted_discovery_score": 0.7,
        "expected_impact_score": 0.6,
        "confidence": 0.5,
        "missing_information": [],
    }
    d.update(over)
    return d


def reply(*items):
    return json.dumps({"assessments": list(items)})


POOL = [cand("a"), cand("b", 2), cand("c", 3)]


def setup(script, **cfg):
    fake = FakeProvider(script)
    return fake, ModelAssessmentProvider(fake, ModelConfig(**cfg))


def test_provider_valid_batch_and_call_parameters():
    fake, prov = setup([reply(item("a"), item("b"))])
    out = prov.assess(POOL[:2], ctx(timeout=5.0))
    assert [a.candidate_id for a in out] == ["a", "b"]
    assert out[0].provider_id == f"llm:{CFG.large_model}" and out[0].prompt_version == "assess-v1"
    call = fake.calls[0]
    assert call["model"] == CFG.large_model and call["timeout_seconds"] == 5.0
    assert prov.records[0].assessments_returned == 2 and prov.records[0].missing_count == 0


def test_prompt_has_method_path_tags_but_no_headers_bodies():
    pool = [cand("a", tags=["admin"], headers={"X-Trace": "trace-abc"}, body={"k": "SECRETBODY"}, method="POST")]
    fake, prov = setup([reply(item("a"))])
    prov.assess(pool, ctx())
    prompt = fake.calls[0]["prompt"]
    assert "POST /a/0" in prompt and "admin" in prompt
    assert "trace-abc" not in prompt and "SECRETBODY" not in prompt


def test_invalid_json_returns_empty_and_records_issue():
    _, prov = setup(["not json"])
    assert prov.assess(POOL, ctx()) == []
    assert prov.records[-1].issue_codes == ("invalid_json",)


def test_severity_field_is_rejected():
    _, prov = setup([reply(item("a", severity="high"), item("b"))])
    out = prov.assess(POOL[:2], ctx())
    assert [a.candidate_id for a in out] == ["b"]


def test_batch_over_limit_is_caller_error():
    _, prov = setup([])
    with pytest.raises(ValueError):
        prov.assess(POOL, ctx(size=2))


def test_prompt_drops_tail_then_raises_when_nothing_fits():
    full, _ = build_assessment_prompt(POOL, 10_000)
    two, _ = build_assessment_prompt(POOL[:2], 10_000)
    prompt, shown = build_assessment_prompt(POOL, len(two))
    assert [c.candidate_id for c in shown] == ["a", "b"] and len(prompt) <= len(two) < len(full)
    with pytest.raises(ContextLimitError):
        build_assessment_prompt(POOL, 10)


def test_planner_context_from_config():
    c = planner_context_from_config("r1", ModelConfig(ranking_batch_size=4))
    assert c.max_candidates_per_assessment == 4 and c.prompt_version == PROMPT_VERSION


def test_all_valid_no_fallback_and_input_order_kept():
    _, prov = setup([reply(item("c"), item("a"), item("b"))])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert [a.candidate_id for a in rep.assessments] == ["a", "b", "c"]
    assert rep.fallback_reasons == {}


def test_invalid_json_falls_back_for_all():
    _, prov = setup(["not json"])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert len(rep.assessments) == 3
    assert {a.provider_id for a in rep.assessments} == {"fallback-mock"}
    assert set(rep.fallback_reasons.values()) == {"missing_or_invalid_assessment"}


def test_partial_output_falls_back_only_for_missing():
    _, prov = setup([reply(item("a"))])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    ids = {a.candidate_id: a.provider_id for a in rep.assessments}
    assert ids["a"].startswith("llm:") and ids["b"] == ids["c"] == "fallback-mock"
    assert set(rep.fallback_reasons) == {"b", "c"}


def test_duplicate_items_for_one_candidate_fall_back():
    _, prov = setup([reply(item("a"), item("a"), item("b"), item("c"))])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert set(rep.fallback_reasons) == {"a"}


def test_timeout_then_retry_succeeds():
    fake, prov = setup([ProviderTimeout("t"), reply(item("a"), item("b"), item("c"))])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert rep.fallback_reasons == {} and len(fake.calls) == 2


def test_timeout_twice_falls_back_with_reason():
    fake, prov = setup([ProviderTimeout("t"), ProviderTimeout("t")])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert len(fake.calls) == 2
    assert set(rep.fallback_reasons.values()) == {"provider_raised:ProviderTimeout"}


def test_provider_error_is_not_retried():
    fake, prov = setup([ProviderError("down")])
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert len(fake.calls) == 1
    assert set(rep.fallback_reasons.values()) == {"provider_raised:ProviderError"}


def test_context_limit_falls_back():
    _, prov = setup([], max_prompt_chars=50)
    rep = assess_candidates_with_report(POOL, prov, ctx())
    assert set(rep.fallback_reasons.values()) == {"provider_raised:ContextLimitError"}


def test_batching_and_per_batch_fallback():
    fake, prov = setup([reply(item("a"), item("b")), "not json"])
    rep = assess_candidates_with_report(POOL, prov, ctx(size=2))
    assert len(fake.calls) == 2 and set(rep.fallback_reasons) == {"c"}


def test_assess_candidates_returns_list_and_validates_input():
    _, prov = setup([reply(item("a"), item("b"), item("c"))])
    assert len(assess_candidates(POOL, prov, ctx())) == 3
    with pytest.raises(ValueError):
        assess_candidates(POOL + [cand("a")], prov, ctx())
    with pytest.raises(ValueError):
        assess_candidates(POOL, prov, ctx(), max_retries=-1)