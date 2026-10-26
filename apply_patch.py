#!/usr/bin/env python3
"""Run from repo root: python apply_patch.py
Patches scripts/run_llm_sequence_experiment.py (backup saved as .bak-pre-outcomes)."""
import shutil, sys
p = "scripts/run_llm_sequence_experiment.py"
s = open(p).read()
if "def uniquify_identity" in s:
    sys.exit("Already patched.")
shutil.copy(p, p + ".bak-pre-outcomes")

helpers = '''
def uniquify_identity(sequence, tag):
    """Replace reused signup email/number/name with fresh per-run values (applied to all steps)."""
    import random
    rng = random.Random(tag)
    mapping = {}
    for step in sequence["steps"]:
        b = step.get("body")
        if step.get("operation_id") == "signup" and isinstance(b, dict):
            if isinstance(b.get("email"), str):
                mapping[b["email"]] = f"fz{tag}@example.com"
            if isinstance(b.get("number"), str):
                mapping[b["number"]] = "9" + "".join(str(rng.randint(0, 9)) for _ in range(9))
            if isinstance(b.get("name"), str):
                mapping[b["name"]] = f"Fuzz{tag}"
    def sub(v):
        if isinstance(v, str): return mapping.get(v, v)
        if isinstance(v, dict): return {k: sub(x) for k, x in v.items()}
        if isinstance(v, list): return [sub(x) for x in v]
        return v
    for step in sequence["steps"]:
        if step.get("body") is not None: step["body"] = sub(step["body"])
        if step.get("query"): step["query"] = sub(step["query"])
    return bool(mapping)


def parse_http_outcomes(seq_dir):
    """Pair Sending/Received entries in RESTler network.replay logs into real HTTP outcomes."""
    out = []
    for f in sorted(seq_dir.rglob("network.replay.*.txt")):
        text = f.read_text(encoding="utf-8", errors="replace")
        sends = re.findall(r"Sending: '(\\w+) (\\S+) HTTP", text)
        recvs = re.findall(r"Received: 'HTTP/1\\.1 (\\d{3})(.*?)(?:\\\\r\\\\n0\\\\r\\\\n|'\\n)", text, flags=re.S)
        for (m, path), (code, rest) in zip(sends, recvs):
            msg = re.search(r'"message":"([^"]{0,120})"', rest)
            out.append({"method": m, "path": path, "status": int(code), "message": msg.group(1) if msg else ""})
    return out

'''

def rep(old, new):
    global s
    if old not in s:
        sys.exit("Anchor not found: " + old[:60])
    s = s.replace(old, new, 1)

rep("def serialize_replay(", helpers + "\ndef serialize_replay(")
rep('            replay_file = seq_dir / "sequence.replay.txt"\n',
    '            uniq = uniquify_identity(sequence, f"{int(time.time())}{index}")\n'
    '            replay_file = seq_dir / "sequence.replay.txt"\n')
rep('            report["runs"].append({\n                "sequence_id": sequence["sequence_id"],\n                "status": status,',
    '            http_results = parse_http_outcomes(seq_dir)\n'
    '            report["runs"].append({\n                "sequence_id": sequence["sequence_id"],\n                "status": status,\n'
    '                "identity_uniquified": uniq,\n                "http_results": http_results,\n'
    '                "http_2xx": sum(200 <= r["status"] < 300 for r in http_results),\n'
    '                "http_4xx": sum(400 <= r["status"] < 500 for r in http_results),\n'
    '                "http_5xx": sum(r["status"] >= 500 for r in http_results),')
rep('''            print(f"  {item['sequence_id']}: {item['status']} (exit={item.get('restler_exit_code', 'n/a')})")''',
    '''            print(f"  {item['sequence_id']}: {item['status']} (exit={item.get('restler_exit_code', 'n/a')}) HTTP 2xx/4xx/5xx = {item.get('http_2xx',0)}/{item.get('http_4xx',0)}/{item.get('http_5xx',0)}")
            for r in item.get("http_results", []): print(f"      {r['method']} {r['path']} -> {r['status']} {r['message']}")''')
open(p, "w").write(s)
print("Patched OK. Backup:", p + ".bak-pre-outcomes")