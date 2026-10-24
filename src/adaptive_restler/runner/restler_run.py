"""Offline summarizer for REAL RESTler run directories (verified only on RESTler 9.3.1, demo server).

Reads artifacts RESTler wrote after ``test`` / ``fuzz-lean``. Never starts RESTler or opens a socket.
Raw logs are referenced (relative paths), never copied. Bug identity = RESTler bucket hash.
Every bug is a *candidate*; nothing here validates or confirms a finding.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from adaptive_restler.runner.artifacts import write_json_artifact

PARSER_VERSION = "restler-run-summary/1"
MODE_DIRS = {"test": "Test", "fuzz-lean": "FuzzLean"}
DEDUP_METHOD = "restler_bucket_hash"

_SEND_RE = re.compile(r": Sending: '")
_RECV_RE = re.compile(r": Received: 'HTTP/1\.[01] (\d{3})")
_BUCKET_RE = re.compile(r"^(\S+) - (.+?) - (\S+\.replay\.txt)\s*$")
_HASH_RE = re.compile(r"^Hash: (\S+)\s*$")


class RestlerRunError(ValueError):
    pass


def count_network_log(path: Path) -> dict[str, Any]:
    sends = receives = 0
    status: dict[str, int] = {}
    with Path(path).open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if _SEND_RE.search(line):
                sends += 1
                continue
            m = _RECV_RE.search(line)
            if m:
                receives += 1
                status[m.group(1)] = status.get(m.group(1), 0) + 1
    return {"sends": sends, "receives": receives, "status_counts": status}


def parse_bug_buckets(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    for line in text.splitlines():
        m = _BUCKET_RE.match(line)
        if m:
            cur = {
                "bucket": m.group(1),
                "state": m.group(2).strip(),
                "reproduced": m.group(2).strip().lower() == "bug was reproduced",
                "replay_file": m.group(3),
                "hash": None,
            }
            out.append(cur)
            continue
        h = _HASH_RE.match(line)
        if h and cur is not None and cur["hash"] is None:
            cur["hash"] = h.group(1)
    return out


def _load_json(path: Path) -> Optional[dict]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _count_logs(paths: list[Path], run_dir: Path, sources: list[str]) -> Optional[dict[str, Any]]:
    if not paths:
        return None
    total: dict[str, Any] = {"sends": 0, "receives": 0, "status_counts": {}}
    for p in paths:
        c = count_network_log(p)
        total["sends"] += c["sends"]
        total["receives"] += c["receives"]
        for k, v in c["status_counts"].items():
            total["status_counts"][k] = total["status_counts"].get(k, 0) + v
        sources.append(p.relative_to(run_dir).as_posix())
    total["no_response"] = total["sends"] - total["receives"]
    total["status_counts"] = dict(sorted(total["status_counts"].items()))
    return total


def summarize_run(
    run_dir: str | Path,
    *,
    mode: str,
    run_id: str,
    target_host: str,
    target_port: int,
    spec_path: Optional[str | Path] = None,
    restler_version: Optional[str] = None,
    seed: Optional[int] = None,
    started_utc: Optional[str] = None,
    ended_utc: Optional[str] = None,
    wall_seconds: Optional[float] = None,
    exit_code: Optional[int] = None,
) -> dict[str, Any]:
    if mode not in MODE_DIRS:
        raise RestlerRunError(f"unsupported mode: {mode!r} (verified: {sorted(MODE_DIRS)})")
    run_dir = Path(run_dir)
    errors: list[str] = []
    sources: list[str] = []

    def rel(p: Path) -> str:
        return p.relative_to(run_dir).as_posix()

    if exit_code is not None and exit_code != 0:
        errors.append("restler_exit_nonzero")

    spec = None
    if spec_path:
        sp = Path(spec_path)
        if sp.is_file():
            spec = {"name": sp.name, "sha256": hashlib.sha256(sp.read_bytes()).hexdigest()}
        else:
            spec = {"name": sp.name, "sha256": None}
            errors.append("spec_missing")

    ts: Optional[dict] = None
    rs: Optional[dict] = None
    buckets: list[dict[str, Any]] = []
    fuzz_log = gc_log = None
    mode_dir = run_dir / MODE_DIRS[mode]

    if not mode_dir.is_dir():
        errors.append("mode_dir_missing")
    else:
        res_root = mode_dir / "RestlerResults"
        exps = sorted(res_root.glob("experiment*")) if res_root.is_dir() else []
        if len(exps) != 1:
            errors.append("experiment_dir_missing" if not exps else "experiment_dir_ambiguous")
        else:
            exp = exps[0]
            logs = exp / "logs"
            ts_path = logs / "testing_summary.json"
            ts = _load_json(ts_path)
            if ts is None:
                errors.append("testing_summary_missing_or_unreadable")
            else:
                sources.append(rel(ts_path))
            fuzz_log = _count_logs(sorted(logs.glob("network.testing.*.txt")), run_dir, sources)
            gc_log = _count_logs(sorted(logs.glob("network.gc.*.txt")), run_dir, sources)
            bb = exp / "bug_buckets" / "bug_buckets.txt"
            if bb.is_file():
                buckets = parse_bug_buckets(bb.read_text(encoding="utf-8", errors="replace"))
                sources.append(rel(bb))
                for b in buckets:
                    rf = bb.parent / b["replay_file"]
                    if rf.is_file():
                        sources.append(rel(rf))
        rs_path = mode_dir / "ResponseBuckets" / "runSummary.json"
        rs = _load_json(rs_path) if rs_path.is_file() else None
        if rs is not None:
            sources.append(rel(rs_path))
        err_path = mode_dir / "EngineStdErr.txt"
        if err_path.is_file() and err_path.read_text(encoding="utf-8", errors="replace").strip():
            errors.append("engine_stderr_nonempty")
            sources.append(rel(err_path))
        for extra in (mode_dir / "EngineStdOut.txt", run_dir / "restler_stdout.txt"):
            if extra.is_file():
                sources.append(rel(extra))

    present = [x for x in (fuzz_log, gc_log) if x]
    attempted = sum(x["sends"] for x in present) if present else None
    completed = sum(x["receives"] for x in present) if present else None
    wall_ok = wall_seconds is not None and wall_seconds > 0
    rps = round(attempted / wall_seconds, 3) if attempted is not None and wall_ok else None
    fuzz_5xx = (
        sum(v for k, v in fuzz_log["status_counts"].items() if k.startswith("5")) if fuzz_log else None
    )

    if any(b["reproduced"] and not b["hash"] for b in buckets):
        errors.append("bucket_hash_missing")
    hashes = sorted({b["hash"] for b in buckets if b["reproduced"] and b["hash"]})
    per_min = round(len(hashes) / (wall_seconds / 60.0), 3) if wall_ok and not errors else None

    return {
        "parser_version": PARSER_VERSION,
        "run_id": run_id,
        "mode": mode,
        "target": {"host": target_host, "port": target_port, "ssl": False},
        "spec": spec,
        "restler_version": restler_version,
        "seed": seed,
        "started_utc": started_utc,
        "ended_utc": ended_utc,
        "wall_seconds": wall_seconds,
        "process_exit_code": exit_code,
        "harness_errors": errors,
        "restler_reported": {
            "coverage": ts.get("final_spec_coverage") if ts else None,
            "sequence_failures": ts.get("num_sequence_failures") if ts else None,
            "requests_by_source": ts.get("total_requests_sent") if ts else None,
        },
        "raw_bucket_counts": ts.get("bug_buckets") if ts else None,
        "run_summary": (
            {k: rs.get(k) for k in ("bugCount", "failedRequestsCount", "codeCounts")} if rs else None
        ),
        "network": {"fuzz_log": fuzz_log, "gc_log": gc_log},
        "requests_attempted": attempted,
        "requests_completed": completed,
        "requests_per_second": rps,
        "http_5xx_in_fuzz_log": fuzz_5xx,
        "bug_buckets": buckets,
        "unique_candidate_bug_hashes": hashes,
        "unique_candidate_bugs": len(hashes),
        "dedup_method": DEDUP_METHOD,
        "bug_status": "candidate_unvalidated",
        "unique_candidate_bugs_per_minute": per_min,
        "statistically_meaningful": False,
        "sources": sources,
    }


def aggregate(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        raise RestlerRunError("no records to aggregate")
    sets = [frozenset(r["unique_candidate_bug_hashes"]) for r in records]

    def vals(key: str) -> list[float]:
        return [r[key] for r in records if r.get(key) is not None]

    def stats(xs: list[float]) -> Optional[dict[str, float]]:
        return {"mean": round(statistics.mean(xs), 3), "min": min(xs), "max": max(xs)} if xs else None

    return {
        "parser_version": PARSER_VERSION,
        "runs": len(records),
        "run_ids": [r["run_id"] for r in records],
        "mode": records[0]["mode"],
        "seed": records[0].get("seed"),
        "spec": records[0].get("spec"),
        "harness_ok_all_runs": all(not r["harness_errors"] for r in records),
        "harness_errors_by_run": [r["harness_errors"] for r in records],
        "unique_candidate_bugs_per_run": [len(s) for s in sets],
        "hash_sets_identical_across_runs": len(set(sets)) == 1,
        "unique_hashes_union": sorted(set().union(*sets)),
        "dedup_method": DEDUP_METHOD,
        "wall_seconds": stats(vals("wall_seconds")),
        "requests_attempted_per_run": [r.get("requests_attempted") for r in records],
        "requests_completed_per_run": [r.get("requests_completed") for r in records],
        "requests_per_second": stats(vals("requests_per_second")),
        "unique_candidate_bugs_per_minute": stats(vals("unique_candidate_bugs_per_minute")),
        "statistically_meaningful": False,
        "notes": [
            "Pipeline-validation baseline on a local demo server, not evidence of outperforming RESTler.",
            "Runs share one server instance without a state reset; bucket hashes were compared across runs.",
            "Candidates are unvalidated; 'unique' means distinct RESTler bucket hash.",
        ],
    }


def _print_human(rec: dict[str, Any]) -> None:
    e = sys.stderr
    print(f"run_id={rec['run_id']} mode={rec['mode']} exit={rec['process_exit_code']} wall={rec['wall_seconds']}s", file=e)
    print(f"  requests attempted={rec['requests_attempted']} completed={rec['requests_completed']} rps={rec['requests_per_second']}", file=e)
    print(f"  unique candidate bugs={rec['unique_candidate_bugs']} ({rec['dedup_method']}) raw_5xx_fuzz_log={rec['http_5xx_in_fuzz_log']}", file=e)
    print(f"  harness_errors={rec['harness_errors']}", file=e)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m adaptive_restler.runner.restler_run")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summarize")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--mode", required=True, choices=sorted(MODE_DIRS))
    s.add_argument("--run-id", required=True)
    s.add_argument("--target-host", required=True)
    s.add_argument("--target-port", required=True, type=int)
    s.add_argument("--spec")
    s.add_argument("--restler-version")
    s.add_argument("--seed", type=int)
    s.add_argument("--started-utc")
    s.add_argument("--ended-utc")
    s.add_argument("--wall-seconds", type=float)
    s.add_argument("--exit-code", type=int)
    s.add_argument("--artifact-root", required=True)
    a = sub.add_parser("aggregate")
    a.add_argument("--records", nargs="+", required=True)
    a.add_argument("--run-id", required=True)
    a.add_argument("--artifact-root", required=True)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "summarize":
        rec = summarize_run(
            args.run_dir, mode=args.mode, run_id=args.run_id, target_host=args.target_host,
            target_port=args.target_port, spec_path=args.spec, restler_version=args.restler_version,
            seed=args.seed, started_utc=args.started_utc, ended_utc=args.ended_utc,
            wall_seconds=args.wall_seconds, exit_code=args.exit_code,
        )
        path = write_json_artifact(args.artifact_root, args.run_id, None, "restler_run_summary", rec)
        _print_human(rec)
        print(Path(path).resolve())
        return 0 if not rec["harness_errors"] else 1
    records = [json.loads(Path(p).read_text(encoding="utf-8")) for p in args.records]
    agg = aggregate(records)
    path = write_json_artifact(args.artifact_root, args.run_id, None, "baseline_aggregate", agg)
    print(json.dumps(agg, indent=2), file=sys.stderr)
    print(Path(path).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
