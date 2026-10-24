# Shared Interfaces and Contracts

**Contract version:** 1.1  
**Status:** Proposed implementation baseline, to be frozen before parallel coding  
**Owners:** Person A — Adaptive Planner; Person B — Execution and Evaluation

This document defines the public data models, function signatures, module ownership, and error behavior shared by the planner and runner.

Both contributors must implement these contracts. Internal implementation details may differ, but public behavior must remain compatible.

## 1. Package and ownership

Use the Python package `adaptive_restler` under `src/`.

### Person A — Planner

Owns:
- `src/adaptive_restler/planner/schemas.py`
- `src/adaptive_restler/planner/provider.py`
- `src/adaptive_restler/planner/prompts.py`
- `src/adaptive_restler/planner/scoring.py`
- `src/adaptive_restler/planner/feedback.py`

Person A is responsible for assessment, deterministic ranking, and feedback-based reprioritization.

### Person B — Execution and Evaluation

Owns:
- `src/adaptive_restler/runner/restler_adapter.py`
- `src/adaptive_restler/runner/executor.py`
- `src/adaptive_restler/runner/budget.py`
- `src/adaptive_restler/validation/findings.py`
- `src/adaptive_restler/evaluation/metrics.py`
- `src/adaptive_restler/evaluation/benchmark.py`
- `src/adaptive_restler/evaluation/report.py`

Person B is responsible for safe execution, budget enforcement, finding validation, benchmarking, and reports.

### Shared ownership

Both contributors review changes to:
- `src/adaptive_restler/models.py`
- `src/adaptive_restler/config.py`
- This document and `docs/decisions.md`
- Contract and end-to-end tests

No contributor may change a shared public interface unilaterally.

## 2. Shared conventions

- Python 3.11+ and Pydantic v2.
- Use snake_case for field and function names.
- Use UTC-aware `datetime` values for timestamps.
- Use unique string IDs for candidates, runs, and findings.
- Scores are finite numbers in their documented ranges.
- All persisted records must be JSON-serializable through Pydantic's JSON serialization.
- Artifact references are relative to the configured run-artifact directory.
- Never store API keys, passwords, bearer tokens, or other secrets in candidate data, model prompts, or experiment artifacts.
- Candidate headers may contain only non-sensitive test headers. Authentication is supplied through a configured authentication profile, not through the LLM.
- All lists and maps must have documented ordering or serialization behavior where that affects reproducibility.
- Unknown or invalid LLM output must be validated before it is used.
- The LLM cannot change the target, authentication profile, allowlist, execution budget, or safety configuration.

## 3. Shared data models

The following are the required logical fields. Python implementations must use Pydantic models and appropriate types, constraints, and defaults.

### 3.1 `CandidateRequest`

Represents one ordered API request within a candidate sequence.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `method` | `str` | Yes | HTTP method, normalized to uppercase |
| `path` | `str` | Yes | Path relative to the configured target |
| `headers` | `dict[str, str]` | No | Non-sensitive headers only; default empty |
| `query` | `dict[str, Any]` | No | Query parameters; default empty |
| `body` | `Any \| None` | No | Synthetic test request body |
| `expected_statuses` | `list[int]` | No | Expected statuses when known; default empty |

Rules:
- The path must not specify a scheme or hostname.
- Target host selection is not part of this model.
- Credentials and secrets must not be embedded in headers or bodies.
- Preserve request order.

### 3.2 `CandidateSequence`

Represents an executable, ordered sequence of API requests.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `candidate_id` | `str` | Yes | Stable unique candidate identifier |
| `sequence` | `list[CandidateRequest]` | Yes | Ordered requests; non-empty |
| `source` | `Literal["restler", "imported", "generated"]` | Yes | Candidate origin |
| `tags` | `list[str]` | No | Semantic tags; default empty |
| `estimated_request_count` | `int` | Yes | Estimated number of requests; at least 1 |
| `auth_profile_id` | `str \| None` | No | Reference to configured test authentication |
| `metadata` | `dict[str, Any]` | No | Non-sensitive metadata; default empty |

