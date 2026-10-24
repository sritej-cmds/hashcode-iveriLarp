# Budget-Aware Adaptive LLM Planner for RESTler
## Detailed implementation plan, equal two-person work split, interfaces, benchmarks, and testing

**Status:** Implementation specification  
**Project type:** Research prototype / hackathon application  
**Primary objective:** Test whether an LLM-guided, cost-aware planner can improve security-test prioritization over existing baselines under the same testing budget.  
**Important:** This is a hypothesis and implementation plan, not evidence that the proposed planner is already better than RESTler or RESTSpecIT. The team uses **Claude Web**, not Claude Code; the workflow below assumes manual file handoff unless a verified GitHub integration is available.

---

## 1. Project goal and non-goals

### 1.1 Goal

Build an end-to-end prototype that accepts an API specification and testing configuration, generates or imports candidate REST API test sequences, prioritizes those candidates, executes them against an explicitly authorized local test target, validates findings, and reports effectiveness and overhead.

The planner must answer:

> Given the candidates available now, which test sequence should run next to maximize expected security value while respecting execution and inference budgets?

### 1.2 Required comparisons

Compare under equivalent conditions:

1. **RESTler baseline:** RESTler's normal supported workflow/configuration, without our LLM planner controlling priorities.
2. **Simple heuristic baseline:** Transparent non-LLM candidate-ranking rules.
3. **LLM planner:** LLM security-value estimates plus deterministic cost-aware ranking.
4. **LLM planner + feedback:** Same planner, with observed execution outcomes updating subsequent priorities.
5. **Optional GPTQ model:** Compare a supported quantized model against the same model in a baseline precision, if hardware/runtime allows.
6. **Optional speculative decoding:** Only if supported and ordinary generation latency is a meaningful bottleneck.

Do not claim RESTSpecIT is being improved unless it is actually integrated and evaluated. RESTSpecIT, RESTler, and our planner have related but different objectives. Describe the prototype accurately as an LLM-guided prioritization layer for RESTler or RESTler-compatible candidate sequences.

### 1.3 Non-goals for the first working version

- Do not train or fine-tune a new foundation model.
- Do not let the LLM execute arbitrary shell commands or unrestricted HTTP requests.
- Do not use production APIs or systems without explicit authorization.
- Do not treat every HTTP 5xx, timeout, or unusual response as a confirmed vulnerability.
- Do not implement GPTQ and speculative decoding before a functioning baseline exists.
- Do not claim novelty, superior bug discovery, or calibrated probabilities before experiments support the claims.
- Do not require cloud LLM access. Support a local OpenAI-compatible endpoint where practical; allow a mock/deterministic provider for tests.
- Do not let the LLM make the final security-severity determination on its own.

---

## 2. Definition of success

The application is end-to-end working when a developer can:

1. Install dependencies from documented instructions.
2. Start the app and required services with a documented command.
3. Load an OpenAPI specification for a local intentionally vulnerable API.
4. Configure a strict time/request budget and choose a baseline or planner strategy.
5. Generate or import a candidate corpus.
6. Run an experiment and observe progress.
7. Execute only against the configured authorized target.
8. Save raw run artifacts, planner decisions, execution outcomes, and validation results.
9. View a report comparing approaches using the same budget.
10. Re-run an experiment from a saved configuration and reproduce its setup.
11. Run automated tests successfully in CI.

### 2.1 Core success metrics

Primary outcome:
- **Unique confirmed security findings per fixed testing budget.**

Secondary outcomes:
- Time to first confirmed finding.
- Confirmed findings per 1,000 requests.
- Known seeded-vulnerability recall, when the benchmark has defensible ground truth.
- Endpoint and state-transition coverage, where measurable.
- False-positive rate after validation.
- Planner latency (median and p95).
- LLM input/output tokens and estimated/actual inference cost.
- Total wall-clock runtime.
- Requests sent and completed.
- Peak process memory.
- Number of candidates evaluated, selected, skipped, and repeated.

Report medians and spread across repeated runs. Start with five runs per configuration if time allows; use more for stronger evidence. Use the same seed where supported, API reset strategy, target version, checkers, and budget across approaches.

---

## 3. Security value: explicit, auditable definition

### 3.1 Separate three concepts

Do not confuse:

1. **Predicted discovery likelihood:** an estimate that a candidate test may expose a new issue.
2. **Security impact/value:** how serious the issue would be if verified.
3. **Execution cost:** the resources required to run the test and plan its selection.

