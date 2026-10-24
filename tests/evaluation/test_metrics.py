from datetime import datetime, timezone

import pytest

from adaptive_restler.evaluation.metrics import (
    METRIC_DEFINITIONS,
    compute_metrics,
    finding_key,
)
from adaptive_restler.models import (
    BenchmarkReport,
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
)


def res(cid, requests=2, status=ExecutionStatus.COMPLETED, run_id="run1", duration=1.0):
    return ExecutionResult(
        run_id=run_id,
        candidate_id=cid,
        status=status,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=duration,
        request_count=requests,
        budget_exhausted=status is ExecutionStatus.BUDGET_EXHAUSTED,
    )


def fnd(cid, status=FindingStatus.LEAD, fp="fp-a", category="server_error", fid=None):
    return Finding(
        finding_id=fid or f"f-{cid}-{fp}-{status.value}",
        run_id="run1",
        candidate_id=cid,
        category=category,
        status=status,
        evidence_paths=["run1/x/execution.json"],
        validation_notes="n",
        fingerprint=fp,
    )


def test_empty_inputs_give_unknown_rates_not_zero():
    m = compute_metrics([], [])
    assert m["attempts_total"] == 0 and m["requests_total"] == 0
    assert m["confirmed_per_100_requests"] is None
    assert m["leads_per_100_requests"] is None
    assert m["requests_to_first_confirmed"] is None


def test_status_counts_and_totals():
    results = [
        res("a", 3),
        res("b", 1, ExecutionStatus.FAILED),
        res("c", 2, ExecutionStatus.TIMED_OUT),
        res("d", 0, ExecutionStatus.SKIPPED),
        res("e", 4, ExecutionStatus.BUDGET_EXHAUSTED),
    ]
    m = compute_metrics(results, [])
    assert m["attempts_total"] == 5
    assert m["attempts_completed"] == m["attempts_failed"] == 1
    assert m["attempts_timed_out"] == m["attempts_skipped"] == 1
    assert m["attempts_budget_exhausted"] == 1
    assert m["requests_total"] == 10
    assert m["duration_seconds_total"] == 5.0


def test_duplicate_findings_counted_once():
    results = [res("a", 5), res("b", 5)]
    findings = [fnd("a", fp="fp-x"), fnd("b", fp="fp-x"), fnd("b", fp="fp-y")]
    m = compute_metrics(results, findings)
    assert m["leads_unique"] == 2
    assert m["leads_per_100_requests"] == 20.0


def test_confirmed_separate_from_leads():
    results = [res("a", 10)]
    findings = [fnd("a", FindingStatus.CONFIRMED, fp="fp-c"), fnd("a", fp="fp-l")]
    m = compute_metrics(results, findings)
    assert m["confirmed_unique"] == 1
    assert m["leads_unique"] == 1
    assert m["confirmed_per_100_requests"] == 10.0


def test_missing_fingerprint_falls_back_to_category_and_candidate():
    f = fnd("a", fp=None, category="timeout")
    assert finding_key(f) == "timeout|a"
    m = compute_metrics([res("a"), res("b")], [f, fnd("b", fp=None, category="timeout")])
    assert m["leads_unique"] == 2


def test_requests_to_first_confirmed_is_cumulative():
    results = [res("a", 3), res("b", 4), res("c", 5)]
    findings = [fnd("b", FindingStatus.CONFIRMED), fnd("c", FindingStatus.CONFIRMED, fp="fp-z")]
    assert compute_metrics(results, findings)["requests_to_first_confirmed"] == 7


def test_unmatched_finding_run_raises():
    bad = fnd("a").model_copy(update={"run_id": "other"})
    with pytest.raises(ValueError):
        compute_metrics([res("a")], [bad])


def test_keys_match_definitions_and_fit_report_model():
    m = compute_metrics([res("a")], [fnd("a")])
    assert set(m) == set(METRIC_DEFINITIONS)
    report = BenchmarkReport(
        benchmark_id="b",
        run_ids=["run1"],
        strategy="random",
        metrics=m,
        errors=[],
        configuration_reference="cfg",
    )
    assert BenchmarkReport.model_validate_json(report.model_dump_json()) == report