Rules:
- The same candidate ID must refer to the same normalized sequence within an experiment.
- Candidate identity must not depend on an LLM-generated rationale.
- `auth_profile_id` is a reference, not a credential.
- The runner must reject unknown or unauthorized authentication profiles.
- Estimated request count may differ from the number of sequence steps if one step expands into multiple requests; the adapter must document such expansion.

### 3.3 `CandidateAssessment`

Represents the LLM or fallback provider's assessment. It is not a security finding.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `candidate_id` | `str` | Yes | ID of the assessed candidate |
| `security_hypotheses` | `list[str]` | Yes | Testable security hypotheses; may be empty |
| `rationale` | `str` | Yes | Concise explanation of the assessment |
| `predicted_discovery_score` | `float` in `[0,1]` | Yes | Estimated chance/value signal for discovering a new valid issue; not calibrated by default |
| `expected_impact_score` | `float` in `[0,1]` | Yes | Estimated impact if a new issue is confirmed |
| `confidence` | `float` in `[0,1]` | Yes | Provider's self-reported confidence, not a calibrated probability |
| `missing_information` | `list[str]` | No | Information gaps; default empty |
| `provider_id` | `str` | Yes | Provider/model identifier |
| `prompt_version` | `str` | Yes | Prompt/schema version used |

Rules:
- The candidate ID must match a submitted candidate.
- Scores must be finite and within range.
- Missing or malformed assessments must be handled through the documented fallback, not silently treated as high-value candidates.
- The assessment cannot mark a vulnerability as confirmed or assign final severity.

### 3.4 `CandidateCost`

Contains cost estimates used by the deterministic ranker.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `candidate_id` | `str` | Yes | Candidate being estimated |
| `estimated_request_count` | `int` | Yes | At least 1 |
| `estimated_execution_seconds` | `float` | Yes | Non-negative execution estimate |
| `estimated_planner_seconds` | `float` | Yes | Non-negative assessment/ranking overhead estimate |
| `estimated_inference_cost` | `float \| None` | No | Non-negative cost in a configured, documented unit |

Rules:
- The unit of inference cost must be consistent within a run.
- Missing inference cost must be explicitly represented as `None`.
- Missing cost components must be omitted and the remaining weights renormalized.
- Unknown cost must never silently become zero.

### 3.5 `ExecutionStatus`

Allowed values:

- `completed`
- `failed`
- `timed_out`
- `budget_exhausted`
- `skipped`

A candidate is not considered successfully executed merely because a process was started.

### 3.6 `ExecutionResult`

Represents the outcome of one candidate execution attempt.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `run_id` | `str` | Yes | Experiment/run ID |
| `candidate_id` | `str` | Yes | Executed candidate |
| `status` | `ExecutionStatus` | Yes | Execution outcome |
| `started_at` | `datetime` | Yes | UTC-aware start time |
| `duration_seconds` | `float` | Yes | Non-negative duration |
| `request_count` | `int` | Yes | Actual requests sent; at least 0 |
| `responses` | `list[dict[str, Any]]` | No | Sanitized response summaries; default empty |
| `error_summary` | `str \| None` | No | Sanitized error description |
| `evidence_paths` | `list[str]` | No | Relative paths to saved evidence; default empty |
| `budget_exhausted` | `bool` | Yes | Whether the budget stopped execution |

Rules:
- The runner owns this model's production.
- The result must be emitted for success, failure, timeout, and budget exhaustion whenever a run has started.
- Response summaries must not contain credentials or unnecessary sensitive data.
- Evidence paths must remain inside the configured artifact directory.
- Actual request count must be measured, not copied from the estimate.

### 3.7 `FindingStatus`

Allowed values:

