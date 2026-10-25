#!/usr/bin/env python3
"""LLM-seeded, feedback-driven, budget-bounded API testing loop (loopback only).

Each LLM round excludes operations already covered by earlier rounds (via
--exclude-path), so qwen2.5:3b/7b are forced onto new endpoints instead of
re-approving the same signup->login sequence every time. After the first
round succeeds, its login token is captured and passed to later rounds via
CRAPI_BEARER_TOKEN so the models/RESTler can reach authenticated endpoints.

Phase 1: repeated LLM rounds (draft -> validate/repair -> select -> RESTler replay),
         each excluding previously-covered paths.
Phase 2: spend remaining budget mutating every request seen across all rounds,
         replayed directly over HTTP (with the captured token where applicable).
Phase 3: re-send each distinct 5xx three times -> 'reproducible 5xx leads'
         (candidate bugs, NOT confirmed vulnerabilities -- untriaged).

Usage: python scripts/run_budget_loop.py --budget 300 [--baseline DIR]
"""
import argparse, glob, json, os, random, re, subprocess, sys, time, urllib.request, urllib.error
from datetime import datetime
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)


def norm(p):
    return re.sub(r"/([0-9a-fA-F-]{8,}|\d+)(?=/|$)", "/{id}", p.split("?")[0])


def read_replay(path):
    steps = []
    for line in open(path, errors="replace"):
        if not line.startswith("-> "):
            continue
        raw = line[3:].rstrip("\n")
        head, _, body = raw.partition("\\r\\n\\r\\n")
        m = re.match(r"(\w+) (\S+) HTTP", head)
        if not m:
            continue
        try:
            b = json.loads(body) if body.strip() else None
        except ValueError:
            continue
        steps.append({"method": m.group(1), "path": m.group(2), "body": b})
    return steps


def send(base, step, token=None, raw_body=None):
    data = raw_body if raw_body is not None else (
        json.dumps(step["body"]).encode() if step["body"] is not None else None)
    req = urllib.request.Request(base + step["path"], data=data, method=step["method"])
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(600).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(600).decode("utf-8", "replace")
    except Exception as e:
        return 0, f"conn-error: {e}"


def mutants(body, rng):
    """Deterministic schema-aware mutations of one JSON object body."""
    if not isinstance(body, dict):
        return
    vals = ["", " ", "A" * 5000, "' OR '1'='1", "../../etc/passwd", "\u0000", "%s%s%s",
            None, 0, -1, 2**63, 3.14, True, [], {}, "9" * 40]
    for k, v in body.items():
        for nv in vals:
            if nv != v:
                yield f"{k}={json.dumps(nv)[:20]}", {**body, k: nv}
        yield f"drop:{k}", {kk: vv for kk, vv in body.items() if kk != k}
    yield "empty-object", {}


def stats(rows):
    n = len(rows)
    return {"requests": n, "2xx": sum(200 <= s < 300 for _, s in rows),
            "4xx": sum(400 <= s < 500 for _, s in rows), "5xx": sum(s >= 500 for _, s in rows),
            "endpoints": len({e for e, _ in rows}),
            "5xx_endpoints": len({e for e, s in rows if s >= 500})}


def parse_logs(files):
    S, R, T = [], [], []
    for f in files:
        for l in open(f, errors="replace"):
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+): (Sending|Received): "
                         r"'(?:(\w+) (\S+) HTTP|HTTP/1\.1 (\d{3}))", l)
            if not m:
                continue
            T.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f"))
            if m.group(2) == "Sending":
                S.append(f"{m.group(3)} {norm(m.group(4))}")
            else:
                R.append(int(m.group(5)))
    pairs = list(zip(S, R))
    secs = (max(T) - min(T)).total_seconds() if len(T) > 1 else 0
    return pairs, secs


