# Benchmark and Evaluation Plan

## Research question

Does cost-aware LLM-guided candidate prioritization discover more independently confirmed security findings under the same testing budget than the selected baselines?

## Benchmark targets

### RESTler demo

Repository: https://github.com/microsoft/restler-fuzzer

Purpose:
- Validate RESTler adapter behavior.
- Verify candidate execution and artifact capture.
- Test request and time limits.
- Debug the end-to-end workflow.

Do not assume the demo contains meaningful security vulnerabilities.

### OWASP crAPI

Repository: https://github.com/OWASP/crAPI

Purpose:
- Evaluate on an API security training target with realistic workflows.
- Investigate authorization and business-logic test cases.
- Compare confirmed findings across approaches.

Pin the benchmark version, document setup, and reset target state between comparable runs.

### Synthetic vulnerable API

Optionally create a small local API with documented seeded issues and test identities.

Use it to measure recall against known ground truth. Clearly distinguish synthetic results from results on crAPI.

## Baselines

- B0: Standard RESTler configuration.
- B1: Random candidate ordering.
- B2: Simple deterministic heuristic.
- B3: LLM assessment with deterministic cost-aware ranking.
- B4: LLM ranking with feedback.

Optional:
- B5: Quantized model.
- B6: Speculative decoding, if supported and justified by profiling.

## Fairness controls

Keep these consistent wherever applicable:
- API version and initial state.
- Authentication setup and test identities.
- Candidate corpus when testing ranking alone.
- Request and time budgets.
- Available checkers and validation rules.
- Reset procedure.
- Machine/runtime environment.
- Random seeds where supported.
- Number of repetitions.

If candidate generation differs between approaches, evaluate that separately from prioritization.

## Metrics

### Primary

- Unique confirmed findings per fixed budget.

### Secondary

- Known seeded-vulnerability recall.
- Time to first confirmed finding.
- Requests per confirmed finding.
- Endpoint coverage, if measurable.
- Planner latency: median and p95.
- Planner overhead as a fraction of total runtime.
- Inference tokens or cost.
- Peak memory.
- False-positive rate among reviewed leads.

State the exact denominator for every reported rate. If there are no confirmed findings, requests per finding is undefined.

## Experimental stages

1. Integration smoke test against the RESTler demo.
2. Scoring tests using fixed candidates and mocked assessments.
3. Security-effectiveness experiments on crAPI and/or the synthetic API.
4. Optional model-optimization experiments.

Run repeated trials where time permits. Five repetitions per configuration is a reasonable initial target, not a guarantee of statistical significance.

## Reporting

Save:
- Run configuration.
- Benchmark and software versions.
- Candidate and assessment logs.
- Execution outcomes.
- Confirmed and rejected findings.
- Metric definitions and results.
- Errors, limitations, and failed runs.

Report negative results. Do not claim that the planner improves performance unless the measurements support that conclusion.

## RESTSpecIT comparison

Paper: https://arxiv.org/abs/2402.05102

A direct comparison is optional. Do not claim RESTSpecIT was benchmarked or improved unless a reproducible implementation was actually run under comparable conditions.