#!/usr/bin/env python3
"""One-shot LLM-seeded, budget-bounded API testing loop for local crAPI.

Each round calls Qwen 3B once (no repair call), then Qwen 7B once to select a
candidate. Invalid batches are recorded and the next round uses a different
seed. The controller keeps producing fresh rounds when its finite mutation
queue is exhausted, until the wall-clock deadline or request cap is reached.
Targets are restricted to loopback addresses.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
RUN_ROOT = ROOT / "artifacts" / "llm_sequence_experiment_oneshot"


def norm(path: str) -> str:
    return re.sub(r"/([0-9a-fA-F-]{8,}|\d+)(?=/|$)", "/{id}", path.split("?", 1)[0])


def read_replay(path: Path) -> list[dict]:
    steps = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return steps
    for line in lines:
        if not line.startswith("-> "):
            continue
        raw = line[3:]
        head, sep, body_text = raw.partition(r"\r\n\r\n")
        match = re.match(r"(\w+) (\S+) HTTP/\d\.\d", head)
        if not match:
            continue
        body = None
        if sep and body_text.strip():
            try:
                body = json.loads(body_text)
            except json.JSONDecodeError:
                continue
        steps.append({"method": match.group(1), "path": match.group(2), "body": body})
    return steps


def read_network_seeds(path: Path) -> list[dict]:
    """Read concrete JSON request bodies from RESTler's escaped network log."""
    steps = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return steps
    for line in lines:
        match = re.search(r"Sending: '(\w+) (\S+) HTTP/1\.1", line)
        if not match:
            continue
        sep = r"\r\n\r\n"
        pos = line.find(sep)
        body = None
        if pos >= 0:
            raw_body = line[pos + len(sep):].strip()
            if raw_body.endswith("'"):
                raw_body = raw_body[:-1]
            if raw_body.strip():
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError:
                    body = None
        steps.append({"method": match.group(1), "path": match.group(2), "body": body})
    return steps


def parse_network_log(path: Path) -> tuple[list[tuple[str, int]], float]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], 0.0
    sends = re.findall(r"Sending: '(\w+) (\S+) HTTP/1\.1", text)
    statuses = [int(x) for x in re.findall(r"Received: 'HTTP/1\.1 (\d{3})", text)]
    rows = [(f"{method} {norm(path)}", status) for (method, path), status in zip(sends, statuses)]
    stamps = re.findall(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+):", text, flags=re.M)
    elapsed = 0.0
    if len(stamps) > 1:
        from datetime import datetime as dt
        try:
            parsed = [dt.strptime(s, "%Y-%m-%d %H:%M:%S.%f") for s in stamps]
            elapsed = max(0.0, (max(parsed) - min(parsed)).total_seconds())
        except ValueError:
            pass
    # Unmatched sends still count as attempted requests.
    for method, path in sends[len(statuses):]:
        rows.append((f"{method} {norm(path)}", 0))
    return rows, elapsed


def extract_token(text: str) -> str | None:
    match = re.search(r'"token"\s*:\s*"([^"\\]+)"', text)
    return match.group(1) if match else None


def send(base: str, step: dict, token: str | None = None, raw_body: bytes | None = None,
         timeout: float = 4.0) -> tuple[int, str]:
    if raw_body is not None:
        data = raw_body
    else:
        data = json.dumps(step["body"]).encode() if step.get("body") is not None else None
    request = urllib.request.Request(base + step["path"], data=data, method=step["method"])
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(800).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(800).decode("utf-8", "replace")
    except Exception as exc:
        return 0, f"connection-error: {type(exc).__name__}"


def mutants(body: object, rng: random.Random):
    """Small local-only mutation set; values vary per seed/cycle to avoid exact repeats."""
    if not isinstance(body, dict):
        return
    values = [
        "", " ", "A" * 1024, "' OR '1'='1", "../../etc/passwd", "\u0000", "%s%s%s",
        None, 0, -1, 2**63, 3.14, True, [], {}, str(rng.randrange(10**9, 10**10)),
    ]
    for key, original in body.items():
        for value in values:
            if value != original:
                yield f"{key}={json.dumps(value)[:24]}", {**body, key: value}
        yield f"drop:{key}", {k: v for k, v in body.items() if k != key}
    yield "empty-object", {}


