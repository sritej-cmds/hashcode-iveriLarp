## D-011: Shared interface contract v1.0

**Status:** AGREED IMPLEMENTATION BASELINE

The authoritative public interfaces are defined in `docs/interfaces.md`, including the supporting model contracts.

The team agrees to the following rules:

1. Shared persisted models have one canonical definition in `src/adaptive_restler/models.py`.
2. Person A owns assessment, ranking, and planner feedback.
3. Person B owns budget enforcement, execution, finding validation, and evaluation.
4. Both people must use the same model fields, enum values, function signatures, and error semantics.
5. The first integrated flow uses a mock assessment provider and fake executor.
6. Real LLM and RESTler integration comes after the contract tests pass.
7. The runner exclusively owns mutable budget state and enforces limits independently of the LLM.
8. The LLM cannot choose or override the target, credentials, or execution budget.
9. An HTTP response status alone cannot confirm a vulnerability.
10. Shared interface changes require both contributors' agreement, updated documentation, and updated tests.

### Implementation sequencing

- First: shared models, contract tests, configuration validation, and mock/fake components.
- Second: Person A implements assessment, ranking, and feedback; Person B implements the runner, budget enforcement, validation, and benchmark infrastructure.
- Third: integrate through the documented public interfaces.
- Fourth: connect real RESTler execution and a real LLM provider.
- Fifth: evaluate baselines under controlled budgets.

### Repository baseline

Both feature branches must be created from the same `main` commit containing this decision and `docs/interfaces.md`. Each contributor must confirm their branch is up to date with that baseline before starting implementation.


## D-012: Contract v1.1 amendments

**Status:** AGREED (Person A and Person B)

Changes to `docs/interfaces.md`:

1. `ScoringConfig` gains `request_budget`, `execution_time_budget_seconds`,
   `planner_latency_budget_seconds` and `inference_cost_budget` (optional).
   Reason: `rank_candidates` takes no `ExecutionBudget`, so cost normalization had no
   denominators. The `rank_candidates` signature is unchanged.
2. `Finding` gains optional `fingerprint`, set by the validator. If it is `None`, the
   planner falls back to `category + candidate_id`. Reason: `PlannerHistory` stores
   fingerprints but `Finding` had no source for them.
3. All supporting models (Sections 11.2-11.8) live in `src/adaptive_restler/models.py`.
4. `pydantic` moves from the dev extras to runtime dependencies.
5. `docs/implementation_plan.md` still shows older function signatures. `interfaces.md`
   is authoritative (D-011).

Model-level guards added in `models.py` (derived from existing contract rules):
confirmed findings and any finding with a severity must have `evidence_paths`; header
names that look credential-bearing are rejected; auth profile and identity references
must be identifier-shaped; `RankedCandidate.reasons` must be non-empty.
