"""Deterministic ranking (interfaces.md 5.2, docs/scoring.md). No I/O.

ExpectedValue = discovery * impact * novelty; Priority = EV / (epsilon + cost).
Novelty is 0 for already-executed candidates, else 1. A missing assessment gives
EV 0 (ranked last, never assumed valuable). Cost components that are unavailable
are omitted and the remaining weights renormalized; without a CandidateCost only
the request component (from the candidate's estimate) is used.
Tie-break: priority desc, EV desc, execution seconds asc (unknown last), ID asc.
"""

import math
from collections.abc import Sequence

from adaptive_restler.models import (
    CandidateAssessment,
    CandidateCost,
    CandidateSequence,
    PlannerHistory,
    RankedCandidate,
    ScoringConfig,
)

__all__ = ["rank_candidates"]


def _by_id(items, label: str) -> dict:
    ids = [i.candidate_id for i in items]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate candidate_id in {label}")
    return {i.candidate_id: i for i in items}


def _normalized_cost(
    candidate: CandidateSequence, cost: CandidateCost | None, cfg: ScoringConfig
) -> tuple[float, list[str]]:
    requests = cost.estimated_request_count if cost else candidate.estimated_request_count
    parts = [(cfg.request_weight, requests / cfg.request_budget, "requests")]
    if cost is not None:
        parts.append(
            (cfg.execution_time_weight,
             cost.estimated_execution_seconds / cfg.execution_time_budget_seconds,
             "execution_time")
        )
        parts.append(
            (cfg.planner_latency_weight,
             cost.estimated_planner_seconds / cfg.planner_latency_budget_seconds,
             "planner_latency")
        )
        if cost.estimated_inference_cost is not None and cfg.inference_cost_budget is not None:
            parts.append(
                (cfg.inference_cost_weight,
                 cost.estimated_inference_cost / cfg.inference_cost_budget,
                 "inference_cost")
            )
    total = sum(w for w, _, _ in parts)
    if total <= 0:
        raise ValueError("no usable cost component has a positive weight")
    return sum(w * v for w, v, _ in parts) / total, [name for _, _, name in parts]


def rank_candidates(
    candidates: Sequence[CandidateSequence],
    assessments: Sequence[CandidateAssessment],
    costs: Sequence[CandidateCost],
    history: PlannerHistory,
    config: ScoringConfig,
) -> list[RankedCandidate]:
    pool = _by_id(candidates, "candidates")
    by_assessment = _by_id(assessments, "assessments")
    by_cost = _by_id(costs, "costs")
    for label, mapping in (("assessments", by_assessment), ("costs", by_cost)):
        if set(mapping) - set(pool):
            raise ValueError(f"{label} reference unknown candidate_ids")

    rows = []
    for cid, candidate in pool.items():
        a = by_assessment.get(cid)
        cost = by_cost.get(cid)
        norm, components = _normalized_cost(candidate, cost, config)
        executed = cid in history.executed_candidate_ids
        novelty = 0.0 if executed else 1.0
        if a is None:
            ev = 0.0
            reasons = ["no assessment: expected value 0"]
        else:
            ev = a.predicted_discovery_score * a.expected_impact_score * novelty
            reasons = [
                f"discovery={a.predicted_discovery_score:.2f} "
                f"impact={a.expected_impact_score:.2f} novelty={novelty:.0f}"
            ]
        if executed:
            reasons.append("already executed: novelty 0")
        reasons.append(f"cost={norm:.3f} from {','.join(components)}")
        priority = ev / (config.epsilon + norm)
        exec_seconds = cost.estimated_execution_seconds if cost else math.inf
        rows.append((cid, ev, norm, priority, exec_seconds, reasons))

    rows.sort(key=lambda r: (-r[3], -r[1], r[4], r[0]))
    return [
        RankedCandidate(
            candidate_id=cid,
            expected_value=ev,
            normalized_cost=norm,
            priority_score=priority,
            rank=rank,
            reasons=reasons,
        )
        for rank, (cid, ev, norm, priority, _, reasons) in enumerate(rows, start=1)
    ]