The LLM may estimate the first concept and provide evidence for a security hypothesis. The system's policy assigns impact/value using deterministic rules. Actual vulnerability and severity must be confirmed by evidence, not by the LLM's confidence score.

### 3.2 Impact rubric for confirmed findings

Use a documented, transparent rubric inspired by CVSS concepts, but do not call it an official CVSS score unless the full applicable CVSS version and calculator are implemented correctly.

For each **confirmed** finding, record:

- **Confidentiality impact:** 0 = none, 1 = limited, 2 = significant, 3 = severe.
- **Integrity impact:** 0 = none, 1 = limited, 2 = significant, 3 = severe.
- **Availability impact:** 0 = none, 1 = limited, 2 = significant, 3 = severe.
- **Exploit preconditions:** 0 = highly constrained, 1 = some access/conditions, 2 = readily reachable within the authorized test setup.
- **Scope:** one resource/user, multiple users/tenants, or broader service.
- **Evidence quality:** unconfirmed, suggestive, reproducible, independently verified.

A simple internal impact score for prioritization of confirmed issue classes can be:

`ImpactScore = 4*C + 4*I + 3*A + 2*Scope + 1*Exploitability`

where:
- `C`, `I`, `A` are each in `[0,3]`;
- `Scope` is in `[0,2]`;
- `Exploitability` is in `[0,2]`.

The maximum is 28. This is an **internal heuristic**, not CVSS. Document the weights and do not present the score as a standards-based severity score. Evidence quality is a gate, not a multiplier: unconfirmed findings must not be reported as confirmed regardless of score.

For the report, use human-reviewable categories:
- Informational: no meaningful security impact demonstrated.
- Low: limited impact under constrained conditions.
- Medium: meaningful impact but bounded scope or preconditions.
- High: serious confidentiality, integrity, or availability impact.
- Critical: broad or severe compromise with strong reproducible evidence.

Severity labels require human review for the prototype. Preserve a rationale and evidence artifacts.

### 3.3 Candidate-level predicted security value

Before a vulnerability is confirmed, do **not** assign it a vulnerability severity. Instead, compute candidate priority from separate estimates.

For candidate `i`:

- `P_i`: estimated chance that running candidate `i` discovers a **new, valid** security finding, in `[0,1]`.
- `V_i`: expected impact value if a new finding is confirmed, normalized to `[0,1]`.
- `N_i`: novelty factor, `[0,1]`, reflecting whether similar tests/findings have already been explored.
- `R_i`: expected REST request count, positive integer or conservative estimate.
- `T_i`: expected execution time in seconds.
- `L_i`: expected planner latency in seconds, including any required LLM call.
- `K_i`: estimated inference cost in the project's chosen unit (tokens, currency, or compute proxy).

Define:

`ExpectedValue_i = P_i * V_i * N_i`

Define normalized cost:

`Cost_i = a*(R_i/R_budget) + b*(T_i/T_budget) + c*(L_i/L_budget) + d*(K_i/K_budget)`

with non-negative weights `a,b,c,d` summing to 1. Start with documented defaults, e.g. `a=0.35, b=0.35, c=0.20, d=0.10`, then run sensitivity analysis. If a cost component is unavailable, omit it and renormalize the remaining weights; do not silently treat missing cost as zero.

Use a stable ranking score:

`Priority_i = ExpectedValue_i / (epsilon + Cost_i)`

where `epsilon` prevents division by zero. This is a proposed heuristic, not a result established by the cited papers. The implementation must also support a value-only ablation (`ExpectedValue_i`) and a cost-only/simple heuristic baseline.

Avoid pretending the LLM can accurately provide `P_i` without calibration. Initially label it **LLM-estimated score**, not a statistically calibrated probability. Calibrate against historical candidate outcomes only if there is sufficient labeled data, and use a held-out evaluation set to avoid leakage.

### 3.4 Candidate novelty and feedback

- Deduplicate exact candidates using a stable hash of normalized method, path, relevant parameters, and sequence order.
- Track previously executed candidates and related findings.
- Reduce priority for exact repeats that yielded no new information.
- Do not automatically suppress related tests just because one test failed; nearby variants can expose different bugs.
- Increase priority of related candidates only when observed results provide a defensible signal.
- Keep the original LLM estimate, feedback-adjusted score, final score, and adjustment reason in the log.

### 3.5 Confirmation gate

A response status alone is not proof:
- HTTP 500 is a lead, not a confirmed vulnerability.
- HTTP 401/403 may be expected access controls, not proof of a bug.
- HTTP 200 does not prove a security issue either.

