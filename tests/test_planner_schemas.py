"""Tests for adaptive_restler.planner.schemas. No network or API key needed."""

import json

import pytest

from adaptive_restler.models import CandidateAssessment
from adaptive_restler.planner.schemas import (
    MAX_HYPOTHESES,
    MAX_RATIONALE_CHARS,
    parse_provider_response,
)

IDS = ["cand-a", "cand-b", "cand-c"]


def item(candidate_id="cand-a", **over):
    data = {
        "candidate_id": candidate_id,
        "security_hypotheses": ["Check IDOR on /users/{id}"],
        "rationale": "Path parameter looks user-controlled.",
        "predicted_discovery_score": 0.6,
        "expected_impact_score": 0.7,
        "confidence": 0.5,
        "missing_information": [],
    }
    data.update(over)
    return data


def payload(*items):
    return json.dumps({"assessments": list(items)})


def parse(raw, ids=IDS, **kw):
    return parse_provider_response(
        raw, expected_candidate_ids=ids, provider_id="test-llm", prompt_version="v1", **kw
    )


# ------------------------------------------------------------------- valid


def test_valid_response_parsed_in_expected_order():
    result = parse(payload(item("cand-c"), item("cand-a"), item("cand-b")))
    assert [a.candidate_id for a in result.assessments] == IDS
    assert result.missing_candidate_ids == []
    assert result.issues == []
    first = result.assessments[0]
    assert isinstance(first, CandidateAssessment)
    assert first.provider_id == "test-llm" and first.prompt_version == "v1"


def test_bytes_input_accepted():
    result = parse(payload(item()).encode("utf-8"), ids=["cand-a"])
    assert len(result.assessments) == 1


def test_whitespace_in_text_is_stripped_and_defaults_applied():
    data = item(rationale="  why  ")
    del data["missing_information"]
    result = parse(payload(data), ids=["cand-a"])
    assert result.assessments[0].rationale == "why"
    assert result.assessments[0].missing_information == []


def test_empty_hypotheses_allowed():
    result = parse(payload(item(security_hypotheses=[])), ids=["cand-a"])
    assert result.assessments[0].security_hypotheses == []


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0])
def test_score_boundaries_accepted(value):
    result = parse(payload(item(predicted_discovery_score=value)), ids=["cand-a"])
    assert result.assessments[0].predicted_discovery_score == value


def test_parsing_is_deterministic():
    raw = payload(item("cand-b"), item("cand-a", confidence=0.1))
    assert parse(raw) == parse(raw)


# ----------------------------------------------------------- score handling


@pytest.mark.parametrize("field", ["predicted_discovery_score", "expected_impact_score", "confidence"])
@pytest.mark.parametrize("bad", [-0.1, 1.1, 5, "0.5", None, True, [0.5]])
def test_bad_scores_reject_only_that_item(field, bad):
    result = parse(payload(item("cand-a", **{field: bad}), item("cand-b")))
    assert [a.candidate_id for a in result.assessments] == ["cand-b"]
    assert result.missing_candidate_ids == ["cand-a", "cand-c"]
    assert any(i.candidate_id == "cand-a" and i.code == "invalid_item" for i in result.issues)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_json_numbers_rejected(token):
    raw = payload(item("cand-a")).replace("0.6", token)
    result = parse(raw, ids=["cand-a"])
    assert result.assessments == []
    assert result.missing_candidate_ids == ["cand-a"]


def test_missing_required_field_rejected():
    data = item()
    del data["rationale"]
    result = parse(payload(data), ids=["cand-a"])
    assert result.assessments == [] and result.issues[0].code == "invalid_item"


# --------------------------------------------------- forbidden / bounded text


@pytest.mark.parametrize("extra", [{"severity": "high"}, {"confirmed": True}, {"provider_id": "x"},
                                   {"prompt_version": "v9"}, {"anything": 1}])
def test_unknown_fields_reject_item(extra):
    result = parse(payload(item(**extra)), ids=["cand-a"])
    assert result.assessments == []
    assert result.missing_candidate_ids == ["cand-a"]


def test_text_bounds_enforced():
    too_many = item(security_hypotheses=["h"] * (MAX_HYPOTHESES + 1))
    long_rationale = item(rationale="x" * (MAX_RATIONALE_CHARS + 1))
    blank_hypothesis = item(security_hypotheses=["   "])
    for bad in (too_many, long_rationale, blank_hypothesis):
        assert parse(payload(bad), ids=["cand-a"]).assessments == []


