#!/usr/bin/env python3
"""Selector benchmark on a local VAmPI container (unauthenticated operations only).
DeterministicSelector vs LLM ranker, same pool, same request budget, DB reset between runs.
Leads are unvalidated candidates; fingerprint = category|method|path-template."""
from __future__ import annotations
import argparse, json, statistics as st, subprocess, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

from adaptive_restler.models import (BudgetState, CandidateRequest, CandidateSequence, ExecutionBudget,
                                     ExperimentContext, TargetConfig, ValidationContext)
from adaptive_restler.runner.artifacts import write_json_artifact
from adaptive_restler.runner.executor import execute_candidate
from adaptive_restler.runner.http_sender import UrllibSender
from adaptive_restler.selection import DeterministicSelector, SelectionContext
from adaptive_restler.selection.config import load_model_config
from adaptive_restler.selection.provider import OllamaProvider
from adaptive_restler.selection.ranker import build_assessment_ranker_selector
from adaptive_restler.validation.findings import validate_findings

JSON_H = {"Content-Type": "application/json"}

def build_pool() -> list[CandidateSequence]:
    seqs: list[list[CandidateRequest]] = []
    R = lambda m, p, b=None: CandidateRequest(method=m, path=p, headers=(JSON_H if b is not None else {}), body=b)
    for p in ["/", "/users/v1", "/users/v1/_debug", "/books/v1"]:
        seqs.append([R("GET", p)])
    for u in ["name1", "admin", "nobody", "%27", "name1%27--", "a" * 200]:
        seqs.append([R("GET", f"/users/v1/{u}")])
    reg = [{"username": "u1", "password": "p1", "email": "u1@mail.com"},
           {"username": "name1", "password": "p1", "email": "dup@mail.com"},
           {"username": "", "password": "p", "email": "e@mail.com"},
           {"username": "u%27x", "password": "p", "email": "e@mail.com"},
           {"username": "u2"},
           {"username": "u3", "password": "p", "email": "not-an-email"},
           {"username": "u4", "password": "p", "email": "u4@mail.com", "admin": True},
           {"username": "u5", "password": 12345, "email": "u5@mail.com"}]
    for b in reg:
        seqs.append([R("POST", "/users/v1/register", b)])
    for b in [{"username": "name1", "password": "wrong"}, {"username": "name1"}, {}, {"username": "x%27--", "password": "y"}]:
        seqs.append([R("POST", "/users/v1/login", b)])
    seqs.append([R("POST", "/users/v1/register", {"username": "c1", "password": "p", "email": "c1@mail.com"}),
                 R("POST", "/users/v1/login", {"username": "c1", "password": "p"})])
    seqs.append([R("POST", "/users/v1/register", {"username": "c2", "password": "p", "email": "c2@mail.com"}),
                 R("GET", "/users/v1/c2")])
    seqs.append([R("POST", "/users/v1/login", {"username": "name1", "password": "bad"}),
                 R("POST", "/users/v1/login", {"username": "name1", "password": "bad2"})])
    return [CandidateSequence(candidate_id=f"gen-{i:03d}", sequence=s, source="generated", tags=[],
                              estimated_request_count=len(s), metadata={}) for i, s in enumerate(seqs)]

def reset(port: int, container: str) -> None:
    subprocess.run(["docker", "restart", container], capture_output=True, timeout=60)
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            if urllib.request.urlopen(base + "/", timeout=3).status == 200:
                break
        except Exception:
            time.sleep(1)
    else:
        raise RuntimeError("VAmPI did not come up")
    urllib.request.urlopen(base + "/createdb", timeout=20).read()

