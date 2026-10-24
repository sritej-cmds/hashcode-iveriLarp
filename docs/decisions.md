# Technical Decisions

## Status

Initial proposal. Items marked OPEN must be agreed upon by both contributors before the relevant implementation begins.

## D-001: Application architecture

**Status:** PROPOSED

Start with one Python application containing modular packages. Do not introduce microservices before there is a demonstrated need.

## D-002: Shared candidate schema

**Status:** OPEN

A candidate represents one ordered sequence of API requests, with an ID, source, tags, request estimate, and metadata.

Before implementation, agree on:
- Required fields.
- Candidate ID generation.
- How request bodies and authentication references are represented.
- How RESTler-generated candidates are imported.
- Schema versioning.

## D-003: LLM provider

**Status:** PROPOSED

Use a replaceable provider interface. Unit tests must use a deterministic mock provider and must not require an API key or network access.

Provider timeouts, retries, output limits, and validation must be bounded.

## D-004: Ranking

**Status:** OPEN

Use a deterministic ranking function that separates:
- Estimated discovery value.
- Estimated impact.
- Novelty.
- Execution and inference costs.

The initial proposed formula is documented in `scoring.md`.

The weights are experimental defaults, not validated optimal values.

## D-005: Budget enforcement

**Status:** PROPOSED

Enforce request and wall-clock limits in deterministic code outside the LLM.

Clarify how the budget is allocated across candidates and how a running request is handled when the time budget expires.

## D-006: Target authorization

**Status:** PROPOSED

Run only against explicitly authorized test targets. The LLM cannot choose or override the target host, credentials, allowlist, or budget.

## D-007: Finding confirmation

**Status:** PROPOSED

An HTTP status code alone cannot confirm a vulnerability. Confirmation requires reproducible evidence of security impact and a documented validation rule or human review.

## D-008: Experiment artifacts

**Status:** OPEN

Each experiment must have:
- Unique run ID.
- Immutable configuration snapshot.
- Target and benchmark version.
- Candidate and assessment records.
- Execution outcomes.
- Finding records and evidence references.
- Software/model versions where available.
- Final metrics and error summary.

Choose JSONL, SQLite, or a documented combination before implementing persistence.

## D-009: Python and dependency versions

**Status:** OPEN

Select the Python version after checking compatibility with RESTler, Pydantic, and the chosen model/runtime libraries. Pin dependencies after a clean installation succeeds.

## D-010: Baselines

**Status:** PROPOSED

At minimum compare standard RESTler, a simple deterministic heuristic, and the LLM-guided planner under equivalent budgets.

## Decision change procedure

For every material change:
1. Record the reason.
2. Describe the alternatives.
3. Identify affected interfaces and tests.
4. Get agreement from both contributors.
5. Update this document and related documentation.