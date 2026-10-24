from datetime import datetime, timezone

from adaptive_restler.evaluation.report import (
    build_report,
    generate_report,
    load_run,
    persist_run,
    render_markdown,
)
from adaptive_restler.models import (
    BenchmarkConfig,
    BenchmarkReport,
    ExecutionBudget,
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
    TargetConfig,
)


def config(root):
    return BenchmarkConfig(
        benchmark_id="bench1",
        strategy="random",
        target_config=TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"]),
        execution_budget=ExecutionBudget(
            max_requests=10, max_runtime_seconds=60.0, request_timeout_seconds=5.0
        ),
        repetitions=2,
        artifact_root=str(root),
        reset_strategy_id="none",
    )


def res(run_id, cid="c1", requests=5):
    return ExecutionResult(
        run_id=run_id,
        candidate_id=cid,
        status=ExecutionStatus.COMPLETED,
        started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=1.0,
        request_count=requests,
        budget_exhausted=False,
    )


def fnd(run_id, fp="fp-x", status=FindingStatus.LEAD):
    return Finding(
        finding_id=f"f-{run_id}-{fp}",
        run_id=run_id,
        candidate_id="c1",
        category="server_error",
        status=status,
        evidence_paths=[f"{run_id}/c1/execution.json"],
        validation_notes="n",
        fingerprint=fp,
    )


def test_persist_and_load_round_trip(tmp_path):
    r, f = [res("run1")], [fnd("run1")]
    persist_run(tmp_path, "run1", r, f)
    assert load_run(tmp_path, "run1") == (r, f)


def test_two_runs_aggregate(tmp_path):
    persist_run(tmp_path, "run1", [res("run1", requests=5)], [fnd("run1")])
    persist_run(tmp_path, "run2", [res("run2", requests=5)], [fnd("run2")])
    report = build_report(config(tmp_path), ["run1", "run2"])
    m = report.metrics
    assert m["requests_total"] == 10
    assert m["leads_unique"] == 2
    assert m["leads_per_100_requests"] == 20.0
    assert m["runs_total"] == 2 and m["runs_loaded"] == 2 and m["runs_unloadable"] == 0
    assert m["confirmed_per_100_requests"] == 0.0
    assert m["requests_to_first_confirmed"] is None
    assert report.errors == []


def test_missing_run_is_reported_not_dropped(tmp_path):
    persist_run(tmp_path, "run1", [res("run1")], [])
    report = build_report(config(tmp_path), ["run1", "run2"])
    assert report.run_ids == ["run1", "run2"]
    assert report.metrics["runs_unloadable"] == 1
    assert any("run2" in e for e in report.errors)


def test_corrupt_artifact_is_reported(tmp_path):
    persist_run(tmp_path, "run1", [res("run1")], [])
    (tmp_path / "run1" / "results.json").write_text("{not json", encoding="utf-8")
    report = build_report(config(tmp_path), ["run1"])
    assert report.metrics["runs_loaded"] == 0
    assert len(report.errors) == 1
    assert report.metrics["confirmed_per_100_requests"] is None


def test_generate_report_writes_files(tmp_path):
    persist_run(tmp_path, "run1", [res("run1")], [fnd("run1")])
    report = generate_report(config(tmp_path), ["run1"])
    base = tmp_path / "bench1"
    assert (base / "config.json").exists() and (base / "report.md").exists()
    loaded = BenchmarkReport.model_validate_json((base / "report.json").read_text(encoding="utf-8"))
    assert loaded == report
    text = (base / "report.md").read_text(encoding="utf-8")
    assert "leads_unique" in text and "distinct fingerprints" in text


def test_write_failure_is_recorded_and_raw_artifacts_survive(tmp_path):
    persist_run(tmp_path, "run1", [res("run1")], [])
    (tmp_path / "bench1").write_text("blocker", encoding="utf-8")
    report = generate_report(config(tmp_path), ["run1"])
    assert any("report write failed" in e for e in report.errors)
    assert (tmp_path / "run1" / "results.json").exists()


def test_markdown_shows_unknown_and_errors(tmp_path):
    report = build_report(config(tmp_path), ["missing"])
    md = render_markdown(report)
    assert "unknown" in md and "missing" in md