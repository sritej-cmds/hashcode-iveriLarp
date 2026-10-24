"""Report generation from persisted artifacts (contract 11.8).

Reports are built by reloading <artifact_root>/<run_id>/results.json and
findings.json, not from in-memory state. Runs that cannot be loaded are listed
in `errors` and counted, never silently dropped.

Aggregation across runs: counts are summed (uniqueness is per run, so a
fingerprint seen in two runs counts twice), rates are recomputed from the
sums, and requests_to_first_confirmed is the mean over runs that have one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from adaptive_restler.evaluation.metrics import METRIC_DEFINITIONS, compute_metrics
from adaptive_restler.models import (
    BenchmarkConfig,
    BenchmarkReport,
    ExecutionResult,
    Finding,
)
from adaptive_restler.runner.artifacts import artifact_dir, write_json_artifact

REPORT_DEFINITIONS: dict[str, str] = {
    **METRIC_DEFINITIONS,
    "requests_to_first_confirmed": (
        "mean over runs that have a confirmed finding of cumulative requests "
        "through the first result whose candidate has one; None if no run does"
    ),
    "runs_total": "count of run IDs requested",
    "runs_loaded": "count of runs whose persisted artifacts loaded and validated",
    "runs_unloadable": "count of runs that failed to load (see errors)",
}

_SUMMED = (
    "attempts_total",
    "attempts_completed",
    "attempts_failed",
    "attempts_timed_out",
    "attempts_budget_exhausted",
    "attempts_skipped",
    "requests_total",
    "leads_unique",
    "confirmed_unique",
    "rejected_unique",
)


def persist_run(
    root: str | Path,
    run_id: str,
    results: list[ExecutionResult],
    findings: list[Finding],
) -> None:
    """Write redacted results.json and findings.json for one run."""
    write_json_artifact(root, run_id, None, "results", [r.model_dump(mode="json") for r in results])
    write_json_artifact(root, run_id, None, "findings", [f.model_dump(mode="json") for f in findings])


def load_run(root: str | Path, run_id: str) -> tuple[list[ExecutionResult], list[Finding]]:
    directory = artifact_dir(root, run_id)
    raw_results = json.loads((directory / "results.json").read_text(encoding="utf-8"))
    raw_findings = json.loads((directory / "findings.json").read_text(encoding="utf-8"))
    return (
        [ExecutionResult.model_validate(x) for x in raw_results],
        [Finding.model_validate(x) for x in raw_findings],
    )


def _rate(numerator: int, requests_total: int) -> float | None:
    if requests_total <= 0:
        return None
    return round(numerator / requests_total * 100.0, 6)


def _aggregate(per_run: list[dict]) -> dict:
    totals: dict = {key: sum(m[key] for m in per_run) for key in _SUMMED}
    totals["duration_seconds_total"] = round(sum(m["duration_seconds_total"] for m in per_run), 6)
    totals["confirmed_per_100_requests"] = _rate(totals["confirmed_unique"], totals["requests_total"])
    totals["leads_per_100_requests"] = _rate(totals["leads_unique"], totals["requests_total"])
    firsts = [m["requests_to_first_confirmed"] for m in per_run if m["requests_to_first_confirmed"] is not None]
    totals["requests_to_first_confirmed"] = round(sum(firsts) / len(firsts), 6) if firsts else None
    return totals


def build_report(
    config: BenchmarkConfig,
    run_ids: list[str],
    extra_errors: list[str] | None = None,
) -> BenchmarkReport:
    per_run: list[dict] = []
    errors: list[str] = list(extra_errors or [])
    for run_id in run_ids:
        try:
            results, findings = load_run(config.artifact_root, run_id)
            per_run.append(compute_metrics(results, findings))
        except (OSError, ValueError, TypeError) as exc:
            errors.append(f"run {run_id}: could not load persisted artifacts ({type(exc).__name__})")

    metrics = _aggregate(per_run)
    metrics["runs_total"] = len(run_ids)
    metrics["runs_loaded"] = len(per_run)
    metrics["runs_unloadable"] = len(run_ids) - len(per_run)
    return BenchmarkReport(
        benchmark_id=config.benchmark_id,
        run_ids=list(run_ids),
        strategy=config.strategy,
        metrics=dict(sorted(metrics.items())),
        errors=errors,
        configuration_reference=f"{config.benchmark_id}/config.json",
    )


def render_markdown(report: BenchmarkReport) -> str:
    lines = [
        f"# Benchmark report: {report.benchmark_id}",
        "",
        f"- Strategy: {report.strategy}",
        f"- Runs: {', '.join(report.run_ids) or '(none)'}",
        f"- Configuration: {report.configuration_reference}",
        "",
        "| Metric | Value | Definition |",
        "|---|---|---|",
    ]
    for name, value in report.metrics.items():
        shown = "unknown" if value is None else str(value)
        lines.append(f"| {name} | {shown} | {REPORT_DEFINITIONS.get(name, '')} |")
    lines += ["", "## Errors", ""]
    lines += [f"- {e}" for e in report.errors] or ["None."]
    lines.append("")
    return "\n".join(lines)


def generate_report(config: BenchmarkConfig, run_ids: list[str]) -> BenchmarkReport:
    """Build and write report.json, report.md and config.json.

    A write failure never raises: raw run artifacts are untouched and the
    failure is recorded in `errors`.
    """
    report = build_report(config, run_ids)
    try:
        directory = artifact_dir(config.artifact_root, config.benchmark_id)
        write_json_artifact(
            config.artifact_root, config.benchmark_id, None, "config",
            config.model_dump(mode="json"),
        )
        write_json_artifact(
            config.artifact_root, config.benchmark_id, None, "report",
            report.model_dump(mode="json"),
        )
        tmp = directory / ".report.md.tmp"
        tmp.write_text(render_markdown(report), encoding="utf-8")
        os.replace(tmp, directory / "report.md")
    except (OSError, ValueError) as exc:
        report = report.model_copy(
            update={"errors": [*report.errors, f"report write failed ({type(exc).__name__})"]}
        )
    return report