def run_once(name, selector, pool, target, budget, root, run_id):
    ctx = ExperimentContext(run_id=run_id, target_config=target, execution_budget=budget, artifact_dir=str(root),
                            benchmark_id="vampi-local", configuration_version="v1")
    vctx = ValidationContext(run_id=run_id, artifact_dir=str(root), benchmark_id="vampi-local",
                             expected_authorization_policy="not-applicable-demo", test_identity_refs=[])
    t0 = time.perf_counter()
    sel = selector.rank(pool, SelectionContext(target=target, remaining_requests=budget.max_requests))
    sel_s = time.perf_counter() - t0
    by_id = {c.candidate_id: c for c in pool}
    state = BudgetState(run_id=run_id, requests_used=0, started_at=datetime.now(timezone.utc), stop_reason=None)
    sender, used, fps, five, order, curve = UrllibSender(), 0, set(), 0, [], []
    t1 = time.perf_counter()
    for cid in sel.ranked_ids:
        if used >= budget.max_requests:
            break
        res = execute_candidate(by_id[cid], ctx, state, sender=sender)
        used += res.request_count
        order.append(cid)
        five += sum(1 for r in res.responses if isinstance(r.get("status_code"), int) and r["status_code"] >= 500)
        fps.update(f.fingerprint for f in validate_findings(res, vctx))
        curve.append([used, len(fps)])
    exec_s = time.perf_counter() - t1
    total = sel_s + exec_s
    return {"selector": name, "selector_name": sel.selector_name, "requests_used": used, "leads_unique": len(fps),
            "fingerprints": sorted(fps), "raw_5xx": five, "selection_seconds": round(sel_s, 3),
            "exec_seconds": round(exec_s, 3), "leads_per_request": round(len(fps) / used, 4) if used else 0.0,
            "leads_per_minute": round(len(fps) / total * 60, 2) if total else 0.0,
            "order": order, "curve_requests_leads": curve}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5002)
    ap.add_argument("--container", default="vampi-vuln")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-requests", type=int, default=15)
    ap.add_argument("--artifact-root", default="artifacts")
    a = ap.parse_args()
    target = TargetConfig(base_url=f"http://127.0.0.1:{a.port}", allowed_hosts=["localhost", "127.0.0.1"], dry_run=False)
    budget = ExecutionBudget(max_requests=a.max_requests, max_runtime_seconds=120.0, request_timeout_seconds=5.0)
    cfg = load_model_config("config/default.yaml")
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = Path(a.artifact_root); pool = build_pool()
    print(f"pool={len(pool)} budget={a.max_requests} reps={a.reps} small_model={cfg.small_model} large_model={cfg.large_model}")
    runs = []
    for rep in range(1, a.reps + 1):
        for name in ("deterministic", "llm_ranker"):
            rid = f"selector-bench-{ts}-{name}-rep{rep}"
            try:
                reset(a.port, a.container)
                sel = (DeterministicSelector() if name == "deterministic" else
                       build_assessment_ranker_selector(OllamaProvider(cfg.base_url), cfg, budget, run_id=rid))
                r = run_once(name, sel, pool, target, budget, root, rid); r["rep"] = rep
            except Exception as e:
                r = {"selector": name, "rep": rep, "error": f"{type(e).__name__}: {e}"[:300]}
            runs.append(r)
            print({k: v for k, v in r.items() if k not in ("order", "curve_requests_leads", "fingerprints")}, flush=True)
    agg = {}
    for name in ("deterministic", "llm_ranker"):
        ok = [r for r in runs if r["selector"] == name and "error" not in r]
        if ok:
            f = lambda k: round(st.mean(r[k] for r in ok), 3)
            agg[name] = {"ok_runs": len(ok), "leads_unique": f("leads_unique"), "requests_used": f("requests_used"),
                         "leads_per_request": f("leads_per_request"), "leads_per_minute": f("leads_per_minute"),
                         "selection_seconds": f("selection_seconds")}
    out = {"ts": ts, "pool": len(pool), "budget": a.max_requests, "models": [cfg.small_model, cfg.large_model],
           "aggregate": agg, "runs": runs,
           "caveats": ["Pipeline comparison on one small local target; not statistically meaningful.",
                       "Leads are unvalidated; fingerprint is category|method|path-template, so the ceiling is low.",
                       "Unauthenticated operations only; the sender's auth support is unverified."]}
    p = write_json_artifact(root, f"selector-bench-{ts}", None, "selector_benchmark", out)
    print("AGGREGATE", json.dumps(agg, indent=1)); print(Path(p).resolve())

if __name__ == "__main__":
    main()
