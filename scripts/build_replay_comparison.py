"""Compare recorded statuses in RESTler *.replay.txt files with statuses seen when RESTler replays them.
Reads artifacts/restler_demo/replay_all/<bucket>/Replay/... and writes
artifacts/restler_demo/replay_check/replay_comparison.json. Run from anywhere."""
import glob, json, os, re, pathlib
os.chdir(pathlib.Path(__file__).resolve().parent.parent)
root = "artifacts/restler_demo"
d = sorted(glob.glob(f"{root}/baseline_fuzz-lean_*/rep1/FuzzLean/RestlerResults/experiment*/bug_buckets"))[-1]
res = {}
for f in sorted(glob.glob(os.path.join(d, "*.replay.txt"))):
    n = os.path.basename(f)[: -len(".replay.txt")]
    rec = []
    for l in open(f, errors="replace"):
        m = re.match(r"PREVIOUS RESPONSE: 'HTTP/1\.1 (\d{3})", l)
        if m:
            rec.append(int(m.group(1)))
    sends, got = [], []
    for p in glob.glob(f"{root}/replay_all/{n}/Replay/RestlerResults/*/logs/network.replay.*.txt"):
        for l in open(p, errors="replace"):
            m = re.search(r": Sending: '([A-Z]+) (\S+)", l)
            if m:
                sends.append(f"{m.group(1)} {m.group(2)}")
            m = re.search(r": Received: 'HTTP/1\.1 (\d{3})", l)
            if m:
                got.append(int(m.group(1)))
    res[n] = {"steps": sends, "restler_replay_statuses": got, "recorded_statuses": rec, "match": bool(got) and got == rec}
os.makedirs(f"{root}/replay_check", exist_ok=True)
json.dump(res, open(f"{root}/replay_check/replay_comparison.json", "w"), indent=2)
for n, r in res.items():
    print(f"{n}: recorded={r['recorded_statuses']} restler_replay={r['restler_replay_statuses']} match={r['match']}")
print("matched:", sum(r["match"] for r in res.values()), "of", len(res))
