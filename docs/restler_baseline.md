# RESTler baseline (local demo server)

Status: pipeline validation only. Not evidence that LLM-guided selection beats RESTler.

## Data flow (verified against RESTler 9.3.1)
OpenAPI -> `Restler.dll compile` -> `Compile/grammar.py` -> `Restler.dll test|fuzz-lean` (real requests to the
target) -> `<Mode>/RestlerResults/experiment*/{logs,bug_buckets}` + `<Mode>/ResponseBuckets` ->
`adaptive_restler.runner.restler_run` (parse, dedup by RESTler bucket hash, write record under `artifacts/`).

RESTler's generation phase builds its own sequences from the grammar; it cannot take an arbitrary operation list.
Supported narrowing: `--path_regex`. Selected sequences re-enter via real `*.replay.txt` files ->
`import_replay_logs` -> `CandidateSequence` -> existing selector/executor (executor wiring: `scripts/run_e2e_replay.py`).

## Method
Local loopback target only, fixed spec (sha256 recorded), fixed seed, `fuzz-lean`, N repetitions.
Attempted/completed requests come from `network.testing.*` + `network.gc.*` logs (gc = RESTler cleanup, reported
separately). Unique candidate bug = distinct RESTler bucket hash among reproduced buckets. Not confirmed.
4xx are not bugs; 422 from fuzzed payloads is expected. Requests with no response are reported, not classified.
Limitation: runs share one server instance with no state reset.

## Commands (repo root; demo server must be listening)
    # terminal A
    cd ../restler-fuzzer/demo_server/demo_server && ../../../restler_venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8899
    # terminal B
    source .venv/bin/activate
    python -m pytest -q
    bash scripts/run_restler_baseline.sh                  # fuzz-lean x3, seed 12345
    MODE=test REPS=1 bash scripts/run_restler_baseline.sh # smoke
Env overrides: RESTLER_BIN RESTLER_PY SPEC TARGET_PORT MODE REPS SEED TIMEOUT OUT TAG.

## End-to-end replay through the project pipeline
Real `*.replay.txt` bucket files -> `import_replay_logs` -> `DeterministicSelector` -> `execute_candidate`
(`UrllibSender`, loopback only) -> `validate_findings` (leads only) -> `compute_metrics` -> `persist_run`.

    D=$(ls -d artifacts/restler_demo/baseline_fuzz-lean_*/rep1/FuzzLean/RestlerResults/experiment*/bug_buckets | tail -1)
    PYTHONPATH=src python scripts/run_e2e_replay.py --replay-dir "$D"

Replay logs use literal ids from the original run, so a bucket may not reproduce against current server state.

<!-- observed-results -->
## Observed results (2026-10-25, local demo server, RESTler 9.3.1)

fuzz-lean x3, seed 12345, spec sha256 6b9088cdaf01f53d..., mode `fuzz-lean`.

- Harness OK in all runs: True
- Requests attempted per run: [790, 790, 790]; completed: [787, 787, 787] (includes RESTler gc cleanup traffic)
- Wall seconds mean/min/max: 3.412 / 3.243 / 3.716; requests per second mean: 232.399
- Unique candidate bugs (RESTler bucket hash): 5; identical hash set across runs: True
- Unique candidate bugs per minute mean/min/max: 88.253 / 80.732 / 92.507 (pipeline number; wall time includes process startup and the same buckets repeat every run)

Replay of the real bug-bucket files (rep1):

| bucket | recorded | project executor | RESTler replay | 5xx (executor) |
|---|---|---|---|---|
| InvalidValueChecker_500_1 | [500] | [500] | [500] | True |
| InvalidDynamicObjectChecker_20x_2 | [201, 204] | [201, 404] | [201, 404] | False |
| InvalidDynamicObjectChecker_20x_1 | [201, 200] | [201, 500] | [201, 500] | True |
| PayloadBodyChecker_500_1 | [201, 500] | [201, 500] | [201, 500] | True |
| UseAfterFreeChecker_500_1 | [201, 204, 500] | [201, 404, 500] | [201, 404, 500] | True |

RESTler's own `replay` task sends real requests only when `target_ip` and `target_port` are in the settings file (`--host` alone caused a socket error). Run without the grammar, it reuses the literal ids from the log, so only part of the buckets reproduce.

Project pipeline: requests_total=10, leads_unique=3 (executor fingerprint: category|method|path template, a different identity than RESTler hashes), harness_errors=[].

Caveats: candidates and leads are unvalidated, none confirmed. Demo server only; this validates the pipeline and says nothing about selection quality vs RESTler. Replay logs hold literal ids, so reproduction can diverge.