A finding may be labeled **confirmed** only when a documented rule or authorized human reviewer can reproduce the security impact and save supporting evidence. For BOLA/IDOR, for example, use a controlled test with two test identities and verify that one identity can access or modify another identity's protected resource contrary to the documented authorization policy. Never test using real user data.

---

## 4. Two-person equal work split

Both people own a comparable amount of implementation, testing, documentation, and integration work. Person A owns the candidate/LLM/planning domain. Person B owns execution/benchmark/reporting. Shared contracts are agreed first; neither person should invent the other person's interfaces.

### Person A — Candidate pipeline, LLM provider, and planner

Responsibilities:
1. Candidate schema and OpenAPI candidate ingestion/generation adapter.
2. LLM provider abstraction.
3. Prompt templates and strict structured output schema.
4. Candidate security hypothesis and predicted-value estimate.
5. Cost-aware ranking implementation.
6. Feedback-adjustment function, using execution events supplied through the shared contract.
7. Planner unit tests and mock provider.
8. Documentation for planner configuration, prompts, and score interpretation.

Deliverables:
- `src/planner/schemas.py`
- `src/planner/provider.py`
- `src/planner/prompts.py`
- `src/planner/scoring.py`
- `src/planner/feedback.py`
- `tests/unit/test_planner_*.py`
- `docs/planner.md`

### Person B — RESTler adapter, safe execution, validation, and evaluation

Responsibilities:
1. RESTler installation/invocation adapter and version capture.
2. Candidate corpus import/export and normalization, using the agreed schema.
3. Authorized target configuration and safety checks.
4. Execution budget enforcement and API reset strategy.
5. Finding evidence collection and validation status.
6. Benchmark harness, baselines, repeated runs, and metric aggregation.
7. Report generation and end-to-end integration tests.
8. Documentation for setup, benchmarks, safety, and reproducibility.

Deliverables:
- `src/runner/restler_adapter.py`
- `src/runner/executor.py`
- `src/runner/budget.py`
- `src/validation/findings.py`
- `src/evaluation/metrics.py`
- `src/evaluation/benchmark.py`
- `tests/integration/`
- `docs/benchmarks.md`
- `docs/safety.md`

### Shared ownership (equal responsibility)

Both people must:
- Review each other's pull requests.
- Participate in interface and architecture decisions.
- Fix integration issues in their own module first.
- Pair on one end-to-end test and one demo rehearsal.
- Review the final findings and avoid overstating results.
- Maintain the README and final presentation together.

### Integration rule

Person A must not import or call private implementation details inside Person B's modules. Person B must not call the LLM directly. All cross-module communication happens through the contracts in Section 5.

---

## 5. Plug-and-play architecture and exact interfaces

Keep the first version as a single Python application with modular packages. Avoid microservices and distributed infrastructure until a working prototype proves they are necessary.

Suggested stack:
- Python 3.11 or 3.12 (choose one version and lock it; confirm compatibility with RESTler and selected ML libraries before freezing).
- Pydantic v2 for schema validation.
- Typer or argparse for CLI.
- SQLite or JSONL for experiment logs.
- pytest for testing.
- A local OpenAI-compatible LLM endpoint or a mock provider for offline tests.
- RESTler invoked through a subprocess adapter; don't embed or fork RESTler initially.
- Optional lightweight web UI only after the CLI end-to-end path works.

### 5.1 Suggested repository layout

```text
restler-adaptive-planner/
├── pyproject.toml
├── README.md
├── CLAUDE.md
├── .env.example
├── .gitignore
├── config/
│   ├── default.yaml
│   └── benchmark.yaml
├── docs/
│   ├── architecture.md
│   ├── planner.md
│   ├── scoring.md
│   ├── benchmarks.md
│   ├── safety.md
│   └── decisions.md
├── src/
│   └── adaptive_restler/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── models.py
│       ├── planner/
│       │   ├── __init__.py
│       │   ├── schemas.py
│       │   ├── provider.py
│       │   ├── prompts.py
│       │   ├── scoring.py
│       │   └── feedback.py
│       ├── runner/
│       │   ├── __init__.py
│       │   ├── restler_adapter.py
│       │   ├── executor.py
│       │   └── budget.py
│       ├── validation/
│       │   └── findings.py
│       └── evaluation/
│           ├── metrics.py
│           ├── benchmark.py
│           └── report.py
├── benchmarks/
│   ├── crapi/
│   ├── restler_demo/
│   └── manifests/
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── fixtures/
└── artifacts/                 # gitignored; run outputs
```

The exact layout may be adjusted only if both people agree before implementation starts.

### 5.2 Candidate schema

