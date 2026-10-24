"""Large-model assessment provider (Phase 4a).

Implements ``AssessmentProvider``. Shows the model method + path + tags only
(never headers, bodies or queries) and parses its JSON with
``planner.schemas.parse_provider_response``, so provider_id/prompt_version are
attached by us and severity/confirmed fields are rejected.

Provider errors, timeouts and ContextLimitError propagate; ``assess_candidates``
(selection/assessment.py) handles retry and fallback. Invalid items are dropped
and recorded in ``records[-1].issue_codes``.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from adaptive_restler.models import CandidateAssessment, CandidateSequence, PlannerContext
from adaptive_restler.planner.schemas import parse_provider_response
from adaptive_restler.selection.config import ModelConfig
from adaptive_restler.selection.generator import ContextLimitError
from adaptive_restler.selection.provider import ModelProvider

__all__ = [
    "AssessmentRecord",
    "ModelAssessmentProvider",
    "PROMPT_VERSION",
    "build_assessment_prompt",
    "planner_context_from_config",
]

PROMPT_VERSION = "assess-v1"
SYSTEM_PROMPT = (
    "You assess API test sequences for security weaknesses worth testing. "
    "Reply with JSON only, in this exact shape: "
    '{"assessments": [{"candidate_id": "<id>", "security_hypotheses": ["<testable idea>"], '
    '"rationale": "<one short sentence>", "predicted_discovery_score": <0 to 1>, '
    '"expected_impact_score": <0 to 1>, "confidence": <0 to 1>, '
    '"missing_information": ["<gap>"]}]}. '
    "One item per candidate, use only the given candidate_ids, at most 2 hypotheses "
    "of under 100 characters each, no other fields. Scores are rough estimates, not "
    "probabilities. Never state that a vulnerability exists and never give a severity."
)
_MAX_PATH_CHARS = 120
_MAX_STEPS_SHOWN = 8
_MAX_TAGS = 5
_MAX_TAG_CHARS = 30


@dataclass(frozen=True)
class AssessmentRecord:
    model: str
    latency_seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None
    candidates_shown: int
    assessments_returned: int
    missing_count: int
    issue_codes: tuple[str, ...]


def planner_context_from_config(run_id: str, config: ModelConfig) -> PlannerContext:
    """PlannerContext whose batch size and timeout come from ModelConfig."""
    return PlannerContext(
        run_id=run_id,
        prompt_version=PROMPT_VERSION,
        max_candidates_per_assessment=config.ranking_batch_size,
        assessment_timeout_seconds=config.llm_timeout_seconds,
    )


def _line(candidate: CandidateSequence) -> str:
    steps = " ; ".join(
        f"{r.method} {r.path[:_MAX_PATH_CHARS]}" for r in candidate.sequence[:_MAX_STEPS_SHOWN]
    )
    if len(candidate.sequence) > _MAX_STEPS_SHOWN:
        steps += " ; ..."
    tags = ",".join(t[:_MAX_TAG_CHARS] for t in candidate.tags[:_MAX_TAGS]) or "none"
    return f"- {candidate.candidate_id} | requests={len(candidate.sequence)} | tags={tags} | {steps}"


def build_assessment_prompt(
    candidates: Sequence[CandidateSequence], max_chars: int
) -> tuple[str, list[CandidateSequence]]:
    """Bounded prompt plus the candidates shown (tail dropped if too long).

    Dropped candidates get no assessment and fall back. Raises ContextLimitError
    if not even one fits.
    """
    head = "Assess each candidate:"
    shown = list(candidates)
    while shown:
        prompt = "\n".join([head] + [_line(c) for c in shown])
        if len(prompt) <= max_chars:
            return prompt, shown
        shown.pop()
    raise ContextLimitError("prompt exceeds max_prompt_chars")


class ModelAssessmentProvider:
    """One model call per ``assess`` batch, using ``config.large_model``."""

    def __init__(self, provider: ModelProvider, config: ModelConfig) -> None:
        self.provider = provider
        self.config = config
        self.provider_id = f"llm:{config.large_model}"
        self.records: list[AssessmentRecord] = []

    def assess(
        self, candidates: list[CandidateSequence], context: PlannerContext
    ) -> list[CandidateAssessment]:
        if len(candidates) > context.max_candidates_per_assessment:
            raise ValueError(
                f"batch of {len(candidates)} exceeds max_candidates_per_assessment="
                f"{context.max_candidates_per_assessment}"
            )
        ids = [c.candidate_id for c in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate IDs in a batch must be unique")
        if not candidates:
            return []

        cfg = self.config
        prompt, shown = build_assessment_prompt(candidates, cfg.max_prompt_chars)
        response = self.provider.generate(
            model=cfg.large_model,
            system=SYSTEM_PROMPT,
            prompt=prompt,
            max_output_tokens=cfg.max_output_tokens,
            timeout_seconds=min(cfg.llm_timeout_seconds, context.assessment_timeout_seconds),
            temperature=cfg.temperature,
            seed=cfg.seed,
            context_tokens=cfg.context_tokens,
        )
        parsed = parse_provider_response(
            response.text,
            expected_candidate_ids=[c.candidate_id for c in shown],
            provider_id=self.provider_id,
            prompt_version=context.prompt_version,
        )
        self.records.append(
            AssessmentRecord(
                model=cfg.large_model,
                latency_seconds=response.latency_seconds,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                candidates_shown=len(shown),
                assessments_returned=len(parsed.assessments),
                missing_count=len(parsed.missing_candidate_ids),
                issue_codes=tuple(i.code for i in parsed.issues),
            )
        )
        return parsed.assessments