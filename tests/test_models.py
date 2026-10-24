"""Contract tests for adaptive_restler.models (docs/interfaces.md v1.1).

No network access or API keys are needed.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from adaptive_restler import models as m
from adaptive_restler.models import (
    BenchmarkConfig,
    BenchmarkReport,
    BudgetState,
    CandidateAssessment,
    CandidateCost,
    CandidateOutcomeSummary,
    CandidateRequest,
    CandidateSequence,
    ExecutionBudget,
    ExecutionResult,
    ExecutionStatus,
    ExperimentContext,
    Finding,
    FindingStatus,
    PlannerContext,
    PlannerHistory,
    RankedCandidate,
    ScoringConfig,
    TargetConfig,
    ValidationContext,
)

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- factories


def make_request(**over):
    data = {"method": "get", "path": "/users/1"}
    data.update(over)
    return CandidateRequest(**data)


def make_sequence(**over):
    data = {
        "candidate_id": "cand-001",
        "sequence": [make_request()],
        "source": "imported",
        "estimated_request_count": 1,
    }
    data.update(over)
    return CandidateSequence(**data)


def make_assessment(**over):
    data = {
        "candidate_id": "cand-001",
        "security_hypotheses": ["IDOR on /users/{id}"],
        "rationale": "Path parameter looks user-controlled.",
        "predicted_discovery_score": 0.6,
        "expected_impact_score": 0.7,
        "confidence": 0.5,
        "provider_id": "mock",
        "prompt_version": "v1",
    }
    data.update(over)
    return CandidateAssessment(**data)


def make_cost(**over):
    data = {
        "candidate_id": "cand-001",
        "estimated_request_count": 3,
        "estimated_execution_seconds": 1.5,
        "estimated_planner_seconds": 0.2,
    }
    data.update(over)
    return CandidateCost(**data)


def make_result(**over):
    data = {
        "run_id": "run-1",
        "candidate_id": "cand-001",
        "status": "completed",
        "started_at": NOW,
        "duration_seconds": 1.0,
        "request_count": 2,
        "budget_exhausted": False,
    }
    data.update(over)
    return ExecutionResult(**data)


def make_finding(**over):
    data = {
        "finding_id": "f-1",
        "run_id": "run-1",
        "candidate_id": "cand-001",
        "category": "broken_access_control",
        "status": "lead",
        "validation_notes": "Needs review.",
    }
    data.update(over)
    return Finding(**data)


def make_budget(**over):
    data = {"max_requests": 100, "max_runtime_seconds": 300.0, "request_timeout_seconds": 10.0}
    data.update(over)
    return ExecutionBudget(**data)


def make_target(**over):
    data = {"base_url": "http://localhost:8888", "allowed_hosts": ["localhost"]}
    data.update(over)
    return TargetConfig(**data)


def make_scoring(**over):
    data = {
        "request_budget": 100.0,
        "execution_time_budget_seconds": 300.0,
        "planner_latency_budget_seconds": 60.0,
    }
    data.update(over)
    return ScoringConfig(**data)


def make_all_models():
    ctx = ExperimentContext(
        run_id="run-1",
        target_config=make_target(),
        execution_budget=make_budget(),
        artifact_dir="artifacts/run-1",
        benchmark_id="bench-1",
        configuration_version="cfg-1",
    )
    return [
        make_request(),
        make_sequence(),
        make_assessment(),
        make_cost(),
        make_result(responses=[{"status": 200}], evidence_paths=["run-1/e.json"]),
        make_finding(status="confirmed", severity="high", impact_score=20,
                     evidence_paths=["run-1/e.json"], fingerprint="fp-1"),
        make_budget(),
        make_target(),
        ctx,
        RankedCandidate(candidate_id="cand-001", expected_value=0.2, normalized_cost=0.1,
                        priority_score=1.9, rank=1, reasons=["high value, low cost"]),
        PlannerContext(run_id="run-1", prompt_version="v1",
                       max_candidates_per_assessment=10, assessment_timeout_seconds=30.0),
        make_scoring(inference_cost_budget=5.0),
        CandidateOutcomeSummary(execution_attempts=2, successful_attempts=1, failed_attempts=1,
                                confirmed_finding_fingerprints=["fp-b", "fp-a"],
                                last_status="failed"),
        PlannerHistory(executed_candidate_ids={"b", "a"},
                       outcomes_by_candidate={"b": CandidateOutcomeSummary(),
                                              "a": CandidateOutcomeSummary()},
                       confirmed_finding_fingerprints={"fp-2", "fp-1"},
                       policy_version="p1"),
        BudgetState(run_id="run-1", started_at=NOW),
        ValidationContext(run_id="run-1", artifact_dir="artifacts/run-1", benchmark_id="bench-1",
                          expected_authorization_policy="owner-only",
                          test_identity_refs=["user-a", "user-b"]),
        BenchmarkConfig(benchmark_id="bench-1", strategy="random", target_config=make_target(),
                        execution_budget=make_budget(), repetitions=5,
                        artifact_root="artifacts", reset_strategy_id="reset-1"),
        BenchmarkReport(benchmark_id="bench-1", run_ids=["run-1"], strategy="random",
                        metrics={"findings": 3, "rate": 0.5, "unit": "per_100_requests", "x": None},
                        errors=[], configuration_reference="cfg-1"),
    ]


# ------------------------------------------------------ cross-cutting checks


@pytest.mark.parametrize("model", make_all_models(), ids=lambda x: type(x).__name__)
def test_json_round_trip(model):
    payload = model.model_dump_json()
    restored = type(model).model_validate_json(payload)
    assert restored == model
    assert restored.model_dump_json() == payload  # serialization is stable


@pytest.mark.parametrize("model", make_all_models(), ids=lambda x: type(x).__name__)
def test_unknown_fields_rejected(model):
    data = json.loads(model.model_dump_json())
    data["surprise"] = 1
    with pytest.raises(ValidationError):
        type(model).model_validate(data)


def test_no_duplicate_model_definitions_in_module():
    names = [n for n in dir(m) if n in m.__all__]
    assert sorted(names) == sorted(m.__all__)
    assert len(m.__all__) == len(set(m.__all__))


# ------------------------------------------------------------ CandidateRequest


def test_method_is_uppercased():
    assert make_request(method=" post ").method == "POST"


@pytest.mark.parametrize("method", ["", "G3T", "GET POST"])
def test_bad_method_rejected(method):
    with pytest.raises(ValidationError):
        make_request(method=method)


@pytest.mark.parametrize(
    "path",
    ["http://evil.example/x", "//evil.example/x", "users/1", "", "/a b", "/a\\b", "/x://y"],
)
def test_path_must_be_relative_without_host(path):
    with pytest.raises(ValidationError):
        make_request(path=path)


@pytest.mark.parametrize("header", ["Authorization", "cookie", "X-API-Key", "X-Auth-Token"])
def test_credential_headers_rejected(header):
    with pytest.raises(ValidationError):
        make_request(headers={header: "x"})


def test_harmless_headers_allowed():
    assert make_request(headers={"Accept": "application/json"}).headers


@pytest.mark.parametrize("status", [99, 600, True, 200.0])
def test_expected_status_validated(status):
    with pytest.raises(ValidationError):
        make_request(expected_statuses=[status])


# ----------------------------------------------------------- CandidateSequence


def test_sequence_must_be_non_empty_and_keeps_order():
    with pytest.raises(ValidationError):
        make_sequence(sequence=[])
    seq = make_sequence(sequence=[make_request(path="/a"), make_request(path="/b")])
    assert [r.path for r in seq.sequence] == ["/a", "/b"]


@pytest.mark.parametrize("count", [0, -1, 1.5, True])
def test_estimated_request_count_at_least_one(count):
    with pytest.raises(ValidationError):
        make_sequence(estimated_request_count=count)


@pytest.mark.parametrize("bad_id", ["", " x", "x ", "a\nb"])
def test_ids_must_be_clean(bad_id):
    with pytest.raises(ValidationError):
        make_sequence(candidate_id=bad_id)


@pytest.mark.parametrize("ref", ["Bearer abc", "user:pass", "a b", ""])
def test_auth_profile_must_be_reference_shaped(ref):
    with pytest.raises(ValidationError):
        make_sequence(auth_profile_id=ref)


def test_source_must_be_known():
    with pytest.raises(ValidationError):
        make_sequence(source="llm")


# ------------------------------------------------------- CandidateAssessment


@pytest.mark.parametrize("field", ["predicted_discovery_score", "expected_impact_score", "confidence"])
@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf"), "0.5", True])
def test_assessment_scores_out_of_range_rejected(field, value):
    with pytest.raises(ValidationError):
        make_assessment(**{field: value})


@pytest.mark.parametrize("value", [0, 0.0, 1, 1.0, 0.5])
def test_assessment_score_boundaries_accepted(value):
    assert make_assessment(predicted_discovery_score=value).predicted_discovery_score == value


def test_assessment_requires_all_fields_and_allows_empty_hypotheses():
    assert make_assessment(security_hypotheses=[]).security_hypotheses == []
    with pytest.raises(ValidationError):
        CandidateAssessment(candidate_id="cand-001")


def test_assessment_malformed_json_rejected():
    with pytest.raises(ValidationError):
        CandidateAssessment.model_validate_json("{not json")
    with pytest.raises(ValidationError):
        CandidateAssessment.model_validate_json('{"candidate_id": "c"}')


def test_assessment_has_no_severity_or_confirmed_field():
    with pytest.raises(ValidationError):
        make_assessment(severity="high")
    with pytest.raises(ValidationError):
        make_assessment(confirmed=True)


# ------------------------------------------------------------ CandidateCost


def test_cost_inference_none_is_kept_not_zero():
    assert make_cost().estimated_inference_cost is None
    assert make_cost(estimated_inference_cost=0.0).estimated_inference_cost == 0.0


@pytest.mark.parametrize(
    "field,value",
    [
        ("estimated_request_count", 0),
        ("estimated_execution_seconds", -1.0),
        ("estimated_planner_seconds", float("nan")),
        ("estimated_inference_cost", -0.1),
        ("estimated_inference_cost", float("inf")),
    ],
)
def test_cost_invalid_values_rejected(field, value):
    with pytest.raises(ValidationError):
        make_cost(**{field: value})


# --------------------------------------------------------- ExecutionStatus etc.


def test_enum_values_match_contract():
    assert [s.value for s in ExecutionStatus] == [
        "completed", "failed", "timed_out", "budget_exhausted", "skipped"
    ]
    assert [s.value for s in FindingStatus] == ["lead", "needs_review", "confirmed", "rejected"]


# ------------------------------------------------------------ ExecutionResult


def test_naive_datetime_rejected_and_other_zone_normalized_to_utc():
    with pytest.raises(ValidationError):
        make_result(started_at=datetime(2026, 1, 1, 12, 0))
    ist = timezone(timedelta(hours=5, minutes=30))
    result = make_result(started_at=datetime(2026, 1, 1, 17, 30, tzinfo=ist))
    assert result.started_at == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert result.started_at.utcoffset() == timedelta(0)


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", "C:/x", "a\\b", ""])
def test_evidence_paths_must_stay_relative(path):
    with pytest.raises(ValidationError):
        make_result(evidence_paths=[path])
    with pytest.raises(ValidationError):
        make_finding(evidence_paths=[path])


def test_relative_evidence_path_accepted():
    assert make_result(evidence_paths=["run-1/req_0.json"]).evidence_paths == ["run-1/req_0.json"]


@pytest.mark.parametrize("field,value", [("duration_seconds", -1.0), ("request_count", -1),
                                         ("duration_seconds", float("nan"))])
def test_result_numbers_validated(field, value):
    with pytest.raises(ValidationError):
        make_result(**{field: value})


def test_zero_requests_allowed_and_status_parsed_from_string():
    result = make_result(status="budget_exhausted", request_count=0, budget_exhausted=True)
    assert result.status is ExecutionStatus.BUDGET_EXHAUSTED


# -------------------------------------------------------------------- Finding


def test_lead_needs_no_score_severity_or_evidence():
    f = make_finding()
    assert f.impact_score is None and f.severity is None and f.fingerprint is None


def test_http_error_alone_cannot_confirm():
    with pytest.raises(ValidationError):
        make_finding(status="confirmed")  # no evidence


def test_confirmed_with_evidence_ok():
    f = make_finding(status="confirmed", evidence_paths=["run-1/e.json"])
    assert f.status is FindingStatus.CONFIRMED


def test_severity_requires_evidence():
    with pytest.raises(ValidationError):
        make_finding(severity="high")


@pytest.mark.parametrize("score", [-1, 29, 1.5, True])
def test_impact_score_range(score):
    with pytest.raises(ValidationError):
        make_finding(impact_score=score)


@pytest.mark.parametrize("score", [0, 28])
def test_impact_score_boundaries(score):
    assert make_finding(impact_score=score).impact_score == score


def test_severity_values_restricted():
    with pytest.raises(ValidationError):
        make_finding(severity="catastrophic", evidence_paths=["e.json"])


def test_status_assignment_is_revalidated():
    f = make_finding()
    with pytest.raises(ValidationError):
        f.status = "confirmed"  # still no evidence


# --------------------------------------------------------- budget and target


@pytest.mark.parametrize("field,value", [("max_requests", 0), ("max_runtime_seconds", 0.0),
                                         ("request_timeout_seconds", -1.0),
                                         ("max_runtime_seconds", float("inf"))])
def test_execution_budget_positive(field, value):
    with pytest.raises(ValidationError):
        make_budget(**{field: value})


def test_target_defaults_are_safe():
    t = make_target()
    assert t.tls_verification_enabled is True
    assert t.dry_run is True


def test_target_hosts_normalized_sorted_unique():
    t = make_target(allowed_hosts=["LocalHost", "localhost", "127.0.0.1"])
    assert t.allowed_hosts == ["127.0.0.1", "localhost"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"base_url": "http://evil.example", "allowed_hosts": ["localhost"]},
        {"base_url": "ftp://localhost", "allowed_hosts": ["localhost"]},
        {"base_url": "localhost:8888", "allowed_hosts": ["localhost"]},
        {"base_url": "http://user:pw@localhost", "allowed_hosts": ["localhost"]},
        {"base_url": "http://localhost?x=1", "allowed_hosts": ["localhost"]},
        {"base_url": "http://localhost:99999", "allowed_hosts": ["localhost"]},
        {"base_url": "http://localhost", "allowed_hosts": []},
        {"base_url": "http://localhost", "allowed_hosts": ["http://localhost"]},
        {"base_url": "http://localhost", "allowed_hosts": ["*.example.com"]},
        {"base_url": "http://localhost", "allowed_hosts": ["localhost"], "dry_run": "false"},
    ],
)
def test_invalid_targets_rejected(kwargs):
    with pytest.raises(ValidationError):
        TargetConfig(**kwargs)


def test_target_accepts_ipv6_and_base_path():
    t = TargetConfig(base_url="https://[::1]:8443/api", allowed_hosts=["::1"])
    assert t.allowed_hosts == ["::1"]


# -------------------------------------------------------------- RankedCandidate


@pytest.mark.parametrize("field,value", [("rank", 0), ("expected_value", -0.1),
                                         ("normalized_cost", float("nan")),
                                         ("priority_score", float("inf")), ("reasons", [])])
def test_ranked_candidate_validated(field, value):
    data = dict(candidate_id="c", expected_value=0.1, normalized_cost=0.1,
                priority_score=1.0, rank=1, reasons=["why"])
    data[field] = value
    with pytest.raises(ValidationError):
        RankedCandidate(**data)


# ---------------------------------------------------------- Planner* configs


@pytest.mark.parametrize("field,value", [("max_candidates_per_assessment", 0),
                                         ("assessment_timeout_seconds", 0.0),
                                         ("assessment_timeout_seconds", -1.0)])
def test_planner_context_limits_positive(field, value):
    data = dict(run_id="r", prompt_version="v1", max_candidates_per_assessment=5,
                assessment_timeout_seconds=30.0)
    data[field] = value
    with pytest.raises(ValidationError):
        PlannerContext(**data)


def test_scoring_defaults_match_docs():
    cfg = make_scoring()
    assert (cfg.request_weight, cfg.execution_time_weight,
            cfg.planner_latency_weight, cfg.inference_cost_weight) == (0.35, 0.35, 0.20, 0.10)
    assert cfg.epsilon == 1e-6
    assert cfg.inference_cost_budget is None


@pytest.mark.parametrize(
    "over",
    [
        {"request_weight": 0.5},                                   # sum != 1
        {"request_weight": -0.1, "execution_time_weight": 0.8},    # negative
        {"request_weight": float("nan")},
        {"epsilon": 0.0},
        {"epsilon": -1e-6},
        {"request_budget": 0.0},
        {"execution_time_budget_seconds": -1.0},
        {"planner_latency_budget_seconds": float("inf")},
        {"inference_cost_budget": 0.0},
    ],
)
def test_invalid_scoring_config_rejected(over):
    with pytest.raises(ValidationError):
        make_scoring(**over)


def test_weight_sum_tolerance():
    make_scoring(request_weight=0.35 + 1e-9)  # within tolerance
    with pytest.raises(ValidationError):
        make_scoring(request_weight=0.35 + 1e-3)


def test_scoring_budgets_are_required():
    with pytest.raises(ValidationError):
        ScoringConfig()


# ------------------------------------------------------------ History models


def test_outcome_summary_sorts_dedupes_and_checks_counts():
    s = CandidateOutcomeSummary(execution_attempts=3, successful_attempts=1,
                                confirmed_finding_fingerprints=["b", "a", "b"])
    assert s.confirmed_finding_fingerprints == ["a", "b"]
    with pytest.raises(ValidationError):
        CandidateOutcomeSummary(execution_attempts=1, successful_attempts=1, failed_attempts=1)


def test_outcome_summary_distinguishes_never_executed():
    s = CandidateOutcomeSummary()
    assert (s.execution_attempts, s.failed_attempts, s.last_status) == (0, 0, None)


def test_skipped_attempt_is_not_a_failure():
    s = CandidateOutcomeSummary(execution_attempts=1, last_status="skipped")
    assert s.failed_attempts == 0 and s.successful_attempts == 0


def test_history_serializes_in_stable_sorted_order():
    h1 = PlannerHistory(
        executed_candidate_ids={"c", "a", "b"},
        outcomes_by_candidate={"z": CandidateOutcomeSummary(), "a": CandidateOutcomeSummary()},
        confirmed_finding_fingerprints={"y", "x"},
        policy_version="p1",
    )
    h2 = PlannerHistory(
        executed_candidate_ids={"b", "c", "a"},
        outcomes_by_candidate={"a": CandidateOutcomeSummary(), "z": CandidateOutcomeSummary()},
        confirmed_finding_fingerprints={"x", "y"},
        policy_version="p1",
    )
    data = json.loads(h1.model_dump_json())
    assert data["executed_candidate_ids"] == ["a", "b", "c"]
    assert data["confirmed_finding_fingerprints"] == ["x", "y"]
    assert list(data["outcomes_by_candidate"]) == ["a", "z"]
    assert h1.model_dump_json() == h2.model_dump_json()


def test_history_requires_policy_version():
    with pytest.raises(ValidationError):
        PlannerHistory()


# ---------------------------------------------------------------- BudgetState


def test_budget_state_rejects_negative_even_on_assignment():
    state = BudgetState(run_id="r", started_at=NOW)
    state.requests_used = 5
    assert state.requests_used == 5
    with pytest.raises(ValidationError):
        state.requests_used = -1
    with pytest.raises(ValidationError):
        BudgetState(run_id="r", started_at=NOW, requests_used=-1)


# ------------------------------------------------- Validation and benchmarking


@pytest.mark.parametrize("ref", ["password=abc", "Bearer tok", "a:b", ""])
def test_identity_refs_are_reference_shaped(ref):
    with pytest.raises(ValidationError):
        ValidationContext(run_id="r", artifact_dir="a", benchmark_id="b",
                          expected_authorization_policy="p", test_identity_refs=[ref])


@pytest.mark.parametrize("reps", [0, -1, 2.5])
def test_benchmark_repetitions_positive(reps):
    with pytest.raises(ValidationError):
        BenchmarkConfig(benchmark_id="b", strategy="random", target_config=make_target(),
                        execution_budget=make_budget(), repetitions=reps,
                        artifact_root="artifacts", reset_strategy_id="r")


def test_benchmark_strategy_restricted():
    with pytest.raises(ValidationError):
        BenchmarkConfig(benchmark_id="b", strategy="magic", target_config=make_target(),
                        execution_budget=make_budget(), repetitions=1,
                        artifact_root="artifacts", reset_strategy_id="r")


def test_report_metrics_must_be_finite():
    with pytest.raises(ValidationError):
        BenchmarkReport(benchmark_id="b", run_ids=[], strategy="random",
                        metrics={"x": float("nan")}, errors=[], configuration_reference="c")
