#!/usr/bin/env zsh
# One-shot reproduction (smoke baseline). Run: zsh scripts/repro.sh   (demo server must be up on 8899)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 9
source .venv/bin/activate
echo "##### server check (expects an HTTP code, not 000)"
curl -s -o /dev/null -w "GET /api/blog/posts -> %{http_code}\n" http://127.0.0.1:8899/api/blog/posts
echo "##### 1 tests"
python -m pytest -q 2>&1 | tail -2; echo "exit: ${pipestatus[1]}"
echo "##### 2 baseline smoke (MODE=test REPS=1)"
MODE=test REPS=1 bash scripts/run_restler_baseline.sh 2>&1 | tail -3; echo "exit: ${pipestatus[1]}"
echo "##### 3 project e2e replay"
D=$(ls -d "$ROOT"/artifacts/restler_demo/baseline_fuzz-lean_*/rep1/FuzzLean/RestlerResults/experiment*/bug_buckets | tail -1)
PYTHONPATH=src python scripts/run_e2e_replay.py --replay-dir "$D" 2>&1 | tail -8; echo "exit: ${pipestatus[1]}"
echo "##### 4 RESTler own replay of all buckets"
bash scripts/restler_replay_all.sh 2>&1; echo "exit: ${pipestatus[1]}"