def extract_token(text):
    m = re.search(r'"token"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=300)
    ap.add_argument("--target", default="http://127.0.0.1:8888")
    ap.add_argument("--baseline", help="dir containing the crAPI RESTler baseline network.testing logs")
    ap.add_argument("--max-rounds", type=int, default=8)
    ap.add_argument("--round-timeout", type=int, default=75,
                     help="hard per-round subprocess timeout; must exceed real round latency (draft+repair+verify+replay, typically 55-65s) or rounds are killed mid-verify and produce nothing")
    ap.add_argument("--phase1-fraction", type=float, default=0.6,
                     help="fraction of --budget spent on LLM rounds before switching to mutation; raise this (or raise --budget) to fit more rounds, since each round costs ~60s minimum regardless of --round-timeout")
    ap.add_argument("--max-mutants-per-step", type=int, default=60)
    ap.add_argument("--max-requests", type=int, default=None,
                     help="cap total HTTP requests counted on both sides to this number, for an "
                          "apples-to-apples request-for-request comparison instead of a time-budget one. "
                          "Still bounded by --budget as a time backstop.")
    a = ap.parse_args()
    if urlparse(a.target).hostname not in ("127.0.0.1", "localhost"):
        sys.exit("loopback targets only")

    t0 = time.monotonic()
    left = lambda: a.budget - (time.monotonic() - t0)
    out = f"artifacts/llm_budget_loop/loop-{datetime.now():%Y%m%dT%H%M%SZ}"
    os.makedirs(out)

    runs, seqs_by_round = [], []
    llm_stats = {"drafts": 0, "first": 0, "post": 0}
    covered_paths = set()  # normalized operation paths already drafted-for, excluded on later rounds
    token = None

    phase1_end = a.budget * a.phase1_fraction
    print(f"[plan] phase 1 (LLM rounds, feedback-excluded) up to t={phase1_end:.0f}s, "
          f"then mutation+reproduction until t={a.budget}s", flush=True)

    round_n = 0
    phase1_sent = 0
    while round_n < a.max_rounds and (time.monotonic() - t0) + a.round_timeout < phase1_end:
        if a.max_requests and phase1_sent >= a.max_requests:
            print(f"[LLM round {round_n+1}] skipped: phase-1 requests already at cap "
                  f"({phase1_sent}/{a.max_requests})", flush=True)
            break
        round_n += 1
        print(f"[LLM round {round_n}] t={time.monotonic()-t0:.0f}s  "
              f"excluding {len(covered_paths)} already-covered path(s)", flush=True)
        cmd = [sys.executable, "scripts/run_llm_sequence_experiment.py",
               "--max-approved-sequences", "3", "--target-ip", urlparse(a.target).hostname or "127.0.0.1",
               "--target-port", str(urlparse(a.target).port or 80),
               "--host", (urlparse(a.target).hostname or "127.0.0.1") + ":" + str(urlparse(a.target).port or 80)]
        for p in covered_paths:
            cmd += ["--exclude-path", p]
        env = dict(os.environ)
        if token:
            env["CRAPI_BEARER_TOKEN"] = token
        try:
            subprocess.run(cmd, check=False, env=env, timeout=max(10, a.round_timeout))
        except subprocess.TimeoutExpired:
            print(f"[LLM round {round_n}] timed out after {a.round_timeout}s, moving on", flush=True)
            break
        rs = sorted(glob.glob("artifacts/llm_sequence_experiment/run-*"))
        r = rs[-1] if rs else None
        if not r or r in runs:
            print(f"[LLM round {round_n}] no new run produced, stopping LLM phase", flush=True)
            break
        runs.append(r)
        try:
            dv = json.load(open(f"{r}/report.json")).get("draft_validation", {})
            llm_stats["drafts"] += dv.get("draft_count", 0)
            llm_stats["first"] += dv.get("first_pass_valid_count", 0)
            llm_stats["post"] += dv.get("post_repair_valid_count", 0)
        except Exception:
            pass
        round_seqs = []
        for f in sorted(glob.glob(f"{r}/sequence-*/sequence.replay.txt")):
            s = read_replay(f)
            if s:
                round_seqs.append(s)
                for st in s:
                    covered_paths.add(norm(st["path"]).strip("/").replace("/{id}", ""))
        if not round_seqs:
            # no replay file (token mode deletes it) -- recover seeds from the network log instead
            for f in sorted(glob.glob(f"{r}/**/network.replay.*.txt", recursive=True)):
                text = open(f, errors="replace").read()
                reqs = re.findall(
                    r"Sending: '(\w+) (\S+) HTTP/1\.1\\r\\n.*?\\r\\n\\r\\n(\{.*?\})?'", text)
                steps = []
                for m, p, b in reqs:
                    try:
                        body = json.loads(b) if b else None
                    except ValueError:
                        body = None
                    steps.append({"method": m, "path": p, "body": body})
                    covered_paths.add(norm(p).strip("/").replace("/{id}", ""))
                if steps:
                    round_seqs.append(steps)
        seqs_by_round.append(round_seqs)
        # capture a token from this round's own network log for use in later rounds / mutation
        round_pairs = []
        for f in glob.glob(f"{r}/**/network.replay.*.txt", recursive=True):
            text = open(f, errors="replace").read()
            tok = extract_token(text)
            if tok:
                token = tok
            rp, _ = parse_logs([f])
            round_pairs += rp
        phase1_sent += len(round_pairs)

    seqs = [s for round_seqs in seqs_by_round for s in round_seqs]
    p1_pairs, _ = parse_logs([f for r in runs for f in glob.glob(f"{r}/**/network.replay.*.txt", recursive=True)])
    rows = list(p1_pairs)
    if not seqs:
        sys.exit("No approved sequences collected across any round; check Ollama/RESTler and crAPI reachability.")

    print(f"[mutation] {len(seqs)} approved sequence(s) across {round_n} round(s), "
          f"{'token captured' if token else 'no token'}, {left():.0f}s left", flush=True)

    seeds = []
    for seq in seqs:
        for st in seq:
            if a.max_requests and len(rows) >= a.max_requests:
                break
            code, txt = send(a.target, st, token)
            rows.append((f"{st['method']} {norm(st['path'])}", code))
            tok = extract_token(txt)
            if tok:
                token = tok
            seeds.append((st, token))

    gens = [(st, tok, iter(list(mutants(st["body"], random.Random(7)))[: a.max_mutants_per_step]))
            for st, tok in seeds]
    log = []
    while gens and left() > 10:
        if a.max_requests and len(rows) >= a.max_requests:
            break
        for g in list(gens):
            if left() <= 10 or (a.max_requests and len(rows) >= a.max_requests):
                break
            st, tok, it = g
            nxt = next(it, None)
            if nxt is None:
                gens.remove(g)
                continue
            label, body = nxt
            code, txt = send(a.target, st, tok, json.dumps(body).encode())
            ep = f"{st['method']} {norm(st['path'])}"
            rows.append((ep, code))
            if code >= 500 or code == 0:
                log.append({"endpoint": ep, "status": code, "mutation": label, "body": json.dumps(body)[:300],
                            "response": txt[:200], "_st": st, "_tok": tok, "_raw": json.dumps(body)})

    buckets = {}
    for h in log:
        if h["status"] >= 500:
            buckets.setdefault((h["endpoint"], h["status"]), h)
    leads = []
    for (ep, status), h in buckets.items():
        hits = sum(send(a.target, h["_st"], h["_tok"], h["_raw"].encode())[0] >= 500 for _ in range(3))
        leads.append({"endpoint": ep, "status": status, "mutation": h["mutation"], "reproduced": f"{hits}/3",
                      "reproducible": hits >= 2, "request_body": h["body"], "response_snippet": h["response"]})

    elapsed = time.monotonic() - t0
    if a.max_requests:
        rows = rows[: a.max_requests]
    S = stats(rows)
    repro = [l for l in leads if l["reproducible"]]
    report = {"budget_s": a.budget, "elapsed_s": round(elapsed, 1), "llm_rounds": round_n, "llm": llm_stats,
              "covered_paths": sorted(covered_paths), "http": S, "distinct_5xx_buckets": len(leads),
              "reproducible_5xx_leads": len(repro), "leads": leads}
    json.dump(report, open(f"{out}/loop_report.json", "w"), indent=2)

    base = None
    if a.baseline:
        bp, bw = parse_logs(glob.glob(f"{a.baseline}/**/network.testing.*.txt", recursive=True))
        if a.max_requests:
            bp = bp[: a.max_requests]
        base = (stats(bp), bw, len({(e, s) for e, s in bp if s >= 500}))

    def row(k, b, l):
        print(f"{k:<34}{str(b):<22}{l}")

    cap_note = f" (both capped at {a.max_requests} requests)" if a.max_requests else ""
    print(f"\n{'METRIC':<34}{'RESTler fuzz-lean':<22}{'LLM-seeded loop'}{cap_note}\n" + "-" * 78)
    bs = base[0] if base else {}
    for k, lab in [("requests", "HTTP requests sent"), ("2xx", "2xx responses"), ("4xx", "4xx responses"),
                   ("5xx", "5xx responses"), ("endpoints", "Distinct endpoints hit"),
                   ("5xx_endpoints", "Distinct endpoints w/ 5xx")]:
        row(lab, bs.get(k, "n/a"), S[k])
    row("Distinct 5xx buckets (ep,status)", base[2] if base else "n/a", len(leads))
    row("Reproducible 5xx leads (>=2/3)", "not measured", len(repro))
    if S["requests"]:
        row("Reproducible leads / 100 reqs", round(100 * (base[2] if base else 0) / bs.get("requests", 1), 2)
            if base else "n/a", round(100 * len(repro) / S["requests"], 2))
    row("Wall time (s)", round(base[1], 1) if base else "n/a", round(elapsed, 1))
    row("Requests / min", round(60 * bs["requests"] / base[1], 1) if base and base[1] else "n/a",
        round(60 * S["requests"] / elapsed, 1) if elapsed else "n/a")
    print("-" * 78)
    row("LLM rounds run", "n/a", round_n)
    row("LLM drafts valid 1st pass", "n/a", f"{llm_stats['first']}/{llm_stats['drafts']}")
    row("LLM drafts valid after repair", "n/a", f"{llm_stats['post']}/{llm_stats['drafts']}")
    print("\nCaveats: 5xx leads are untriaged candidates, not confirmed vulnerabilities. "
          "Baseline 5xx are not re-reproduced here.")
    print("Each LLM round excludes operations covered by earlier rounds, so rounds should cover new endpoints; "
          "mutation then probes every endpoint seen across all rounds.")
    print("Report:", f"{out}/loop_report.json\n")


main()