- `lead`
- `needs_review`
- `confirmed`
- `rejected`

### 3.8 `Finding`

Represents a security observation and its validation state.

| Field | Type | Required | Meaning |
|---|---|---|---|
| `finding_id` | `str` | Yes | Stable unique finding ID |
| `run_id` | `str` | Yes | Run where the observation occurred |
| `candidate_id` | `str` | Yes | Candidate associated with the observation |
| `category` | `str` | Yes | Suspected issue category |
| `status` | `FindingStatus` | Yes | Validation state |
| `impact_score` | `int \| None` | No | Internal score in `[0,28]`, if assessed |
| `severity` | `Literal["informational", "low", "medium", "high", "critical"] \| None` | No | Human-reviewed severity |
| `evidence_paths` | `list[str]` | No | Supporting evidence references; default empty |
| `validation_notes` | `str` | Yes | Rationale and review notes |
| `fingerprint` | `str \| None` | No | Stable issue fingerprint set by the validator; default `None` (v1.1) |

Rules:
- The validator owns the initial finding classification.
- An HTTP status code alone cannot create a `confirmed` finding.
- Final severity must have supporting evidence and human review.
- A lead may have no impact score or severity.
- Deduplication must preserve links to all relevant runs and evidence.
- `impact_score` is an internal rubric, not CVSS.

## 4. Additional shared run and budget models

### 4.1 `ExecutionBudget`

Configuration for an experiment's execution limits.

Required fields:
- `max_requests: int`, greater than zero.
- `max_runtime_seconds: float`, greater than zero.
- `request_timeout_seconds: float`, greater than zero.

The runner may define an internal mutable budget state for tracking consumption. That state is runner-owned and must not be mutated by the planner.

### 4.2 `TargetConfig`

Required logical fields:
- `base_url`
- `allowed_hosts`
- `auth_profile_id`, optional
- `tls_verification_enabled`
- `dry_run`

Rules:
- Construct and validate this configuration outside the LLM.
- The base URL must be on the allowlist.
- Reject requests that attempt to escape the configured host.
- Do not disable TLS verification by default.
- Local benchmark mode should be the default during development.
- An invalid target must fail before execution begins.

### 4.3 `ExperimentContext`

Contains the shared context for one run:
- `run_id`
- `target_config`
- `execution_budget`
- `artifact_dir`
- `benchmark_id`
- `configuration_version`

Credentials themselves must not be stored in this context or persisted with the run. Authentication profile IDs may refer to a separately configured credential source.

### 4.4 `RankedCandidate`

The planner's ranking output for one candidate.

Required fields:
- `candidate_id`
- `expected_value`
- `normalized_cost`
- `priority_score`
- `rank`
- `reasons`

All scores must be finite. `rank` starts at 1. `reasons` must make the decision explainable without exposing secrets.

## 5. Public function contracts

The following functions define the public module boundaries. Internal helper functions may be added freely.

### 5.1 Person A — assessment

```python
def assess_candidates(
    candidates: list[CandidateSequence],
    provider: AssessmentProvider,
    context: PlannerContext,
) -> list[CandidateAssessment]:
    ...
```

Requirements:
- Return assessments keyed by `candidate_id`.
- Never execute API requests.
- Validate provider output.
- Bound retries, input size, output size, and latency.
- Use the configured fallback for unavailable or invalid assessments.
- Record provider errors in structured logs.
- Do not let provider output alter execution safety settings.

### 5.2 Person A — ranking

```python
def rank_candidates(
    candidates: list[CandidateSequence],
    assessments: list[CandidateAssessment],
    costs: list[CandidateCost],
    history: PlannerHistory,
    config: ScoringConfig,
) -> list[RankedCandidate]:
    ...
```

