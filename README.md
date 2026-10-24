# Adaptive RESTler Planner

A research prototype exploring whether an LLM-guided, budget-aware planner can improve the prioritization of API security tests executed with RESTler.

## Problem

API security testing can involve many possible request sequences, but testing time, request budgets, and LLM inference resources are limited. We want to investigate whether selecting tests according to estimated security value and execution cost can improve testing efficiency.

## Proposed approach

1. Load an API specification and obtain candidate test sequences.
2. Assess candidates using an LLM or a deterministic fallback.
3. Rank candidates using a documented cost-aware scoring policy.
4. Execute selected candidates against an explicitly authorized test target.
5. collect execution outcomes and validate security findings.
6. Use feedback to reprioritize remaining candidates.
7. Compare results against standard RESTler and a simple heuristic baseline.

## Research question

Can cost-aware LLM-guided prioritization discover more independently confirmed security findings under the same testing budget than the selected baselines?

This is a hypothesis to evaluate, not an established result.

## Project status

**Stage:** Initial setup and interface design.

The implementation, integration, and benchmark results are not yet complete. Do not interpret the proposed scoring formula as a validated model.

## Documentation

- [Implementation plan](docs/IMPLEMENTATION_PLAN.md)
- [Architecture](docs/architecture.md)
- [Technical decisions](docs/decisions.md)
- [Scoring policy](docs/scoring.md)
- [Benchmark methodology](docs/benchmarks.md)
- [Safety and validation](docs/safety.md)
- [Claude Web instructions](CLAUDE.md)

## Planned technology

- Python
- Pydantic for structured data contracts
- RESTler for API fuzzing and execution
- An LLM provider behind a replaceable interface
- pytest for automated tests
- JSONL or SQLite for experiment records

Exact versions and dependencies will be selected after compatibility checks.

## Development principles

- Use only explicitly authorized test targets.
- Keep planning, execution, and finding validation separate.
- Enforce request and time budgets deterministically.
- Treat HTTP errors as leads, not automatic proof of vulnerabilities.
- Use reproducible experiments and fair baselines.
- Record negative results as well as positive results.

## Getting started

The project is currently in the setup stage. Installation and execution instructions will be added after the initial application skeleton is implemented and tested.

## Team workflow

The two contributors share responsibility for architecture, interface agreements, code review, integration, and the final evaluation. See `docs/IMPLEMENTATION_PLAN.md` for the proposed work split.