def stats(rows: list[tuple[str, int]]) -> dict:
    statuses = [status for _, status in rows]
    return {
        "requests": len(rows),
        "2xx": sum(200 <= s < 300 for s in statuses),
        "4xx": sum(400 <= s < 500 for s in statuses),
        "5xx": sum(500 <= s < 600 for s in statuses),
        "network_errors": sum(s == 0 for s in statuses),
        "endpoints": len({endpoint for endpoint, _ in rows}),
        "5xx_endpoints": len({endpoint for endpoint, status in rows if 500 <= status < 600}),
    }


def baseline_rows(directory: str, max_requests: int | None) -> tuple[list[tuple[str, int]], float]:
    files = glob.glob(os.path.join(directory, "**", "network.testing.*.txt"), recursive=True)
    rows: list[tuple[str, int]] = []
    durations = []
    for filename in files:
        chunk, duration = parse_network_log(Path(filename))
        rows.extend(chunk)
        if duration:
            durations.append(duration)
    if max_requests:
        rows = rows[:max_requests]
    # Prefer full campaign wall time inferred from the complete set of event timestamps.
    if files:
        all_stamps = []
        pattern = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+):", re.M)
        from datetime import datetime as dt
        for filename in files:
            try:
                text = Path(filename).read_text(encoding="utf-8", errors="replace")
                all_stamps.extend(dt.strptime(x, "%Y-%m-%d %H:%M:%S.%f") for x in pattern.findall(text))
            except (OSError, ValueError):
                pass
        duration = (max(all_stamps) - min(all_stamps)).total_seconds() if len(all_stamps) > 1 else 0.0
    else:
        duration = 0.0
    return rows, duration


