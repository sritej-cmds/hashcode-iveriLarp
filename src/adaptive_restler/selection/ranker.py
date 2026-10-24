"""Assessment-based selector (Phase 4b).

The large model only assesses; ``rank_candidates`` orders deterministically; the
result goes through ``build_result`` -> the deterministic validator. Assessments
are cached per candidate for the life of the selector (assessment does not
depend on history). Executed candidates are never re-assessed.
"""

import time
from collections.abc import Sequence

from adaptive_restler.models import (
    CandidateAssessment,
    CandidateSequence,
    ExecutionBudget,
    PlannerHistory,
    ScoringConfig,
)
from adaptive_restler.planner.provider import AssessmentProvider
from adaptive_restler.planner.scoring import rank_candidates
from adaptive_restler.selection.assessment import assess_candidates_with_report
from adaptive_restler.selection.assessor import ModelAssessmentProvider, planner_context_from_config
from adaptive_restler.selection.config import ModelConfig
from adaptive_restler.selection.contract import (
    SelectionContext,
    SelectionProposal,
    SelectionResult,
)
from adaptive_restler.selection.feedback import FeedbackSelector
from adaptive_restler.selection.provider import ModelProvider
from adaptive_restler.selection.selector import (
    DeterministicSelector,
    FallbackSelector,
    build_result,
)

__all__ = ["AssessmentRankingSelector", "POLICY_VERSION", "build_assessment_ranker_selector"]

POLICY_VERSION = "rank-v1"
_MAX_RATIONALE = 500


class AssessmentRankingSelector:
    name = "llm_assessment_ranker"

    def __init__(
        self,
        assessor: AssessmentProvider,
        config: ModelConfig,
        budget: ExecutionBudget,
        *,
        run_id: str = "run",
        fallback: AssessmentProvider | None = None,
        max_retries: int = 1,
    ) -> None:
        self.assessor = assessor
        self.fallback = fallback
        self.max_retries = max_retries
        self.scoring = ScoringConfig(
            request_budget=float(budget.max_requests),
            execution_time_budget_seconds=budget.max_runtime_seconds,
            planner_latency_budget_seconds=budget.max_runtime_seconds,
        )
        self.planner_context = planner_context_from_config(run_id, config)
        self.assessments: dict[str, CandidateAssessment] = {}
        self.fallback_reasons: dict[str, str] = {}

    def rank(
        self, candidates: Sequence[CandidateSequence], context: SelectionContext
    ) -> SelectionResult:
        start = time.perf_counter()
        done = context.executed_candidate_ids
        pool = [c for c in candidates if c.candidate_id not in done]
        if not pool:
            return build_result(self.name, [], candidates, context)

        new = [c for c in pool if c.candidate_id not in self.assessments]
        if new:
            report = assess_candidates_with_report(
                new, self.assessor, self.planner_context,
                fallback=self.fallback, max_retries=self.max_retries,
            )
            for a in report.assessments:
                self.assessments[a.candidate_id] = a
            self.fallback_reasons.update(report.fallback_reasons)

        history = PlannerHistory(executed_candidate_ids=set(done), policy_version=POLICY_VERSION)
        ranked = rank_candidates(
            pool, [self.assessments[c.candidate_id] for c in pool], [], history, self.scoring
        )
        top = ranked[0].priority_score
        proposals = [
            SelectionProposal(
                candidate_id=r.candidate_id,
                priority=(r.priority_score / top) if top > 0 else 0.0,
                rationale="; ".join(r.reasons)[:_MAX_RATIONALE],
            )
            for r in ranked
        ]
        return build_result(
            self.name, proposals, candidates, context,
            latency_seconds=time.perf_counter() - start,
        )


def build_assessment_ranker_selector(
    provider: ModelProvider, config: ModelConfig, budget: ExecutionBudget, *, run_id: str = "run"
) -> FallbackSelector:
    """Ranker with the configured deterministic fallback. Ranker is ``.primary``."""
    primary = AssessmentRankingSelector(
        ModelAssessmentProvider(provider, config), config, budget, run_id=run_id
    )
    fallback = FeedbackSelector() if config.fallback_strategy == "feedback" else DeterministicSelector()
    return FallbackSelector(primary, fallback)