from datetime import datetime, timezone

import pytest

from adaptive_restler.models import (
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
    ValidationContext,
)
from adaptive_restler.validation.findings import (
    make_fingerprint,
    normalize_path_template,
    validate_findings,
)

CTX = ValidationContext(
    run_id="run1",
    artifact_dir="artifacts",
    benchmark_id="bench1",
    expected_authorization_policy="owner-only",
)


def result(responses=(), status=ExecutionStatus.COMPLETED, run_id="run1"):
    return ExecutionResult(
        run_id=run_id,
        candidate_id="c1",
        status=status,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=1.0,
        request_count=len(responses),
        responses=list(responses),
        evidence_paths=["run1/c1/execution.json"],
        budget_exhausted=status is ExecutionStatus.BUDGET_EXHAUSTED,
    )


def resp(code, step=0, method="GET", path="/items/1"):
    return {"step": step, "method": method, "path": path, "status_code": code}


def test_5xx_is_lead_never_confirmed():
    findings = validate_findings(result([resp(500)]), CTX)
    assert len(findings) == 1
    f = findings[0]
    assert f.status is FindingStatus.LEAD
    assert f.severity is None and f.impact_score is None
    assert f.evidence_paths == ["run1/c1/execution.json"]
    assert f.fingerprint.startswith("fp-")


@pytest.mark.parametrize("code", [200, 201, 301, 400, 401, 403, 404])
def test_non_5xx_statuses_alone_produce_nothing(code):
    assert validate_findings(result([resp(code)]), CTX) == []


def test_timeout_is_lead():
    findings = validate_findings(result([resp(200)], status=ExecutionStatus.TIMED_OUT), CTX)
    assert [f.category for f in findings] == ["timeout"]
    assert findings[0].status is FindingStatus.LEAD
    assert findings[0].fingerprint is None


def test_failed_run_produces_no_finding():
    assert validate_findings(result([], status=ExecutionStatus.FAILED), CTX) == []


def test_budget_exhausted_still_analyzes_collected_responses():
    r = result([resp(503)], status=ExecutionStatus.BUDGET_EXHAUSTED)
    assert len(validate_findings(r, CTX)) == 1


def test_same_template_deduplicated_within_result():
    r = result([resp(500, 0, path="/items/1"), resp(500, 1, path="/items/2")])
    findings = validate_findings(r, CTX)
    assert len(findings) == 1
    assert "[0, 1]" in findings[0].validation_notes


def test_different_templates_or_methods_are_distinct():
    r = result(
        [
            resp(500, 0, path="/items/1"),
            resp(500, 1, path="/users/1"),
            resp(500, 2, method="POST", path="/items/1"),
        ]
    )
    findings = validate_findings(r, CTX)
    assert len({f.fingerprint for f in findings}) == 3


def test_normalize_path_template():
    assert normalize_path_template("/items/123?x=1") == "/items/{id}"
    assert normalize_path_template("/u/550e8400-e29b-41d4-a716-446655440000") == "/u/{id}"
    assert normalize_path_template("/items/list") == "/items/list"


def test_fingerprint_stable_across_ids_and_method_case():
    a = make_fingerprint("server_error", "get", "/items/1")
    b = make_fingerprint("server_error", "GET", "/items/99")
    assert a == b
    assert a != make_fingerprint("server_error", "POST", "/items/1")


def test_deterministic_and_json_round_trip():
    r = result([resp(500), resp(502, 1, path="/x")])
    first = validate_findings(r, CTX)
    assert first == validate_findings(r, CTX)
    assert [f.finding_id for f in first] == sorted(f.finding_id for f in first)
    for f in first:
        assert Finding.model_validate_json(f.model_dump_json()) == f


def test_malformed_response_entries_ignored():
    r = result([{"status_code": True}, {"status_code": "500"}, {"method": "GET"}])
    assert validate_findings(r, CTX) == []


def test_run_id_mismatch_raises():
    with pytest.raises(ValueError):
        validate_findings(result([resp(500)], run_id="other"), CTX)