"""assess_candidates (interfaces.md 5.1): batching, bounded retry, fallback.

- Batches of ``context.max_candidates_per_assessment``.
- A ProviderTimeout is retried up to ``max_retries`` times; ProviderError and
  ContextLimitError are not retried.
- Any candidate without exactly one valid assessment gets the fallback
  (deterministic mock by default) and the reason is recorded. Missing
  assessments are never treated as high value.
- Only exception types are logged, never provider text.
"""

import logging
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

from adaptive_restler.models import CandidateAssessment, CandidateSequence, PlannerContext
from adaptive_restler.planner.provider import AssessmentProvider, MockAssessmentProvider
from adaptive_restler.selection.generator import ContextLimitError
from adaptive_restler.selection.provider import ProviderError, ProviderTimeout

__all__ = ["AssessmentReport", "assess_candidates", "assess_candidates_with_report"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssessmentReport:
    """``assessments`` follow input order. ``fallback_reasons`` maps candidate_id -> reason."""

    assessments: list[CandidateAssessment]
    fallback_reasons: dict[str, str] = field(default_factory=dict)


def _call(
    provider: AssessmentProvider,
    batch: list[CandidateSequence],
    context: PlannerContext,
    max_retries: int,
) -> tuple[list[CandidateAssessment], str | None]:
    reason = None
    for _ in range(max_retries + 1):
        try:
            return provider.assess(batch, context), None
        except ProviderTimeout:
            reason = "provider_raised:ProviderTimeout"
            logger.warning("assessment timeout batch=%d", len(batch))
        except (ProviderError, ContextLimitError) as exc:
            reason = f"provider_raised:{type(exc).__name__}"
            logger.warning("assessment failed type=%s batch=%d", type(exc).__name__, len(batch))
            break
    return [], reason


def assess_candidates_with_report(
    candidates: Sequence[CandidateSequence],
    provider: AssessmentProvider,
    context: PlannerContext,
    *,
    fallback: AssessmentProvider | None = None,
    max_retries: int = 1,
) -> AssessmentReport:
    if max_retries < 0:
        raise ValueError("max_retries must be >= 0")
    pool = list(candidates)
    ids = [c.candidate_id for c in pool]
    if len(set(ids)) != len(ids):
        raise ValueError("candidate IDs must be unique")
    fb = fallback if fallback is not None else MockAssessmentProvider(provider_id="fallback-mock")
    size = context.max_candidates_per_assessment

    by_id: dict[str, CandidateAssessment] = {}
    reasons: dict[str, str] = {}
    for start in range(0, len(pool), size):
        batch = pool[start : start + size]
        batch_ids = {c.candidate_id for c in batch}
        got, reason = _call(provider, batch, context, max_retries)
        counts = Counter(a.candidate_id for a in got if a.candidate_id in batch_ids)
        for a in got:
            if counts.get(a.candidate_id) == 1:
                by_id[a.candidate_id] = a
        missing = [c for c in batch if c.candidate_id not in by_id]
        if missing:
            for c in missing:
                reasons[c.candidate_id] = reason or "missing_or_invalid_assessment"
            for a in fb.assess(missing, context):
                by_id[a.candidate_id] = a
    return AssessmentReport([by_id[cid] for cid in ids], reasons)


def assess_candidates(
    candidates: Sequence[CandidateSequence],
    provider: AssessmentProvider,
    context: PlannerContext,
    *,
    fallback: AssessmentProvider | None = None,
    max_retries: int = 1,
) -> list[CandidateAssessment]:
    return assess_candidates_with_report(
        candidates, provider, context, fallback=fallback, max_retries=max_retries
    ).assessments