Requirements:
- Ranking must be deterministic for identical validated inputs and configuration.
- The ranker must not make network calls.
- The ranker must not start RESTler or any subprocess.
- Validate that IDs match across candidates, assessments, and costs.
- Handle missing assessments and costs according to the fallback policy.
- Use deterministic tie-breaking: priority descending, expected value descending, estimated execution time ascending, candidate ID ascending.
- Return the full ranking; selection under remaining budget is performed by the orchestration/budget layer.

### 5.3 Person A — feedback

```python
def update_history(
    history: PlannerHistory,
    results: list[ExecutionResult],
    findings: list[Finding],
) -> PlannerHistory:
    ...
```

Requirements:
- Record execution outcomes and validated finding information.
- Distinguish no finding observed from a candidate that was not executed.
- Avoid repeatedly rewarding duplicate findings.
- Preserve the original assessment and score history.
- Return updated history; do not mutate runner-owned execution records.
- Feedback rules must be documented and tested.

### 5.4 Person B — execution

```python
def execute_candidate(
    candidate: CandidateSequence,
    context: ExperimentContext,
    budget_state: BudgetState,
) -> ExecutionResult:
    ...
```

Requirements:
- Validate target authorization before execution.
- Enforce request, runtime, and per-request time limits.
- Do not accept target overrides from the candidate or LLM.
- Record actual requests and execution duration.
- Save sanitized evidence and return paths relative to the artifact directory.
- Return structured results for failures and timeouts.
- Never classify a vulnerability as confirmed in this function.

### 5.5 Person B — validation

```python
def validate_findings(
    result: ExecutionResult,
    context: ValidationContext,
) -> list[Finding]:
    ...
```

Requirements:
- Use explicit validation rules and reproducible evidence.
- A 5xx response, timeout, or unexpected status alone cannot establish confirmation.
- Return leads or review-required findings when evidence is insufficient.
- Keep confirmation rules independent of the LLM's confidence score.
- Preserve evidence references and validation rationale.

### 5.6 Person B — benchmark execution

```python
def run_benchmark(
    config: BenchmarkConfig,
) -> BenchmarkReport:
    ...
```

Requirements:
- Record the immutable run configuration and software versions.
- Apply the configured reset strategy before comparable runs.
- Enforce the same budget policy across compared approaches.
- Preserve run IDs, candidate IDs, results, findings, and metrics.
- Report errors and incomplete runs rather than silently dropping them.

## 6. Error behavior

| Situation | Required behavior |
|---|---|
| Invalid candidate schema | Reject before execution; report validation errors |
| LLM timeout | Bounded retry, then fallback or mark assessment unavailable |
| Malformed LLM response | Reject, log sanitized error, then fallback |
| Missing candidate assessment | Use documented fallback; never assume maximum value |
| Missing cost component | Omit and renormalize cost weights, if remaining components are valid |
| Invalid or unauthorized target | Fail before sending requests |
| Request timeout | Stop or safely terminate the request and record `timed_out` |
| Request budget exhausted | Stop scheduling requests and record budget exhaustion |
| Runner subprocess fails | Record `failed` and preserve available diagnostic artifacts |
| Evidence insufficient | Do not mark the finding confirmed |
| Report generation fails | Preserve raw artifacts and report-generation error |

Exceptions may be used internally, but public orchestration boundaries must convert expected operational failures into structured results or clearly typed errors.

## 7. Ranking policy

Use the initial scoring policy documented in `docs/scoring.md`:

`ExpectedValue = predicted_discovery_score * expected_impact_score * novelty_factor`

`Priority = ExpectedValue / (epsilon + normalized_cost)`

Novelty comes from deterministic history/deduplication logic, not an unverified LLM claim.

Default cost weights:
- Requests: 0.35
- Execution time: 0.35
- Planner latency: 0.20
- Inference cost: 0.10

These are initial experimental defaults. They are not calibrated or proven optimal.

## 8. Shared integration flow

