"""Tests for adaptive_restler.planner.provider. No network or API key needed."""

import json
import socket

import pytest

from adaptive_restler.models import CandidateAssessment, CandidateRequest, CandidateSequence, PlannerContext
from adaptive_restler.planner.provider import AssessmentProvider, MockAssessmentProvider
from adaptive_restler.planner.schemas import parse_provider_response


def ctx(**over):
    data = {"run_id": "run-1", "prompt_version": "v1", "max_candidates_per_assessment": 10,
            "assessment_timeout_seconds": 30.0}
    data.update(over)
    return PlannerContext(**data)


def cand(candidate_id="c1", requests=None, tags=None):
    requests = requests or [("GET", "/health")]
    return CandidateSequence(
        candidate_id=candidate_id,
        sequence=[CandidateRequest(method=m, path=p) for m, p in requests],
        source="imported",
        tags=tags or [],
        estimated_request_count=len(requests),
    )


PLAIN = cand("plain")
RISKY = cand("risky", [("POST", "/users"), ("DELETE", "/users/{id}")], ["admin", "user"])


def test_satisfies_provider_protocol():
    assert isinstance(MockAssessmentProvider(), AssessmentProvider)


def test_one_assessment_per_candidate_in_input_order():
    out = MockAssessmentProvider().assess([RISKY, PLAIN], ctx())
    assert [a.candidate_id for a in out] == ["risky", "plain"]
    assert all(isinstance(a, CandidateAssessment) for a in out)


def test_metadata_attached():
    a = MockAssessmentProvider().assess([PLAIN], ctx(prompt_version="v7"))[0]
    assert a.provider_id == "mock" and a.prompt_version == "v7"
    assert MockAssessmentProvider("other").assess([PLAIN], ctx())[0].provider_id == "other"


def test_empty_batch_returns_empty_list():
    assert MockAssessmentProvider().assess([], ctx()) == []


def test_deterministic_across_calls_and_instances():
    batch = [RISKY, PLAIN]
    first = MockAssessmentProvider().assess(batch, ctx())
    assert first == MockAssessmentProvider().assess(batch, ctx())
    assert first == MockAssessmentProvider().assess(batch, ctx())


def test_assessment_independent_of_batch_composition_and_order():
    alone = MockAssessmentProvider().assess([RISKY], ctx())[0]
    in_batch = MockAssessmentProvider().assess([PLAIN, RISKY], ctx())[1]
    reversed_batch = MockAssessmentProvider().assess([RISKY, PLAIN], ctx())[0]
    assert alone == in_batch == reversed_batch


def test_risky_candidate_scores_higher_than_plain():
    plain, risky = MockAssessmentProvider().assess([PLAIN, RISKY], ctx())
    assert risky.predicted_discovery_score > plain.predicted_discovery_score
    assert risky.expected_impact_score > plain.expected_impact_score
    assert risky.security_hypotheses and not plain.security_hypotheses


def test_scores_always_in_range_even_with_every_signal():
    everything = cand("max", [("POST", "/a/1"), ("DELETE", "/b/{id}")],
                      ["admin", "auth", "delete", "payment", "upload", "user"])
    a = MockAssessmentProvider().assess([everything], ctx())[0]
    for score in (a.predicted_discovery_score, a.expected_impact_score, a.confidence):
        assert 0.0 <= score <= 1.0


def test_hypotheses_are_hedged_not_confirmed_claims():
    a = MockAssessmentProvider().assess([RISKY], ctx())[0]
    text = " ".join(a.security_hypotheses).lower()
    assert "may" in text
    assert "confirmed" not in text and "vulnerable" not in text


def test_tags_are_normalized_and_irrelevant_tags_ignored():
    shouty = MockAssessmentProvider().assess([cand("t", tags=["  ADMIN "])], ctx())[0]
    boring = MockAssessmentProvider().assess([cand("t", tags=["colour"])], ctx())[0]
    assert shouty.predicted_discovery_score > boring.predicted_discovery_score


def test_batch_larger_than_context_limit_rejected():
    with pytest.raises(ValueError):
        MockAssessmentProvider().assess([PLAIN, RISKY], ctx(max_candidates_per_assessment=1))
    MockAssessmentProvider().assess([PLAIN], ctx(max_candidates_per_assessment=1))


def test_duplicate_candidate_ids_rejected():
    with pytest.raises(ValueError):
        MockAssessmentProvider().assess([PLAIN, cand("plain")], ctx())


def test_does_not_mutate_inputs():
    batch = [RISKY, PLAIN]
    before = [c.model_dump_json() for c in batch]
    MockAssessmentProvider().assess(batch, ctx())
    assert [c.model_dump_json() for c in batch] == before


def test_works_with_networking_disabled(monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    assert MockAssessmentProvider().assess([RISKY, PLAIN], ctx())


def test_mock_output_survives_a_trip_through_the_provider_json_schema():
    """What a real provider would send must parse back to the same assessments."""
    batch = [RISKY, PLAIN]
    assessments = MockAssessmentProvider().assess(batch, ctx())
    items = [a.model_dump(exclude={"provider_id", "prompt_version"}) for a in assessments]
    result = parse_provider_response(
        json.dumps({"assessments": items}),
        expected_candidate_ids=[c.candidate_id for c in batch],
        provider_id="mock",
        prompt_version="v1",
    )
    assert result.issues == []
    assert result.assessments == assessments
