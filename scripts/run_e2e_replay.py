#!/usr/bin/env python3
"""Replay REAL RESTler bug-bucket files through the existing pipeline against a LOCAL target.

import_replay_logs -> DeterministicSelector -> execute_candidate (UrllibSender) -> validate_findings
-> compute_metrics -> persist_run. Findings are leads only. Replay logs hold literal ids from the original
run, so server state may differ and a bucket may not reproduce; that is reported, not hidden.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from adaptive_restler.evaluation.metrics import compute_metrics
from adaptive_restler.evaluation.report import persist_run
from adaptive_restler.models import (
    BudgetState, ExecutionBudget, ExperimentContext, TargetConfig, ValidationContext,
)
from adaptive_restler.runner.artifacts import write_json_artifact
from adaptive_restler.runner.executor import execute_candidate
from adaptive_restler.runner.http_sender import UrllibSender
from adaptive_restler.runner.restler_adapter import PARSER_STATUS, import_replay_logs
from adaptive_restler.selection import DeterministicSelector, SelectionContext
from adaptive_restler.validation.findings import validate_findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay-dir", required=True)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--artifact-root", default="artifacts")
    ap.add_argument("--run-id")
    ap.add_argument("--max-requests", type=int, default=100)
    ap.add_argument("--max-seconds", type=float, default=60.0)
    ap.add_argument("--request-timeout", type=float, default=10.0)
    a = ap.parse_args(argv)

    run_id = a.run_id or "e2e-replay-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    paths = sorted(Path(a.replay_dir).glob("*.replay.txt"))
    if not paths:
        print(f"no *.replay.txt in {a.replay_dir}", file=sys.stderr)
        return 2

    target = TargetConfig(base_url=f"http://localhost:{a.port}", allowed_hosts=["localhost", "127.0.0.1"], dry_run=False)
    budget = ExecutionBudget(max_requests=a.max_requests, max_runtime_seconds=a.max_seconds,
                             request_timeout_seconds=a.request_timeout)
    root = Path(a.artifact_root)
    ctx = ExperimentContext(run_id=run_id, target_config=target, execution_budget=budget,
                            artifact_dir=str(root), benchmark_id="restler-demo-local", configuration_version="v1")
    vctx = ValidationContext(run_id=run_id, artifact_dir=str(root), benchmark_id="restler-demo-local",
                             expected_authorization_policy="not-applicable-demo", test_identity_refs=[])

    errors: list[str] = []
    imp = import_replay_logs(paths, target=target)
    for rej in imp.rejections:
        errors.append(f"import_rejected:{Path(rej.source_name).name}:{rej.code}")
    if not imp.candidates:
        errors.append("no_candidates")
    by_id = {c.candidate_id: c for c in imp.candidates}

    sel = DeterministicSelector().rank(imp.candidates, SelectionContext(target=target, remaining_requests=budget.max_requests))
    started = datetime.now(timezone.utc)
    state = BudgetState(run_id=run_id, requests_used=0, started_at=started, stop_reason=None)
    sender = UrllibSender()

    results, findings, rows = [], [], []
    for cid in sel.ranked_ids:
        cand = by_id[cid]
        res = execute_candidate(cand, ctx, state, sender=sender)
        found = validate_findings(res, vctx)
        results.append(res)
        findings.extend(found)
        prov = imp.provenance[cid]
        bucket = Path(prov.source_file).name.replace(".replay.txt", "")
        recorded = [p.recorded_status for p in prov.requests]
        observed = [r.get("status_code") for r in res.responses]
        if res.status.value == "failed":
            errors.append(f"candidate_failed:{bucket}")
        rows.append({
            "bucket": bucket, "candidate_id": cid, "status": res.status.value,
            "steps": [f"{r.method} {r.path}" for r in cand.sequence],
            "recorded_statuses": recorded, "observed_statuses": observed,
            "status_sequence_match": recorded == observed,
            "observed_5xx": any(isinstance(s, int) and s >= 500 for s in observed),
            "request_count": res.request_count, "error": res.error_summary,
            "evidence_paths": list(res.evidence_paths),
        })
    ended = datetime.now(timezone.utc)

    metrics = compute_metrics(results, findings)
    persist_run(root, run_id, results, findings)
    summary = {
        "run_id": run_id, "importer_status": PARSER_STATUS, "replay_dir": str(a.replay_dir),
        "target": f"localhost:{a.port}", "started_utc": started.isoformat(), "ended_utc": ended.isoformat(),
        "wall_seconds": round((ended - started).total_seconds(), 3),
        "selector": sel.selector_name, "ranked_ids": sel.ranked_ids,
        "selection_rejections": [d.model_dump(mode="json") for d in sel.rejected],
        "import_rejections": [{"file": Path(r.source_name).name, "code": r.code, "line": r.line_no} for r in imp.rejections],
        "candidates": rows, "metrics": metrics,
        "findings": [{"category": f.category, "status": f.status.value, "fingerprint": f.fingerprint} for f in findings],
        "harness_errors": errors,
        "limitations": [
            "Replay logs contain literal ids from the original run; server state differs, so reproduction is not guaranteed.",
            "Findings are unvalidated leads (5xx alone is not proof of a vulnerability).",
            "Local demo server, one importer-parsed RESTler version; not a measure of selection quality.",
        ],
    }
    path = write_json_artifact(root, run_id, None, "e2e_replay_summary", summary)

    for r in rows:
        print(f"{r['bucket']:<42} {r['status']:<10} recorded={r['recorded_statuses']} observed={r['observed_statuses']} 5xx={r['observed_5xx']}")
    print(f"requests_total={metrics['requests_total']} leads_unique={metrics['leads_unique']} "
          f"attempts_completed={metrics['attempts_completed']} wall={summary['wall_seconds']}s harness_errors={errors}")
    print(Path(path).resolve())
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