def make_mutation_queue(seeds: list[tuple[dict, str | None]], max_mutants: int, cycle: int,
                        seen_mutations: set[str]) -> deque:
    queue = deque()
    for index, (step, token) in enumerate(seeds):
        body = step.get("body")
        if not isinstance(body, dict):
            continue
        rng = random.Random(cycle * 10007 + index * 97)
        for label, mutated_body in list(mutants(body, rng))[:max_mutants]:
            signature = "|".join((step.get("method", ""), step.get("path", ""), json.dumps(mutated_body, sort_keys=True, default=str)))
            if signature in seen_mutations:
                continue
            seen_mutations.add(signature)
            queue.append((step, token, label, mutated_body))
    return queue


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--budget", type=int, default=300)
    parser.add_argument("--target", default="http://127.0.0.1:8888")
    parser.add_argument("--baseline", help="Directory containing RESTler network.testing logs")
    parser.add_argument("--max-rounds", type=int, default=100, help="Hard ceiling; wall-clock budget normally stops the loop first")
    parser.add_argument("--round-timeout", type=int, default=68, help="Maximum wall time for one no-repair LLM round")
    parser.add_argument("--phase1-fraction", type=float, default=0.6, help="Initial exploration phase fraction; new LLM rounds can also start later when mutation work is exhausted")
    parser.add_argument("--max-mutants-per-step", type=int, default=60)
    parser.add_argument("--max-requests", type=int, default=None, help="Optional request-count cap; when set, the loop stops at the cap or budget")
    parser.add_argument("--draft-temperature", type=float, default=0.1)
    args = parser.parse_args()

    parsed = urlparse(args.target)
    if parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        parser.error("loopback targets only")
    if args.budget < 60 or args.round_timeout < 10 or not 0 < args.phase1_fraction <= 1:
        parser.error("budget must be >=60, round-timeout >=10, and phase1-fraction in (0,1]")

    t0 = time.monotonic()
    left = lambda: args.budget - (time.monotonic() - t0)
    out_dir = ROOT / "artifacts" / "llm_budget_loop" / f"loop-{datetime.now():%Y%m%dT%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=False)
    baseline, baseline_wall = baseline_rows(args.baseline, args.max_requests) if args.baseline else ([], 0.0)
    rows: list[tuple[str, int]] = []
    covered_paths: set[str] = set()
    run_dirs: list[str] = []
    round_summaries: list[dict] = []
    seeds: list[tuple[dict, str | None]] = []
    token: str | None = None
    llm_stats = {"drafts": 0, "first": 0, "final": 0, "repair_calls": 0}
    queue: deque = deque()
    round_n = 0
    cycle = 0
    phase1_end = args.budget * args.phase1_fraction
    phase1_done = False
    consecutive_no_run = 0
    seen_mutations: set[str] = set()
    lead_candidates: dict[tuple[str, int], dict] = {}
    # A short reserve is left for reproducing 5xx leads and writing the report.
    reserve = 5.0
    print(f"[plan] one-shot 3B drafting (repair disabled); target phase 1 ends near t={phase1_end:.0f}s; "
          f"fresh LLM rounds will continue when mutation work is exhausted, up to t={args.budget}s", flush=True)

    while left() > reserve:
        hit_cap = bool(args.max_requests and len(rows) >= args.max_requests)
        if hit_cap:
            print(f"[stop] request cap reached ({len(rows)}/{args.max_requests})", flush=True)
            break

        elapsed = args.budget - left()
        if elapsed >= phase1_end and not phase1_done:
            phase1_done = True
            cycle += 1
            queue = make_mutation_queue(seeds, args.max_mutants_per_step, cycle, seen_mutations)
            print(f"[mutation] starting phase 2 with {len(seeds)} seed request(s); {left():.0f}s remain", flush=True)
        can_start_round = left() > 42 and round_n < args.max_rounds and consecutive_no_run < 2
        if args.max_requests and len(rows) + 15 >= args.max_requests and not queue:
            can_start_round = False
        # During phase 1 keep discovering new operations. After it, mutate current seeds;
        # when this finite queue empties, generate a new batch rather than exiting early.
        should_run_round = can_start_round and (elapsed < phase1_end or not queue)
        if should_run_round:
            round_n += 1
            print(f"[LLM round {round_n}] t={elapsed:.0f}s, excluding {len(covered_paths)} covered path(s), repair disabled", flush=True)
            before = set(glob.glob(str(RUN_ROOT / "run-*")))
            target_ip = parsed.hostname or "127.0.0.1"
            target_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            host = f"{target_ip}:{target_port}"
            command = [
                sys.executable, "scripts/run_llm_sequence_experiment_oneshot.py",
                "--output-root", "artifacts/llm_sequence_experiment_oneshot",
                "--max-approved-sequences", "3",
                "--max-repair-attempts", "0",
                "--draft-seed", str(41 + 997 * round_n),
                "--draft-temperature", str(args.draft_temperature),
                "--target-ip", target_ip,
                "--target-port", str(target_port),
                "--host", host,
            ]
            for path in sorted(covered_paths):
                command.extend(["--exclude-path", path])
            env = dict(os.environ)
            if token:
                env["CRAPI_BEARER_TOKEN"] = token
            timeout = max(5, min(args.round_timeout, int(left() - reserve)))
            try:
                proc = subprocess.run(command, cwd=ROOT, env=env, timeout=timeout, check=False)
                return_code = proc.returncode
            except subprocess.TimeoutExpired:
                return_code = 124
                print(f"[LLM round {round_n}] timed out after {timeout}s; continuing within overall budget", flush=True)

            after = set(glob.glob(str(RUN_ROOT / "run-*")))
            new_dirs = sorted(after - before, key=lambda p: os.path.getmtime(p))
            run_dir = new_dirs[-1] if new_dirs else None
            summary = {"round": round_n, "exit_code": return_code, "run_dir": run_dir, "sequences": 0, "drafts": 0, "first_pass_valid": 0, "final_valid": 0}
            if run_dir:
                consecutive_no_run = 0
                run_dirs.append(run_dir)
                report_path = Path(run_dir) / "report.json"
                if report_path.is_file():
                    try:
                        run_report = json.loads(report_path.read_text(encoding="utf-8"))
                        dv = run_report.get("draft_validation") or {}
                        llm_stats["drafts"] += int(dv.get("draft_count", 0))
                        llm_stats["first"] += int(dv.get("first_pass_valid_count", 0))
                        llm_stats["final"] += int(dv.get("post_repair_valid_count", dv.get("first_pass_valid_count", 0)))
                        llm_stats["repair_calls"] += int(dv.get("repair_calls_made", 0))
                        summary.update({
                            "drafts": dv.get("draft_count", 0),
                            "first_pass_valid": dv.get("first_pass_valid_count", 0),
                            "final_valid": dv.get("post_repair_valid_count", dv.get("first_pass_valid_count", 0)),
                            "error": run_report.get("error"),
                        })
                    except (OSError, ValueError, TypeError):
                        pass
                network_files = sorted(Path(run_dir).rglob("network.replay.*.txt"))
                round_seeds: list[dict] = []
                for replay in sorted(Path(run_dir).rglob("sequence.replay.txt")):
                    round_seeds.extend(read_replay(replay))
                if not round_seeds:
                    for network in network_files:
                        round_seeds.extend(read_network_seeds(network))
                # Deduplicate a request body/path within this round.
                uniq_round = []
                seen_keys = set()
                for step in round_seeds:
                    key = (step.get("method"), step.get("path"), json.dumps(step.get("body"), sort_keys=True, default=str))
                    if key not in seen_keys:
                        seen_keys.add(key)
                        uniq_round.append(step)
                round_seeds = uniq_round
                for network in network_files:
                    text = network.read_text(encoding="utf-8", errors="replace")
                    found_token = extract_token(text)
                    if found_token:
                        token = found_token
                    network_rows, _ = parse_network_log(network)
                    rows.extend(network_rows)
                for step in round_seeds:
                    path = norm(step["path"]).strip("/")
                    covered_paths.add(path)
                    seeds.append((step, token))
                summary["sequences"] = len(round_seeds)
                if round_seeds and phase1_done:
                    cycle += 1
                    queue = make_mutation_queue(seeds[-len(round_seeds):], args.max_mutants_per_step, cycle, seen_mutations)
            else:
                consecutive_no_run += 1
                summary["error"] = "child runner did not create a new report directory"
                # A fully exhausted exclude list can leave fewer than two operations.
                # Clear it once and let the changed draft seed explore the full catalog again.
                if consecutive_no_run == 1 and covered_paths:
                    print("[LLM loop] no new run directory; clearing path exclusions for one full-catalog attempt", flush=True)
                    covered_paths.clear()
                elif consecutive_no_run >= 2:
                    print("[LLM loop] repeated preflight failures; will stop model retries and use available mutation seeds", flush=True)
            round_summaries.append(summary)
            print(f"[LLM round {round_n}] result: sequences={summary['sequences']}, "
                  f"first-pass valid={summary['first_pass_valid']}/{summary['drafts']}; "
                  f"{left():.0f}s remain", flush=True)
            continue

        if queue:
            if args.max_requests and len(rows) >= args.max_requests:
                print(f"[stop] request cap reached ({len(rows)}/{args.max_requests})", flush=True)
                break
            step, step_token, label, body = queue.popleft()
            code, response = send(args.target, step, step_token or token, json.dumps(body).encode())
            endpoint = f"{step['method']} {norm(step['path'])}"
            rows.append((endpoint, code))
            if code >= 500:
                # Keep only non-sensitive metadata in the eventual report.
                response_message = ""
                try:
                    response_json = json.loads(response)
                    response_message = str(response_json.get("message") or response_json.get("detail") or "")[:160]
                except (ValueError, AttributeError):
                    pass
                leads_key = (endpoint, code)
                # Keep the actual triggering mutation in memory for reproducibility;
                # only sanitized metadata is written to the report.
                if leads_key not in lead_candidates:
                    lead_candidates[leads_key] = {
                        "endpoint": endpoint, "status": code, "mutation": label,
                        "response_message": response_message, "step": dict(step),
                        "token": step_token or token, "raw_body": json.dumps(body).encode(),
                    }
            continue

        # No finite mutation work remains. If another model round can fit, the loop above
        # starts one; otherwise use remaining seconds for one last shorter mutation cycle.
        if seeds and left() > reserve + 1:
            cycle += 1
            queue = make_mutation_queue(seeds, args.max_mutants_per_step, cycle, seen_mutations)
            if queue:
                print(f"[mutation] refreshed unseen mutation values for {len(seeds)} seed request(s); {left():.0f}s remain", flush=True)
                continue
        # If a batch had no valid/approved sequences, retry with a fresh seed while a
        # full round still fits. Exit only when the budget/round ceiling makes another
        # attempt impractical; never spin without a bounded model round.
        if not seeds and left() > 42 and round_n < args.max_rounds and consecutive_no_run < 2:
            print(f"[LLM loop] no usable seeds yet; trying another one-shot batch, {left():.0f}s remain", flush=True)
            continue
        # No usable seeds and insufficient time for another bounded model round.
        break

    # Reproduce each distinct 5xx bucket found by the mutation stage, while preserving budget.
    found_buckets = dict(lead_candidates)
    leads = []
    # Reproduce the exact mutated request (not the original seed body).
    for (endpoint, status), metadata in found_buckets.items():
        if left() <= 1:
            break
        hits = 0
        for _ in range(3):
            if left() <= 0.7:
                break
            code, _ = send(
                args.target, metadata["step"], metadata.get("token"), metadata["raw_body"],
                timeout=min(2.0, max(0.5, left())),
            )
            hits += int(code == status)
        leads.append({
            "endpoint": endpoint, "status": status, "mutation": metadata.get("mutation"),
            "reproduced": f"{hits}/3", "reproducible": hits >= 2,
            "response_message": metadata.get("response_message", ""),
        })

    elapsed = time.monotonic() - t0
    final_rows = rows[:args.max_requests] if args.max_requests else rows
    s = stats(final_rows)
    repro = [lead for lead in leads if lead["reproducible"]]
    report = {
        "budget_s": args.budget, "elapsed_s": round(elapsed, 1), "budget_used_percent": round(100 * elapsed / args.budget, 1),
        "llm_rounds": round_n, "llm": llm_stats, "rounds": round_summaries,
        "covered_paths": sorted(covered_paths), "http": s, "distinct_5xx_buckets": len(found_buckets),
        "reproducible_5xx_leads": len(repro), "leads": leads,
        "baseline_http": stats(baseline) if baseline else None, "baseline_wall_s": round(baseline_wall, 1) if baseline_wall else None,
        "caveat": "5xx status codes are untriaged leads, not confirmed vulnerabilities. Mutation traffic is generated only for the loopback target.",
    }
    (out_dir / "loop_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{'METRIC':<34}{'RESTler fuzz-lean':<22}{'LLM one-shot loop'}")
    print("-" * 78)
    b = stats(baseline) if baseline else {}
    for key, label in [("requests", "HTTP requests sent"), ("2xx", "2xx responses"), ("4xx", "4xx responses"),
                       ("5xx", "5xx responses"), ("endpoints", "Distinct endpoints hit"),
                       ("5xx_endpoints", "Distinct endpoints w/ 5xx")]:
        print(f"{label:<34}{str(b.get(key, 'n/a')):<22}{s[key]}")
    print(f"{'Distinct 5xx buckets (ep,status)':<34}{'n/a':<22}{len(found_buckets)}")
    print(f"{'Reproducible 5xx leads (>=2/3)':<34}{'not measured':<22}{len(repro)}")
    if baseline and baseline_wall > 0:
        base_rate = 60 * len(baseline) / baseline_wall
        print(f"{'Wall time (s)':<34}{str(round(baseline_wall,1)):<22}{round(elapsed,1)}")
        print(f"{'Requests / min':<34}{str(round(base_rate,1)):<22}{round(60 * s['requests'] / elapsed,1) if elapsed else 0}")
    else:
        print(f"{'Wall time (s)':<34}{'n/a':<22}{round(elapsed,1)}")
        print(f"{'Requests / min':<34}{'n/a':<22}{round(60 * s['requests'] / elapsed,1) if elapsed else 0}")
    print("-" * 78)
    print(f"{'LLM rounds run':<34}{'n/a':<22}{round_n}")
    print(f"{'LLM drafts valid 1st pass':<34}{'n/a':<22}{llm_stats['first']}/{llm_stats['drafts']}")
    print(f"{'LLM drafts valid final':<34}{'n/a':<22}{llm_stats['final']}/{llm_stats['drafts']}")
    print(f"Budget used: {elapsed:.1f}/{args.budget}s ({100 * elapsed / args.budget:.1f}%)")
    print("Caveat: reproducible HTTP 5xx responses are leads, not confirmed vulnerabilities.")
    print(f"Report: {out_dir / 'loop_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