Every candidate sequence must be representable without depending on an LLM provider or runner implementation.

```python
from typing import Any, Literal
from pydantic import BaseModel, Field

class CandidateRequest(BaseModel):
    method: str
    path: str
    headers: dict[str, str] = Field(default_factory=dict)
    query: dict[str, Any] = Field(default_factory=dict)
    body: Any | None = None
    expected_statuses: list[int] = Field(default_factory=list)

class CandidateSequence(BaseModel):
    candidate_id: str
    sequence: list[CandidateRequest]
    source: Literal["restler", "imported", "generated"]
    tags: list[str] = Field(default_factory=list)
    estimated_requests: int = Field(ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### 5.3 LLM planner output schema

```python
from pydantic import BaseModel, Field

class CandidateAssessment(BaseModel):
    candidate_id: str
    security_hypotheses: list[str]
    rationale: str
    predicted_discovery_score: float = Field(ge=0, le=1)
    expected_impact_score: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)
    missing_information: list[str] = Field(default_factory=list)
```

Rules:
- These are estimates, not confirmed findings or calibrated probabilities.
- Validate every output. Reject malformed data and use a safe fallback heuristic.
- Do not allow the model to change target host, credentials, safety configuration, or budgets.
- Do not expose secrets in prompts or logs.
- Use bounded input length, output tokens, timeout, and retries.
- Log model identifier, model settings, prompt version/hash, duration, and token counts where available.

### 5.4 Cost and ranking interface

```python
class CandidateCost(BaseModel):
    request_count: int = Field(ge=1)
    estimated_execution_seconds: float = Field(ge=0)
    estimated_planner_seconds: float = Field(ge=0)
    estimated_inference_cost: float = Field(ge=0)

def rank_candidates(candidates, assessments, costs, budget, history):
    # Return candidates ordered by the documented priority policy.
    ...
```

The final function must be deterministic for the same validated inputs and configuration. Separate the LLM's assessment from the deterministic ranking calculation.

### 5.5 Runner interface

```python
class ExecutionResult(BaseModel):
    candidate_id: str
    status: str
    started_at: str
    duration_seconds: float
    request_count: int
    responses: list[dict]
    error_summary: str | None = None
    evidence_paths: list[str] = Field(default_factory=list)

def execute_candidate(candidate, target_config, budget) -> ExecutionResult:
    # Execute only through the configured authorized target and stop at budget.
    ...
```

The executor must enforce target allowlists, request/time caps, timeouts, and a stop signal. Do not accept an arbitrary target host from the LLM.

### 5.6 Finding interface

```python
from typing import Literal
from pydantic import BaseModel, Field

class Finding(BaseModel):
    finding_id: str
    candidate_id: str
    category: str
    status: Literal["lead", "needs_review", "confirmed", "rejected"]
    impact_score: int | None = Field(default=None, ge=0, le=28)
    severity: Literal["informational", "low", "medium", "high", "critical"] | None = None
    evidence_paths: list[str] = Field(default_factory=list)
    validation_notes: str = ""
