#!/usr/bin/env python3
import argparse
import json
import re
from pathlib import Path
from datetime import datetime

def norm(path):
    path = path.split("?", 1)[0]
    return re.sub(r"/([0-9a-fA-F-]{8,}|\d+)(?=/|$)", "/{id}", path)

def stats(requests, responses, pairs):
    codes = [code for _, code in pairs]
    endpoints = set(requests)
    five = [(endpoint, code) for endpoint, code in pairs if 500 <= code < 600]
    total_responses = len(codes)
    return {
        "requests": len(requests),
        "2xx": sum(200 <= c < 300 for c in codes),
        "4xx": sum(400 <= c < 500 for c in codes),
        "5xx": sum(500 <= c < 600 for c in codes),
        "endpoints": len(endpoints),
        "5xx_endpoints": len({e for e, _ in five}),
        "5xx_buckets": len(set(five)),
        "2xx_rate": 100 * sum(200 <= c < 300 for c in codes) / total_responses if total_responses else 0,
        "4xx_rate": 100 * sum(400 <= c < 500 for c in codes) / total_responses if total_responses else 0,
        "5xx_rate": 100 * sum(500 <= c < 600 for c in codes) / total_responses if total_responses else 0,
    }

def parse_baseline(root):
    files = sorted(root.rglob("network.testing.*.txt"))
    if not files:
        raise SystemExit(f"No network.testing logs found under {root}")

    requests, pairs, times = [], [], []
    for f in files:
        sends, receives = [], []
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            tm = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+):", line)
            if tm:
                try:
                    times.append(datetime.strptime(tm.group(1), "%Y-%m-%d %H:%M:%S.%f"))
                except ValueError:
                    pass

            if "Sending:" in line:
                m = re.search(r"Sending:\s*'(\w+)\s+(\S+)\s+HTTP", line)
                if m:
                    sends.append(f"{m.group(1)} {norm(m.group(2))}")

            if "Received:" in line:
                m = re.search(r"Received:\s*'HTTP/\d\.\d\s+(\d{3})", line)
                if m:
                    receives.append(int(m.group(1)))

        requests.extend(sends)
        pairs.extend(zip(sends, receives))

    elapsed = (max(times) - min(times)).total_seconds() if len(times) > 1 else 0
    return stats(requests, [(e, c) for e, c in pairs], pairs), elapsed

def parse_oneshot(root):
    report_path = root / "report.json"
    if not report_path.is_file():
        raise SystemExit(f"Report not found: {report_path}")
    report = json.loads(report_path.read_text(encoding="utf-8"))

    requests, pairs = [], []
    for f in sorted(root.rglob("network.replay.*.txt")):
        text = f.read_text(encoding="utf-8", errors="replace")
        sends = [
            f"{method} {norm(path)}"
            for method, path in re.findall(
                r"Sending:\s*'(\w+)\s+(\S+)\s+HTTP/\d\.\d", text
            )
        ]
        receives = [
            int(code)
            for code in re.findall(
                r"Received:\s*'HTTP/\d\.\d\s+(\d{3})", text
            )
        ]
        requests.extend(sends)
        pairs.extend(zip(sends, receives))

    result = stats(requests, [], pairs)
    result["elapsed"] = float(report.get("total_elapsed_seconds") or 0)
    dv = report.get("draft_validation") or {}
    result["drafts"] = dv.get("draft_count", 0)
    result["first_valid"] = dv.get("first_pass_valid_count", 0)
    result["final_valid"] = dv.get("post_repair_valid_count", dv.get("first_pass_valid_count", 0))
    result["repairs"] = dv.get("repair_calls_made", len(report.get("repair_calls", [])))
    return result

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", default="~/Documents/crapi-restless-baseline/results/benchmark/standard/run1")
    ap.add_argument("--oneshot", default="artifacts/llm_sequence_experiment/run-20261025T014831Z")
    args = ap.parse_args()

    baseline_path = Path(args.baseline).expanduser().resolve()
    oneshot_path = Path(args.oneshot).expanduser().resolve()

    b, btime = parse_baseline(baseline_path)
    n = parse_oneshot(oneshot_path)

    def fmt_num(x):
        return f"{x:,}" if isinstance(x, int) else str(x)

    rows = [
        ("HTTP requests sent", "requests"),
        ("2xx responses", "2xx"),
        ("4xx responses", "4xx"),
        ("5xx responses", "5xx"),
        ("2xx rate", "2xx_rate"),
        ("4xx rate", "4xx_rate"),
        ("5xx rate", "5xx_rate"),
        ("Distinct endpoints hit", "endpoints"),
        ("Distinct endpoints w/ 5xx", "5xx_endpoints"),
        ("Distinct 5xx buckets (ep,status)", "5xx_buckets"),
    ]

    print(f"\n{'METRIC':<38}{'RESTler fuzz-lean':<23}{'LLM one-shot'}")
    print("-" * 82)
    for label, key in rows:
        bv, nv = b[key], n[key]
        if key.endswith("_rate"):
            bv, nv = f"{bv:.1f}%", f"{nv:.1f}%"
        print(f"{label:<38}{fmt_num(bv):<23}{fmt_num(nv)}")

    nrpm = 60 * n["requests"] / n["elapsed"] if n["elapsed"] else 0
    brpm = 60 * b["requests"] / btime if btime else 0
    print(f"{'Reproducible 5xx leads (>=2/3)':<38}{'not measured':<23}{'not measured'}")
    print(f"{'Wall time (s)':<38}{btime:<23.1f}{n['elapsed']:.1f}")
    print(f"{'Requests / min':<38}{brpm:<23.1f}{nrpm:.1f}")
    print("-" * 82)
    print(f"{'LLM drafts valid 1st pass':<38}{'n/a':<23}{n['first_valid']}/{n['drafts']}")
    print(f"{'LLM drafts valid after final validation':<38}{'n/a':<23}{n['final_valid']}/{n['drafts']}")
    print(f"{'Repair calls':<38}{'n/a':<23}{n['repairs']}")

    print("\nCaveats:")
    print("- One-shot metrics come from the existing report and RESTler replay logs.")
    print("- RESTler wall time is measured from network-log timestamps.")
    print("- Reproducibility was not measured in this table.")
    print("- 5xx responses are leads, not confirmed vulnerabilities.")
    print(f"\nBaseline: {baseline_path}")
    print(f"One-shot: {oneshot_path}")

if __name__ == "__main__":
    main()  