#!/usr/bin/env python3
"""Side-by-side terminal comparison: RESTler baseline vs LLM-guided run, from real network logs.
Usage (repo root): python scripts/compare_results.py [--baseline DIR] [--llm RUN_DIR]
Defaults: newest fuzz-lean baseline log under artifacts/, newest llm_sequence_experiment run."""
import argparse, glob, json, os, re, sys
from datetime import datetime

def norm(path):
    path = path.split("?")[0]
    return re.sub(r"/([0-9a-fA-F-]{8,}|\d+)(?=/|$)", "/{id}", path)

def parse(files):
    sends, recv, ts = [], [], []
    for f in files:
        for l in open(f, errors="replace"):
            m = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+): (Sending|Received): '(?:(\w+) (\S+) HTTP|HTTP/1\.1 (\d{3}))", l)
            if not m: continue
            ts.append(datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S.%f"))
            if m.group(2) == "Sending": sends.append((m.group(3), norm(m.group(4))))
            else: recv.append(int(m.group(5)))
    pairs = list(zip(sends, recv))
    secs = (max(ts) - min(ts)).total_seconds() if len(ts) > 1 else 0.0
    return pairs, secs

def metrics(pairs, wall):
    n = len(pairs)
    ok = [p for p in pairs if 200 <= p[1] < 300]
    return {
        "HTTP requests sent": n,
        "2xx responses": len(ok),
        "4xx responses": sum(400 <= s < 500 for _, s in pairs),
        "5xx responses": sum(s >= 500 for _, s in pairs),
        "2xx rate": f"{100*len(ok)/n:.0f}%" if n else "n/a",
        "Distinct endpoints hit": len({e for e, _ in pairs}),
        "Distinct endpoints w/ 2xx": len({e for e, s in pairs if 200 <= s < 300}),
        "Distinct endpoints w/ 5xx (leads)": len({e for e, s in pairs if s >= 500}),
        "Wall time (s)": round(wall, 1),
        "Requests / min": round(60*n/wall, 1) if wall else "n/a",
    }

ap = argparse.ArgumentParser()
ap.add_argument("--baseline"); ap.add_argument("--llm")
a = ap.parse_args()
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")

def is_crapi(f):
    t = open(f, errors="replace").read()
    return "/identity/api" in t or "/workshop/api" in t
if a.baseline:
    bl = glob.glob(f"{a.baseline}/**/network.testing.*.txt", recursive=True)
else:
    cands = sorted(glob.glob("artifacts/**/network.testing.*.txt", recursive=True), key=os.path.getmtime)
    bl = [f for f in cands if is_crapi(f)][-1:]
if not bl:
    sys.exit("No crAPI baseline logs found under artifacts/ (the logs there are other targets, e.g. demo server/VAmPI,\n"
             "which would be an unfair comparison). Find yours with:\n"
             "  find ~/Documents /tmp -name 'network.testing.*.txt' 2>/dev/null | xargs grep -l '/identity/api'\n"
             "then rerun with:  python scripts/compare_results.py --baseline <dir containing that FuzzLean folder>")
if not all(is_crapi(f) for f in bl):
    print("WARNING: --baseline logs do not look like crAPI traffic; comparison may be unfair.\n")
run = a.llm or sorted(glob.glob("artifacts/llm_sequence_experiment/run-*"))[-1]
ll = glob.glob(f"{run}/**/network.replay.*.txt", recursive=True)
if not ll: sys.exit(f"No replay logs in {run}")

bp, bw = parse(bl); lp, lw = parse(ll)
rep = json.load(open(f"{run}/report.json"))
llm_total = rep.get("total_elapsed_seconds", lw)
bm, lm = metrics(bp, bw), metrics(lp, lw)
lm["Wall time (s)"] = f"{lw:.1f} replay / {llm_total:.1f} total"
lm["Requests / min"] = f"{60*len(lp)/lw:.1f} replay / {60*len(lp)/llm_total:.1f} total" if lw and llm_total else "n/a"
dv = rep.get("draft_validation", {})
w = max(len(k) for k in bm) + 2
print(f"\n{'METRIC':<{w}}{'RESTler fuzz-lean':<22}{'LLM-guided (3B+7B)'}")
print("-" * (w + 22 + 30))
for k in bm: print(f"{k:<{w}}{str(bm[k]):<22}{lm[k]}")
print("-" * (w + 22 + 30))
print(f"{'LLM drafts valid 1st pass':<{w}}{'n/a':<22}{dv.get('first_pass_valid_count','?')}/{dv.get('draft_count','?')}")
print(f"{'LLM drafts valid after repair':<{w}}{'n/a':<22}{dv.get('post_repair_valid_count','?')}/{dv.get('draft_count','?')}")
print(f"\nBaseline log: {bl[0]}\nLLM run:      {run}")
print("Caveats: 5xx = leads, not confirmed bugs. LLM run is a short fixed replay, not a fuzzing loop;")
print("         rates are descriptive only. Neither side has a verified unique-bug count.\n")