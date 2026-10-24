# Project Instructions for Claude

## Project identity

We are building a budget-aware adaptive LLM planner for RESTler API security testing.

The goal is to evaluate whether LLM-guided candidate prioritization can improve confirmed security findings under a fixed testing budget.

This is an experimental hypothesis, not a proven result.

## Source of truth

- `docs/IMPLEMENTATION_PLAN.md` is the main implementation plan.
- `docs/decisions.md` contains agreed technical decisions.
- `docs/architecture.md` documents component responsibilities and data flow.
- `docs/scoring.md` defines the proposed scoring policy.
- `docs/benchmarks.md` defines the evaluation methodology.
- `docs/safety.md` defines execution and finding-validation requirements.

Do not silently contradict these documents. If a change is necessary, explain why and identify which documents and tests need updating.

## Development workflow

- We use Claude Web, not Claude Code.
- Do not assume access to the local filesystem or the current Git branch.
- Work only on the explicitly assigned task.
- Inspect all supplied current files before proposing changes.
- Return complete file contents or a unified diff for every changed file.
- Include tests, exact commands for local verification, and a concise change summary.
- Distinguish tests actually executed from tests the developers still need to run.
- Never claim tests passed without verified execution output.
- Do not invent repository files, function names, dependencies, or benchmark results.
- Avoid unrelated refactoring and large rewrites.

## Team responsibilities

Person A:
- Candidate and assessment schemas.
- LLM provider and prompts.
- Deterministic scoring and ranking.
- Feedback-based reprioritization.
- Planner tests and documentation.

Person B:
- RESTler adapter.
- Execution and budget enforcement.
- Finding validation and evidence capture.
- Benchmark harness, metrics, and reports.

Both contributors:
- Agree on shared interfaces before parallel implementation.
- Review each other's changes.
- Test integration together.
- Keep architecture and interface decisions documented.

## Engineering requirements

- Use typed, validated data contracts.
- Keep LLM assessment separate from deterministic ranking.
- Keep planner internals independent of runner internals.
- Use mock providers and fake executors in unit tests.
- Provide safe fallback behavior for malformed or unavailable LLM responses.
- Enforce execution budgets outside the LLM.
- Keep output artifacts traceable to a run ID and candidate ID.

## Security requirements

- Execute only against explicitly authorized targets.
- Do not allow the LLM to change target hosts, credentials, or safety limits.
- Never commit API keys, access tokens, credentials, or sensitive test data.
- Do not treat HTTP 500, 401, or 403 alone as proof of a vulnerability.
- Keep unconfirmed leads separate from confirmed findings.
- Preserve reproducible evidence and validation notes.

## Change discipline

Before changing a shared interface:
1. Explain the proposed change.
2. Identify affected modules and tests.
3. Obtain agreement from both contributors.
4. Update the relevant documentation.
5. Update and run the contract tests.

Make small, reviewable changes. Do not implement the entire project in one response.