1. Candidate ingestion produces validated `CandidateSequence` objects.
2. Person A's assessment function produces `CandidateAssessment` objects.
3. Cost estimation produces `CandidateCost` objects.
4. Person A's ranker returns `RankedCandidate` objects.
5. The orchestration layer chooses the next candidate subject to remaining budget.
6. Person B's executor returns an `ExecutionResult`.
7. Person B's validator returns zero or more `Finding` objects.
8. Person A's feedback function updates planner history.
9. The next ranking uses the updated history.
10. The evaluation layer aggregates records and creates a report.

The first end-to-end test must use a mock assessment provider and fake executor. Real LLM and RESTler integrations come afterward.

## 9. Required contract tests

Both contributors must support tests for:
- Model validation and invalid ranges.
- Stable IDs and cross-reference consistency.
- JSON serialization and deserialization.
- Deterministic ranking and tie-breaking.
- Missing/invalid assessments and fallback behavior.
- Missing cost components and weight renormalization.
- Request/time budget enforcement.
- Unauthorized target rejection.
- Execution failures and partial artifacts.
- HTTP errors not automatically confirming findings.
- Feedback distinguishing executed, failed, skipped, and unexecuted candidates.
- End-to-end flow from candidates through ranking, execution, validation, and feedback.

## 10. Change control

A contract change requires:
1. A clear explanation of why the current contract is insufficient.
2. Identification of affected models, functions, modules, and tests.
3. Agreement from Person A and Person B.
4. Updates to this document and `docs/decisions.md`.
5. Contract tests updated before dependent implementations are merged.

Until both contributors agree, treat this version as the shared implementation baseline and do not independently redefine the public interfaces.
## 11. Supporting model contracts

These supporting types complete the public function signatures in Section 5. They must also be defined before implementation.

### 11.1 Shared model location

All shared persisted data models are defined once in:

`src/adaptive_restler/models.py`

This includes:
- `CandidateRequest`
- `CandidateSequence`
- `CandidateAssessment`
- `CandidateCost`
- `ExecutionStatus`
- `ExecutionResult`
- `FindingStatus`
- `Finding`
- `ExecutionBudget`
- `TargetConfig`
- `ExperimentContext`
- `RankedCandidate`
- `PlannerContext`
- `ScoringConfig`
- `CandidateOutcomeSummary`
- `PlannerHistory`
- `BudgetState`
- `ValidationContext`
- `BenchmarkConfig`
- `BenchmarkReport`

(`AssessmentProvider` is a Protocol, not a persisted model; it lives in `planner/provider.py`.)

`planner/schemas.py` may define provider-specific response parsing or import/re-export shared models, but it must not create duplicate competing definitions of these shared models.

### 11.2 `PlannerContext`

Required fields:
- `run_id: str`
- `prompt_version: str`
- `max_candidates_per_assessment: int`
- `assessment_timeout_seconds: float`

Rules:
- Limits must be positive.
- No credentials or secrets may be stored in this model.
- It must not contain permission to override target or budget settings.

### 11.3 `ScoringConfig`

Required fields:
- `request_weight: float`
- `execution_time_weight: float`
- `planner_latency_weight: float`
- `inference_cost_weight: float`
- `epsilon: float`
- `request_budget: float` (v1.1)
- `execution_time_budget_seconds: float` (v1.1)
- `planner_latency_budget_seconds: float` (v1.1)
- `inference_cost_budget: float | None` (v1.1)

Rules:
- Weights must be non-negative and sum to 1 within a small numeric tolerance.
- Epsilon must be positive.
- Invalid configurations must fail before ranking begins.
- Missing cost components require documented renormalization.
- The four `*_budget` fields are the denominators of the normalized cost formula. The orchestration layer fills `request_budget` and `execution_time_budget_seconds` from `ExecutionBudget` (`max_requests`, `max_runtime_seconds`). If `inference_cost_budget` is `None`, the inference component is omitted and the remaining weights are renormalized.

### 11.4 `PlannerHistory`

