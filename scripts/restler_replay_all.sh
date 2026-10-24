#!/usr/bin/env bash
# Replay every real bug-bucket *.replay.txt through RESTler's own replay task (loopback only).
# Needs the demo server running. Env overrides: TARGET_PORT, RESTLER_BIN, RESTLER_PY.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 9
PORT="${TARGET_PORT:-8899}"
DLL="${RESTLER_BIN:-$ROOT/../restler_bin}/restler/Restler.dll"
PY="${RESTLER_PY:-$HOME/Documents/restler_venv/bin/python}"
D=$(ls -d "$ROOT"/artifacts/restler_demo/baseline_fuzz-lean_*/rep1/FuzzLean/RestlerResults/experiment*/bug_buckets | tail -1)
[ -d "$D" ] || { echo "no bug_buckets dir; run scripts/run_restler_baseline.sh first"; exit 2; }
OUT="$ROOT/artifacts/restler_demo/replay_all"; rm -rf "$OUT"; mkdir -p "$OUT"
for F in "$D"/*.replay.txt; do
  N=$(basename "$F" .replay.txt); W="$OUT/$N"; mkdir -p "$W"; cd "$W" || exit 9
  # target_ip/target_port in settings are required; --host alone gives a socket error
  printf '{"target_ip": "127.0.0.1", "target_port": %s}\n' "$PORT" > settings.json
  perl -e 'alarm shift; exec @ARGV' 120 dotnet "$DLL" --disable_log_upload --python_path "$PY" replay \
    --replay_log "$F" --settings settings.json --no_ssl > stdout.txt 2>&1
  echo "$N exit=$?"
  cd "$ROOT"
done
python3 "$ROOT/scripts/build_replay_comparison.py"
