"""Benchmark metrics: pure functions over execution results and findings.

Rates are None (unknown) when the denominator is zero, never 0.
Every metric's unit and denominator is listed in METRIC_DEFINITIONS.
"""

from __future__ import annotations

from adaptive_restler.models import (
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
)

METRIC_DEFINITIONS: dict[str, str] = {
    "attempts_total": "count of ExecutionResult records (all statuses)",
    "attempts_completed": "count of results with status completed",
    "attempts_failed": "count of results with status failed",
    "attempts_timed_out": "count of results with status timed_out",
    "attempts_budget_exhausted": "count of results with status budget_exhausted",
    "attempts_skipped": "count of results with status skipped",
    "requests_total": "sum of measured request_count over all results (requests)",
    "duration_seconds_total": "sum of duration_seconds over all results (seconds)",
    "leads_unique": "distinct fingerprints among findings with status lead or needs_review",
    "confirmed_unique": "distinct fingerprints among findings with status confirmed",
    "rejected_unique": "distinct fingerprints among findings with status rejected",
    "confirmed_per_100_requests": "confirmed_unique / requests_total * 100; None if requests_total is 0",
    "leads_per_100_requests": "leads_unique / requests_total * 100; None if requests_total is 0",
    "requests_to_first_confirmed": (
        "cumulative requests, in result order, through the first result whose "
        "candidate has a confirmed finding; None if no confirmed finding"
    ),
}


def finding_key(finding: Finding) -> str:
    """Dedup key: the validator's fingerprint, else category + candidate_id."""
    if finding.fingerprint is not None:
        return finding.fingerprint
    return f"{finding.category}|{finding.candidate_id}"


def _unique(findings: list[Finding], statuses: set[FindingStatus]) -> int:
    return len({finding_key(f) for f in findings if f.status in statuses})


def _rate_per_100(numerator: int, requests_total: int) -> float | None:
    if requests_total <= 0:
        return None
    return round(numerator / requests_total * 100.0, 6)


def requests_to_first_confirmed(
    results: list[ExecutionResult], findings: list[Finding]
) -> int | None:
    confirmed_ids = {
        f.candidate_id for f in findings if f.status is FindingStatus.CONFIRMED
    }
    cumulative = 0
    for result in results:
        cumulative += result.request_count
        if result.candidate_id in confirmed_ids:
            return cumulative
    return None


def compute_metrics(
    results: list[ExecutionResult], findings: list[Finding]
) -> dict[str, float | int | str | None]:
    """Return metrics keyed as in METRIC_DEFINITIONS (JSON-safe values)."""
    run_ids = {r.run_id for r in results}
    for f in findings:
        if f.run_id not in run_ids:
            raise ValueError("finding.run_id has no matching execution result")

    def count(status: ExecutionStatus) -> int:
        return sum(1 for r in results if r.status is status)

    requests_total = sum(r.request_count for r in results)
    leads = _unique(findings, {FindingStatus.LEAD, FindingStatus.NEEDS_REVIEW})
    confirmed = _unique(findings, {FindingStatus.CONFIRMED})

    return {
        "attempts_total": len(results),
        "attempts_completed": count(ExecutionStatus.COMPLETED),
        "attempts_failed": count(ExecutionStatus.FAILED),
        "attempts_timed_out": count(ExecutionStatus.TIMED_OUT),
        "attempts_budget_exhausted": count(ExecutionStatus.BUDGET_EXHAUSTED),
        "attempts_skipped": count(ExecutionStatus.SKIPPED),
        "requests_total": requests_total,
        "duration_seconds_total": round(sum(r.duration_seconds for r in results), 6),
        "leads_unique": leads,
        "confirmed_unique": confirmed,
        "rejected_unique": _unique(findings, {FindingStatus.REJECTED}),
        "confirmed_per_100_requests": _rate_per_100(confirmed, requests_total),
        "leads_per_100_requests": _rate_per_100(leads, requests_total),
        "requests_to_first_confirmed": requests_to_first_confirmed(results, findings),
    }