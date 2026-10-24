# Architecture

## Status

Proposed architecture. Interfaces are not yet implemented or frozen.

## Design goals

- Modular components with explicit contracts.
- Deterministic budget enforcement and ranking.
- Replaceable LLM provider.
- Independent testing without network access.
- Reproducible experiments and traceable evidence.

## Data flow

```text
OpenAPI specification
        |
        v
Candidate ingestion / RESTler adapter
        |
        v
Candidate sequences
        |
        v
LLM assessment or deterministic fallback
        |
        v
Validated assessments
        |
        v
Deterministic cost-aware ranker <--- Cost estimates and history
        |
        v
Budget-aware candidate selector
        |
        v
Authorized execution through RESTler
        |
        v
Execution results and raw artifacts
        |
        v
Finding validation and deduplication
        |
        v
Feedback update
        |
        +------> Reprioritize remaining candidates
        |
        v
Metrics and experiment report
```

## Component responsibilities

### Candidate ingestion

Normalizes candidate sequences into the shared schema. Each candidate must have a stable identifier and sufficient metadata to reproduce its intended test.

### Assessment provider

Estimates security hypotheses, potential discovery value, and uncertainty. The provider may use a remote model, a local model, or a deterministic mock.

It does not execute requests or determine whether a vulnerability is confirmed.

### Deterministic ranker

Combines validated assessments, estimated costs, novelty, and configured weights. The same inputs and configuration must produce the same ordering.

### Budget controller

Tracks request and time budgets and stops execution when limits are reached. It is independent of the LLM.

### Runner

Executes approved candidates through the RESTler integration and records responses, errors, duration, and artifacts.

### Finding validator

Converts raw observations into leads, review-required findings, confirmed findings, or rejected findings according to documented evidence requirements.

### Feedback manager

Updates the priority of remaining candidates using execution history. Every adjustment must be explainable and recorded.

### Evaluation

Aggregates run records and computes metrics using consistent definitions and denominators.

## Module boundaries

- Planner code must not start RESTler subprocesses.
- Runner code must not call the LLM directly.
- The LLM must not control target selection or execution budgets.
- Finding validation must not rely on response status alone.
- Reports must be reproducible from saved experiment artifacts.

## Failure behavior

- Invalid LLM output: validate, retry within configured limits, then fall back safely.
- LLM timeout: record the error and use the configured fallback.
- Runner timeout: stop the relevant execution and record a partial result.
- Budget exhausted: stop scheduling additional tests.
- Evidence unavailable: do not mark a finding confirmed.
- Configuration invalid: fail before starting network execution.