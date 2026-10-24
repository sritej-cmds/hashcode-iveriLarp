#!/usr/bin/env bash
# Bounded RESTler baseline against a LOCAL demo target. Refuses non-loopback hosts.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT" || exit 2
RESTLER_BIN="${RESTLER_BIN:-$ROOT/../restler_bin}"
RESTLER_PY="${RESTLER_PY:-$HOME/Documents/restler_venv/bin/python}"
SPEC="${SPEC:-$ROOT/../restler-fuzzer/demo_server/swagger.json}"
TARGET_HOST="${TARGET_HOST:-127.0.0.1}"; TARGET_PORT="${TARGET_PORT:-8899}"
HEALTH_PATH="${HEALTH_PATH:-/api/blog/posts}"
MODE="${MODE:-fuzz-lean}"; REPS="${REPS:-3}"; SEED="${SEED:-12345}"; TIMEOUT="${TIMEOUT:-600}"
OUT="${OUT:-$ROOT/artifacts/restler_demo}"
TAG="${TAG:-$(date -u +%Y%m%dT%H%M%SZ)}"
PROJ_PY="${PROJ_PY:-python}"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
DLL="$RESTLER_BIN/restler/Restler.dll"

case "$TARGET_HOST" in 127.0.0.1|localhost) ;; *) echo "refusing non-loopback target: $TARGET_HOST" >&2; exit 2;; esac
case "$MODE" in test|fuzz-lean) ;; *) echo "MODE must be test or fuzz-lean" >&2; exit 2;; esac

# stage 1: spec available and parseable
"$PROJ_PY" -c "import json,sys; assert 'paths' in json.load(open(sys.argv[1]))" "$SPEC" || { echo "spec missing/invalid: $SPEC" >&2; exit 3; }
# stage 3: target reachable
code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$TARGET_HOST:$TARGET_PORT$HEALTH_PATH")
[ "$code" = "000" ] && { echo "target unreachable: $TARGET_HOST:$TARGET_PORT" >&2; exit 4; }
echo "target reachable: HTTP $code"

# stage 2: compile (skipped if already compiled)
mkdir -p "$OUT"
if [ ! -f "$OUT/Compile/grammar.py" ]; then
  ( cd "$OUT" && cp -n "$SPEC" ./swagger.json && dotnet "$DLL" compile --api_spec ./swagger.json > compile_stdout.txt 2>&1 )
fi
[ -f "$OUT/Compile/grammar.py" ] || { echo "compile failed, see $OUT/compile_stdout.txt" >&2; exit 5; }
RV=$(dotnet "$DLL" --version 2>&1 | head -1 | sed 's/RESTler version: //')
echo "restler=$RV mode=$MODE reps=$REPS seed=$SEED tag=$TAG"

BASE="$OUT/baseline_${MODE}_${TAG}"; mkdir -p "$BASE"
RECS=()
now() { perl -MTime::HiRes=time -e 'printf "%.3f", time'; }
for i in $(seq 1 "$REPS"); do
  RUN="$BASE/rep$i"; mkdir -p "$RUN"
  printf '{\n  "random_seed": %s,\n  "max_request_execution_time": 10,\n  "max_combinations": 20\n}\n' "$SEED" > "$RUN/settings.json"
  t0=$(now); s0=$(date -u +%FT%TZ)
  ( cd "$RUN" && perl -e 'alarm shift; exec @ARGV' "$TIMEOUT" dotnet "$DLL" \
      --disable_log_upload --python_path "$RESTLER_PY" "$MODE" \
      --grammar_file "$OUT/Compile/grammar.py" --dictionary_file "$OUT/Compile/dict.json" \
      --settings settings.json --target_ip "$TARGET_HOST" --target_port "$TARGET_PORT" --no_ssl \
      --host "localhost:$TARGET_PORT" > restler_stdout.txt 2>&1 )
  rc=$?
  t1=$(now); s1=$(date -u +%FT%TZ)
  wall=$(perl -e "printf '%.3f', $t1 - $t0")
  REL="${RUN#$ROOT/}"
  rec=$("$PROJ_PY" -m adaptive_restler.runner.restler_run summarize \
    --run-dir "$REL" --mode "$MODE" --run-id "restler-demo-${MODE}-${TAG}-rep$i" \
    --target-host "$TARGET_HOST" --target-port "$TARGET_PORT" --spec "$SPEC" \
    --restler-version "$RV" --seed "$SEED" --started-utc "$s0" --ended-utc "$s1" \
    --wall-seconds "$wall" --exit-code "$rc" --artifact-root "$ROOT/artifacts")
  echo "rep$i restler_exit=$rc wall=${wall}s record=$rec"
  [ -n "$rec" ] && RECS+=("$rec")
done
if [ "${#RECS[@]}" -gt 0 ]; then
  "$PROJ_PY" -m adaptive_restler.runner.restler_run aggregate --records "${RECS[@]}" \
    --run-id "restler-demo-${MODE}-${TAG}" --artifact-root "$ROOT/artifacts"
fi