# --------------------------------------------------- candidate ID matching


def test_unknown_candidate_ids_ignored_and_reported():
    result = parse(payload(item("ghost"), item("cand-a")), ids=["cand-a"])
    assert [a.candidate_id for a in result.assessments] == ["cand-a"]
    assert any(i.code == "unknown_candidate_id" for i in result.issues)
    assert all("ghost" not in i.detail for i in result.issues)
    assert all(i.candidate_id != "ghost" for i in result.issues)


@pytest.mark.parametrize("bad_id", [None, 5, ["cand-a"], {"x": 1}])
def test_non_string_candidate_id_handled(bad_id):
    result = parse(payload(item(candidate_id=bad_id)), ids=["cand-a"])
    assert result.assessments == [] and result.missing_candidate_ids == ["cand-a"]


def test_duplicate_candidate_ids_are_ambiguous_and_dropped():
    result = parse(payload(item("cand-a"), item("cand-a", confidence=0.9), item("cand-b")))
    assert [a.candidate_id for a in result.assessments] == ["cand-b"]
    assert result.missing_candidate_ids == ["cand-a", "cand-c"]
    assert any(i.code == "duplicate_candidate_id" and i.candidate_id == "cand-a" for i in result.issues)


def test_duplicate_with_one_invalid_copy_still_dropped():
    result = parse(payload(item("cand-a"), item("cand-a", confidence=7)), ids=["cand-a"])
    assert result.assessments == []


def test_missing_assessments_listed_not_invented():
    result = parse(payload(item("cand-b")))
    assert result.missing_candidate_ids == ["cand-a", "cand-c"]


def test_empty_expected_ids():
    result = parse(payload(item()), ids=[])
    assert result.assessments == [] and result.missing_candidate_ids == []


def test_duplicate_expected_ids_is_a_caller_bug():
    with pytest.raises(ValueError):
        parse(payload(item()), ids=["cand-a", "cand-a"])


@pytest.mark.parametrize("kw", [{"provider_id": ""}, {"prompt_version": " v1"}])
def test_bad_provider_metadata_is_a_caller_bug(kw):
    args = {"provider_id": "p", "prompt_version": "v1", **kw}
    with pytest.raises(ValueError):
        parse_provider_response(payload(item()), expected_candidate_ids=["cand-a"], **args)


# ------------------------------------------------------ malformed responses


@pytest.mark.parametrize(
    "raw,code",
    [
        ("not json", "invalid_json"),
        ("", "invalid_json"),
        ('{"assessments": [', "invalid_json"),
        (b"\xff\xfe\x00bad", "invalid_json"),
        ("[" * 100_000, "invalid_json"),
        ("[]", "invalid_structure"),
        ('"text"', "invalid_structure"),
        ("{}", "invalid_structure"),
        ('{"assessments": {}}', "invalid_structure"),
        ('{"assessments": "none"}', "invalid_structure"),
    ],
)
def test_malformed_responses_reported_never_raised(raw, code):
    result = parse(raw)
    assert result.assessments == []
    assert result.missing_candidate_ids == IDS
    assert [i.code for i in result.issues] == [code]


def test_non_object_items_reported():
    result = parse(json.dumps({"assessments": [1, "x", None, item("cand-a")]}), ids=["cand-a"])
    assert len(result.assessments) == 1
    assert [i.code for i in result.issues].count("invalid_item") == 3


def test_oversized_response_rejected_before_parsing():
    raw = payload(item())
    result = parse(raw, max_response_chars=len(raw) - 1)
    assert result.assessments == []
    assert result.issues[0].code == "response_too_large"
    assert parse(raw, max_response_chars=len(raw)).assessments  # boundary is inclusive


# ------------------------------------------------------------- sanitization


def test_issue_details_never_echo_provider_text():
    secret = "SECRET-TOKEN-abc123"
    result = parse(
        payload(
            item("cand-a", rationale=secret, predicted_discovery_score=5),
            item("cand-b", **{secret: secret}),
        ),
        ids=["cand-a", "cand-b"],
    )
    assert result.assessments == []
    blob = " ".join(f"{i.candidate_id} {i.code} {i.detail}" for i in result.issues)
    assert secret not in blob


def test_issue_details_are_bounded_and_printable():
    weird = "x\n\x00" * 500
    result = parse(payload(item("cand-a", **{weird: 1})), ids=["cand-a"])
    for i in result.issues:
        assert len(i.detail) <= 300
        assert i.detail.isprintable()
