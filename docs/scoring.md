# Scoring and Candidate Prioritization

## Purpose

Define a transparent candidate-prioritization policy that can be compared against simple baselines.

This scoring system is a research hypothesis. It has not been demonstrated to improve security findings.

## Candidate assessment

For candidate i, collect:

- `P_i`: LLM-estimated discovery score in [0, 1].
- `V_i`: estimated potential impact value in [0, 1].
- `N_i`: novelty factor in [0, 1].
- `R_i`: estimated request count.
- `T_i`: estimated execution time.
- `L_i`: estimated planner latency.
- `K_i`: estimated inference cost.

Initially, `P_i` is an estimated score, not a calibrated probability.

## Expected value

`ExpectedValue_i = P_i * V_i * N_i`

The LLM should explain the security hypothesis and relevant evidence. It must not label an unverified issue as a confirmed vulnerability.

## Normalized cost

`Cost_i = a*(R_i/R_budget) + b*(T_i/T_budget) + c*(L_i/L_budget) + d*(K_i/K_budget)`

Initial proposed weights:

- Request cost: 0.35
- Execution time: 0.35
- Planner latency: 0.20
- Inference cost: 0.10

All weights must be non-negative and sum to one.

If a cost component is unavailable, omit it and renormalize the available weights. Never silently interpret missing cost as zero.

## Priority

`Priority_i = ExpectedValue_i / (epsilon + Cost_i)`

Use a small positive epsilon to prevent division by zero.

Implement deterministic tie-breaking, for example:
1. Higher priority.
2. Higher expected value.
3. Lower estimated execution time.
4. Lexicographically smaller candidate ID.

## Baselines and ablations

Compare:
- Random order.
- Simple deterministic heuristic.
- LLM score only.
- Cost-only heuristic.
- LLM score plus cost.
- LLM score plus cost plus feedback.

These comparisons help determine whether the LLM and feedback actually contribute value.

## Feedback policy

Record:
- Original assessment.
- Original priority.
- Observed result.
- Novelty adjustment.
- Adjusted priority.
- Reason for adjustment.

Do not permanently suppress related candidates because a single test did not find an issue.

## Confirmed severity

Candidate priority is not vulnerability severity.

Confirmed findings may receive a separate internal impact score using the rubric in `IMPLEMENTATION_PLAN.md`. This score is not CVSS. Human review is required for final severity labels.

## Calibration and reporting

Do not describe LLM estimates as calibrated probabilities unless calibration has been evaluated on appropriate labeled data.

Report score quality, planner overhead, and confirmed findings separately. A higher predicted score does not itself demonstrate better security testing.