```

Only confirmed findings can receive a final severity label in the summary. `impact_score` and severity must have a human-reviewable rationale.

---

## 6. Plug-and-play integration contract

Before implementing modules, create `docs/decisions.md` with these agreements:

1. Python version and dependency lock approach.
2. Candidate schema and ID generation rule.
3. LLM provider contract.
4. Ranking score formula and default weights.
5. Runner timeout/request budget behavior.
6. How each benchmark resets target state.
7. What constitutes a confirmed finding.
8. Artifact directory and JSONL/SQLite log format.

### 6.1 Integration order

1. Both people create the repository, layout, and CI skeleton.
2. Person A implements Pydantic models and mock LLM provider.
3. Person B implements runner interfaces with a fake executor and budget object.
4. Both run contract tests against the same fixtures.
5. Person A plugs the real provider and scoring into the interfaces.
6. Person B plugs the RESTler adapter and validation into the interfaces.
7. Integrate only through public functions documented in module `__init__.py` files.
8. Add a local benchmark and end-to-end command.
9. Add optional optimizations only after a baseline run is saved.

### 6.2 Integration guarantees

- Every module can be tested without network access by using fakes/mocks.
- A planner unit test must not start RESTler.
- A runner unit test must not call the LLM.
- A failed LLM call must not crash the whole experiment; use a logged fallback strategy.
- A runner timeout must stop further execution and mark the run as budget-exhausted.
- Every run gets a unique ID and a saved immutable configuration.
- Results from different runs must not be merged without recording their run IDs.
- Keep output schemas versioned; breaking contract changes require both owners' approval.
- No direct cross-imports between planner internals and runner internals.

---

## 7. Benchmark targets and what to compare against

Only test against local or otherwise explicitly authorized targets. Pin benchmark versions/commits and record setup instructions.

### 7.1 Primary target: OWASP crAPI

Repository: https://github.com/OWASP/crAPI

Why:
- Purpose-built for API security practice.
- Has multiple API workflows and security issues relevant to authorization and business logic.
- Provides a realistic demo target for the project.

Caveats:
- Confirm the current version and available seeded vulnerabilities before choosing expected results.
- Use only documented, reproducible findings as ground truth.
- Reset the target to a known state between runs.
- Do not assume every bug class can be detected by generic RESTler execution without custom test sequences or checkers.

### 7.2 Secondary target: RESTler demo / examples

Repository: https://github.com/microsoft/restler-fuzzer

Why:
- Exercises RESTler's intended workflow and integration.
- Useful for verifying candidate import, execution, request limits, and basic stateful sequences.
- Good first target for debugging the adapter.

Caveats:
- Demo APIs may not contain meaningful security vulnerabilities.
- Use this primarily for integration, not as the sole security-effectiveness benchmark.

### 7.3 Optional benchmark: synthetic vulnerable API

Create a tiny local API with intentionally seeded and documented cases, such as:
- BOLA/IDOR using two synthetic test identities.
- Missing authorization on a protected state-changing route.
- Inconsistent state handling after delete/update.
- Improper workflow enforcement across a multi-step sequence.

Keep each vulnerability behind a documented fixture and include a corresponding expected test. Do not introduce real secrets or real data. Use this as a ground-truth benchmark for measuring recall, but clearly label it synthetic and avoid overgeneralizing its results to production systems.

### 7.4 RESTSpecIT comparison

Paper/preprint: https://arxiv.org/abs/2402.05102

Do not claim a direct RESTSpecIT comparison unless you have a reproducible, runnable implementation and can align the target, budget, and metrics. RESTSpecIT primarily targets specification inference and black-box request mutation; it is not interchangeable with RESTler's default strategy.

### 7.5 Baselines to include

At minimum:
- B0: RESTler baseline.
- B1: Random candidate order (sanity check only).
- B2: Simple heuristic ranking, e.g. sensitive-operation tags, sequence length, and observed novelty.
- B3: LLM assessment + deterministic cost ranking.
- B4: B3 + feedback.
- B5: B3 with quantized model, if feasible.
- B6: B3 with speculative decoding, if feasible.

Ablations:
- LLM score only, no cost.
- Cost-only heuristic, no LLM.
- LLM score + cost.
- LLM score + cost + feedback.
- FP16 vs GPTQ quantized model.
- Standard decoding vs speculative decoding.

The simple heuristic baseline is important: if a few deterministic rules perform as well as the LLM, the added model complexity may not be justified.

---

## 8. Experimental protocol

### 8.1 Fairness controls

For each comparison, hold constant:
- Target API version and initial state.
- API specification and candidate corpus where the comparison permits.
- Authentication setup and test identities.
- Available checkers and validation rules.
- Time limit and maximum request count.
- Machine/runtime environment where possible.
- Random seeds, where supported.
- Reset and cleanup procedure.
- Number of repetitions.

If the LLM planner changes the candidate corpus, report that as a separate experiment from candidate prioritization; do not conflate generation improvements with ranking improvements.

### 8.2 Recommended staged protocol

**Stage 1 — Integration correctness**
- Run RESTler against its demo target.
- Verify request generation, execution, artifact capture, stop behavior, and error handling.
- Confirm no unauthorized host can be selected.

**Stage 2 — Scoring correctness**
- Use a fixed candidate fixture and mocked LLM outputs.
- Verify exact scores and deterministic ordering.
- Test zero/unknown costs, duplicates, missing assessments, and ties.
- Test invalid LLM output and fallback behavior.

**Stage 3 — Security effectiveness**
- Run B0–B4 on crAPI and the synthetic ground-truth API.
- Use equal budgets and reset state.
- Run at least five repetitions if time permits.
- Validate and deduplicate findings independently.

**Stage 4 — Efficiency optimization**
- Measure baseline planner latency and memory first.
- Test GPTQ only if memory or local deployment cost is a problem.
- Test speculative decoding only if token generation is a meaningful bottleneck.
- Compare optimization against the same target model and prompt/task.

### 8.3 Metrics and definitions

- `confirmed_unique_findings`: distinct findings that passed the confirmation gate.
- `known_vulnerability_recall`: known seeded findings confirmed / total known seeded findings in scope.
- `time_to_first_confirmed_finding`: elapsed wall time to first confirmed finding; report as “not found” if none.
- `requests_per_finding`: total executed requests / confirmed unique findings; undefined when zero findings.
- `planner_overhead_fraction`: planner wall time / total run wall time.
- `p95_planner_latency`: 95th percentile latency for individual planning decisions.
- `inference_cost`: measured tokens/currency/compute proxy, with its units clearly named.
- `false_positive_rate`: rejected leads / all reviewed leads, with exact denominator reported.
- `endpoint_coverage`: exercised endpoints / endpoints in the pinned spec, if measurable.
- `peak_memory`: specify process RSS or device memory and measurement method.

Report the actual denominator for every rate. Do not report a recall number if the total known vulnerability count is unknown.

### 8.4 Result presentation

Generate:
- Summary table by approach.
- Cumulative confirmed findings vs elapsed time.
- Planner overhead vs total run time.
- Memory vs quantization mode.
- Ranking quality / confirmed findings vs planner mode.
- Run configuration and software versions.

Include failures and negative results. Don't cherry-pick the best run.

---

## 9. Test plan

### 9.1 Unit tests — Person A

1. Valid assessment parses.
2. Invalid score below 0 or above 1 is rejected.
3. Missing candidate ID is rejected.
4. Unknown candidate assessment falls back safely.
5. Same inputs yield same deterministic rank.
6. Cost weights are non-negative and sum to 1.
7. Missing cost field triggers documented renormalization or a safe fallback.
8. Zero cost is handled using epsilon.
9. Exact duplicate candidate is detected.
10. Previously tested no-signal candidate receives the documented adjustment.
11. New evidence changes feedback only according to explicit rules.
12. LLM prompt does not contain target credentials or secrets.
13. Timeout and malformed JSON are handled.
14. Provider retry count is bounded.

### 9.2 Unit tests — Person B

1. Budget stops execution at request limit.
2. Budget stops execution at time limit.
3. Timeout is recorded without hanging the run.
4. Target host outside allowlist is rejected.
5. Candidate sequence order is preserved.
6. RESTler subprocess failure is captured.
7. Artifacts are saved for successful and failed runs.
8. Finding deduplication is stable.
9. HTTP 500 alone cannot create a confirmed finding.
10. 401/403 alone cannot create a confirmed finding.
11. A known synthetic BOLA fixture is confirmed only when cross-user access is demonstrated.
12. API reset happens before every comparable run.
13. Interrupted run writes a partial manifest and can be diagnosed.
14. Output paths cannot escape the configured artifacts directory.

### 9.3 Integration tests — joint

1. Candidate fixture → mock planner → ranking → fake runner → feedback → next ranking.
2. Real local LLM provider → schema validation → deterministic rank.
3. RESTler adapter imports/executes a tiny safe sequence against the local demo.
4. Run stops exactly at budget.
5. Findings and raw evidence link back to the candidate and run ID.
6. Full run report can be generated from saved artifacts alone.
7. Replaying a saved config reproduces the setup and records any nondeterminism.
8. Optional quantization and speculative decoding modes fail clearly when unsupported.

### 9.4 End-to-end acceptance test

From a clean environment:
1. Follow README install steps.
2. Start the local target.
3. Run the documented smoke test.
4. Run a short baseline experiment.
5. Run a short LLM-planner experiment using the mock provider.
6. Inspect saved report and evidence.
7. Run unit/integration tests.
8. Confirm all generated artifacts are documented and no secrets are committed.

---

## 10. GitHub and Claude Web workflow

**Important team constraint:** We use Claude through its web interface, not Claude Code in a terminal. Claude Web may not have direct access to our local working tree. GitHub is the source of truth; our local machines are where code is applied, executed, tested, and verified.

Use one shared repository with two feature branches and a protected main branch if the hosting setup permits.

Suggested branches:
- `feat/planner` — Person A
- `feat/runner-evaluation` — Person B
- Prefer small pull requests into `main`; use an integration branch only if needed.

### 10.1 Commit rules

Commit at each meaningful milestone; during active implementation, aim for several small commits per work session. Do not create meaningless commits just to satisfy a count.

Suggested prefixes:
- `docs:` documentation/contracts
- `feat:` feature
- `test:` tests
- `fix:` bug fix
- `refactor:` refactoring
- `bench:` benchmark/evaluation
- `chore:` tooling/config

Examples:
- `docs: define candidate and planner contracts`
- `feat: add deterministic cost-aware ranking`
- `test: cover planner score edge cases`
- `feat: add RESTler subprocess adapter`
- `feat: enforce request and time budgets`
- `bench: add crAPI experiment harness`
- `docs: document finding confirmation rubric`

### 10.2 Claude Web rules

- Keep `CLAUDE.md` at the repository root with project rules, module ownership, contracts, test commands, and known gaps.
- At the start of a Claude Web task, provide the relevant sections of `IMPLEMENTATION_PLAN.md`, the current public interfaces, and the exact files needed for the task. Do not assume Claude can see your local repository automatically.
- Ask Claude to inspect the supplied current files before proposing edits. If its chat has GitHub access enabled, confirm which branch and files it can actually read; do not assume access.
- Request complete file contents or a unified diff, plus tests and a concise change summary. Avoid disconnected snippets that are hard to apply.
- Apply the changes locally, inspect the diff, run tests, and only then commit. Never claim a test passed unless it was executed and the output checked.
- Use separate Claude conversations or clearly separated tasks for Person A and Person B. Do not ask both conversations to rewrite shared files at the same time.
- Do not paste API secrets, access tokens, or production data into prompts.
- Review all changes to security validation, budget enforcement, target allowlists, and score formulas.
- If Claude suggests a dependency, verify compatibility with the selected Python version and OS before locking it.
- If a response is too long, ask for one named file at a time while preserving the shared contract.

### 10.3 Claude Web handoff loop

Use this loop for every task:

1. **Prepare context:** Copy the relevant plan section and provide the current versions of files the task may touch. Include `CLAUDE.md`, `docs/decisions.md`, and the relevant tests when applicable.
2. **Constrain scope:** Name the allowed files and explicitly list files that must not be changed.
3. **Request an artifact:** Ask for complete file contents or a unified diff, tests, exact commands to run, and a summary of assumptions.
4. **Apply locally:** Save the changes into the matching paths in your repository.
5. **Inspect:** Run `git diff --check`, inspect `git diff`, and confirm no unrelated files or secrets changed.
6. **Test locally:** Run the requested tests and save the actual output. If something fails, send Claude the error plus the relevant current file, not only the last line of the traceback.
7. **Commit:** Commit only after the tests appropriate to that change pass, or clearly label a WIP commit if the team explicitly agrees.
8. **Share:** Push the branch and open a pull request. The other teammate reviews the interface, tests, and diff before merging.

Do not let Claude invent files it claims to have changed without returning their contents or a patch. If using any Claude Web GitHub integration, verify the repository, branch, and actual resulting diff before relying on it.

### 10.4 First-session checklist

- [ ] Create the GitHub repository and add `IMPLEMENTATION_PLAN.md`.
- [ ] Add `CLAUDE.md` with the project summary, contracts, ownership, test commands, and safety rules.
- [ ] Both teammates review and agree on `docs/decisions.md`.
- [ ] Create `feat/planner` and `feat/runner-evaluation`.
- [ ] Ask Claude Web to implement only the shared skeleton/contracts first.
- [ ] Apply and test those files locally before either person builds the full module.
- [ ] Build a mock end-to-end flow before connecting the real RESTler adapter or real LLM.

### 10.5 Example prompts for Claude Web

**Person A, first task**
> Use the supplied `IMPLEMENTATION_PLAN.md` and current repository files. Implement only the shared Pydantic candidate/assessment/cost schemas and deterministic scoring interface. Do not implement the RESTler adapter or alter unrelated files. Return complete file contents or a unified diff for each changed file, add unit tests for boundaries, invalid input, missing costs, deterministic ordering, and ties, and provide the exact local commands I should run. Do not claim tests passed unless you actually ran them in an available environment; clearly mark anything not executed.

**Person B, first task**
> Use the supplied `IMPLEMENTATION_PLAN.md` and current repository files. Implement only the budget model, fake runner, artifact manifest, and target allowlist. Do not call an LLM or implement ranking. Return complete file contents or a unified diff for each changed file, add tests for request/time limits, unauthorized target rejection, subprocess failure, and partial-run artifacts, and provide the exact local commands I should run. Clearly distinguish tests you executed from tests I must run locally.

**Integration task**
> Read the current public interfaces and tests supplied from the repository before changing anything. Connect the planner and runner only through documented contracts. Do not change score semantics or confirmation rules without updating the decision record and tests. Return a minimal patch and add one end-to-end test using a mock LLM and fake local executor. Provide exact commands for local verification and list any work not completed.

---

## 11. Milestones and definition of done

### Milestone 0 — Contracts first
- [ ] Repository layout and `CLAUDE.md`.
- [ ] Candidate, assessment, cost, result, and finding contracts approved by both people.
- [ ] Score formula and severity/confirmation rules documented.
- [ ] CI and test command work.

### Milestone 1 — Working skeleton
- [ ] Mock provider.
- [ ] Deterministic ranker.
- [ ] Fake executor.
- [ ] Budget enforcement.
- [ ] JSONL/SQLite artifacts.
- [ ] One end-to-end smoke test.

### Milestone 2 — Real RESTler connection
- [ ] Pinned RESTler version.
- [ ] Demo target works locally.
- [ ] Candidate import/adapter works.
- [ ] Run caps and safety controls verified.
- [ ] Artifacts captured.

### Milestone 3 — Real planner
- [ ] Local or compatible LLM provider works.
- [ ] Structured output validation and fallback.
- [ ] Feedback loop updates priorities.
- [ ] Prompt/model versions logged.

### Milestone 4 — Security validation and benchmarks
- [ ] crAPI pinned and reproducible.
- [ ] Synthetic ground-truth API ready.
- [ ] Findings reviewed and deduplicated.
- [ ] Baseline and planner runs use equal budgets.
- [ ] Report includes all metrics and negative results.

### Milestone 5 — Optional inference optimizations
- [ ] Baseline latency/memory measured.
- [ ] GPTQ evaluated if memory warrants it.
- [ ] Speculative decoding evaluated if generation latency warrants it.
- [ ] Ranking quality and end-to-end outcomes rechecked.

### Milestone 6 — Final delivery
- [ ] Clean install instructions verified.
- [ ] One-command local demo documented.
- [ ] Full tests pass.
- [ ] Reports/artifacts reproducible.
- [ ] No secrets or production data committed.
- [ ] Demo script and limitations written.
- [ ] Claims match the evidence.

---

## 12. Risks and mitigation

| Risk | Mitigation |
|---|---|
| RESTler adapter is more difficult than expected | First support import/export of a fixed candidate corpus; add deeper integration incrementally. |
| crAPI findings are difficult to validate automatically | Use a small synthetic ground-truth API for quantitative validation, plus crAPI as a realistic secondary target. |
| LLM scores are inconsistent | Use a fixed prompt/schema, log model/version, repeat runs, and retain deterministic fallback ranking. |
| LLM overhead outweighs benefit | Batch assessments, cap prompt/output size, cache by candidate hash, and compare overhead against findings gained. |
| Planner does not outperform heuristic baseline | Report honestly; inspect whether the LLM adds measurable value. |
| Quantization damages prioritization | Evaluate ranking and security outcomes, not only memory. |
| Speculative decoding is unsupported or slower | Keep it optional and skip it unless profiling justifies it. |
| Integration conflicts | Freeze interfaces early, use small PRs, avoid simultaneous edits to shared files. |
| False positives undermine demo | Separate leads from confirmed findings and preserve reproducible evidence. |
| Unsafe target configuration | Local-only default, explicit allowlist, strict budget caps, and no LLM-controlled host selection. |

---

## 13. Final principles

1. **Build the smallest complete system first.** A CLI end-to-end demo is more valuable than a polished UI with a broken backend.
2. **Measure before optimizing.** GPTQ and speculative decoding are optional techniques, not required project features.
3. **Treat the LLM as an advisor.** Deterministic code controls budgets, execution, and final ranking.
4. **Never equate a server error with a vulnerability.** Confirm security impact with evidence.
5. **Use fair baselines.** Standard RESTler and a transparent heuristic are mandatory comparisons.
6. **Make all scores explainable.** Store the reasons and inputs behind each priority decision.
7. **Keep modules plug-and-play.** Use stable contracts, mocks, and independent tests.
8. **Make commits small and meaningful.** Every milestone should leave the repository in a testable state.
9. **Do not claim improvements before measuring them.**
10. **The final deliverable is a reproducible experiment, not just an LLM wrapper.**

## 14. Official starting references

- RESTler repository: https://github.com/microsoft/restler-fuzzer
- OWASP crAPI: https://github.com/OWASP/crAPI
- RESTSpecIT preprint: https://arxiv.org/abs/2402.05102
- GPTQ paper: https://arxiv.org/abs/2210.17323
- Speculative decoding paper: https://arxiv.org/abs/2211.17192
- CVSS resources: https://www.first.org/cvss/

Before finalizing dependencies or claiming benchmark capabilities, check current official repositories and papers. This plan is the agreed implementation target; exact compatibility and measured results must be verified during implementation.