Required fields:
- `executed_candidate_ids: set[str]`
- `outcomes_by_candidate: dict[str, CandidateOutcomeSummary]`
- `confirmed_finding_fingerprints: set[str]`
- `policy_version: str`

`CandidateOutcomeSummary` contains:
- `execution_attempts: int`
- `successful_attempts: int`
- `failed_attempts: int`
- `confirmed_finding_fingerprints: list[str]`
- `last_status: ExecutionStatus | None`

Rules:
- Skipped or never-executed candidates must not be treated as unsuccessful security tests.
- Repeated findings must not be counted as new discoveries.
- Sets must be converted to a stable sorted representation before persistence.
- Feedback must not overwrite the original assessment.

### 11.5 `BudgetState`

Runner-owned mutable state for one experiment.

Required fields:
- `run_id: str`
- `requests_used: int`
- `started_at: datetime`
- `stop_reason: str | None`

Rules:
- The runner is the only component allowed to mutate budget state.
- The runner must check limits before dispatching each request.
- Request accounting must be updated from actual dispatched requests.
- The state must not allow negative request counts.
- Concurrent execution, if introduced later, must use atomic budget accounting. The first version should use sequential execution to avoid this complexity.

### 11.6 `ValidationContext`

Required fields:
- `run_id: str`
- `artifact_dir: str`
- `benchmark_id: str`
- `expected_authorization_policy: str`
- `test_identity_refs: list[str]`

Rules:
- Identity references must not contain passwords or tokens.
- Validation rules must be explicit and reproducible.
- Evidence paths must remain within the artifact directory.
- Human review must be recorded when required by the finding policy.

### 11.7 `BenchmarkConfig`

Required fields:
- `benchmark_id: str`
- `strategy: Literal["restler_baseline", "random", "heuristic", "llm_ranker", "llm_feedback"]`
- `target_config: TargetConfig`
- `execution_budget: ExecutionBudget`
- `repetitions: int`
- `artifact_root: str`
- `reset_strategy_id: str`

Rules:
- Repetitions must be positive.
- Reset strategies must be registered and explicitly configured; do not execute arbitrary reset commands supplied by the LLM.
- Each repetition gets a distinct run ID.
- The selected strategy and all relevant settings must be saved with the run.

### 11.8 `BenchmarkReport`

Required fields:
- `benchmark_id: str`
- `run_ids: list[str]`
- `strategy: str`
- `metrics: dict[str, float | int | str | None]`
- `errors: list[str]`
- `configuration_reference: str`

Rules:
- Metrics must include documented units and denominator definitions in the report metadata or accompanying documentation.
- Failed and incomplete runs must be represented, not silently omitted.
- Reports must be generated from persisted artifacts, not transient in-memory state alone.

### 11.9 `AssessmentProvider`

Define a protocol or abstract interface:

```python
class AssessmentProvider(Protocol):
    def assess(
        self,
        candidates: list[CandidateSequence],
        context: PlannerContext,
    ) -> list[CandidateAssessment]:
        ...
```

Requirements:
- The provider returns assessments only for the supplied candidate IDs.
- The provider never executes candidates.
- Provider implementations may be remote, local, or mocked.
- Timeouts and malformed output must be handled by the assessment orchestration layer.
- The mock provider must work without network access or credentials.

## 12. Ownership summary

| Contract or component | Owner |
|---|---|
| Shared persisted models | Joint agreement; implementation may be coordinated by Person A |
| Assessment provider and prompts | Person A |
| Scoring configuration and ranking | Person A |
| Planner history and feedback policy | Person A |
| Budget state and enforcement | Person B |
| Target validation and RESTler execution | Person B |
| Finding validation | Person B |
| Benchmark configuration and reports | Person B |
| Contract and end-to-end tests | Both |

The shared `models.py` file must have one canonical definition for every shared type. No duplicate model classes with the same logical purpose are allowed.
