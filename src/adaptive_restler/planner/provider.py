"""Assessment provider interface and a deterministic mock (contract 11.9).

Providers never execute candidates and never touch targets, credentials or
budgets. Timeouts, retries, malformed output and fallback are handled by the
assessment orchestration layer, not here.
"""

from typing import Protocol, runtime_checkable

from adaptive_restler.models import (
    CandidateAssessment,
    CandidateSequence,
    PlannerContext,
)

__all__ = ["AssessmentProvider", "MockAssessmentProvider"]


@runtime_checkable
class AssessmentProvider(Protocol):
    """Anything that can assess candidates (remote, local or mocked).

    Must return assessments only for the supplied candidate IDs.
    """

    def assess(
        self,
        candidates: list[CandidateSequence],
        context: PlannerContext,
    ) -> list[CandidateAssessment]: ...


_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SENSITIVE_TAGS = frozenset({"admin", "auth", "delete", "payment", "upload", "user"})
_HIGH_IMPACT_TAGS = frozenset({"admin", "auth", "payment"})
_MAX_HYPOTHESIS_CHARS = 500


def _clip(text: str) -> str:
    return text[:_MAX_HYPOTHESIS_CHARS]


def _has_path_parameter(path: str) -> bool:
    return "{" in path or any(segment.isdigit() for segment in path.split("/"))


class MockAssessmentProvider:
    """Deterministic, offline provider for tests and the first integration.

    Scores come from simple structural features (write methods, path
    parameters, sensitive tags, sequence length). The same candidate always
    gets the same assessment, regardless of batch composition, call order or
    process. No network, no randomness, no credentials. These are *not*
    security judgments; hypotheses are phrased as things to check.
    """

    def __init__(self, provider_id: str = "mock") -> None:
        self.provider_id = provider_id

    def assess(
        self,
        candidates: list[CandidateSequence],
        context: PlannerContext,
    ) -> list[CandidateAssessment]:
        if len(candidates) > context.max_candidates_per_assessment:
            raise ValueError(
                f"batch of {len(candidates)} exceeds max_candidates_per_assessment="
                f"{context.max_candidates_per_assessment}"
            )
        ids = [c.candidate_id for c in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate IDs in a batch must be unique")
        return [self._assess_one(c, context.prompt_version) for c in candidates]

    def _assess_one(self, candidate: CandidateSequence, prompt_version: str) -> CandidateAssessment:
        methods = {r.method for r in candidate.sequence}
        tags = sorted({t.strip().lower() for t in candidate.tags} & _SENSITIVE_TAGS)
        has_write = bool(methods & _WRITE_METHODS)
        param_request = next((r for r in candidate.sequence if _has_path_parameter(r.path)), None)
        has_high_impact_tag = bool(set(tags) & _HIGH_IMPACT_TAGS)

        discovery = 20
        discovery += 15 if has_write else 0
        discovery += 15 if param_request is not None else 0
        discovery += 20 if tags else 0
        discovery += 10 if len(candidate.sequence) >= 2 else 0

        impact = 30
        impact += 20 if has_write else 0
        impact += 30 if has_high_impact_tag else 0
        impact += 10 if "DELETE" in methods else 0

        hypotheses: list[str] = []
        if param_request is not None:
            hypotheses.append(
                _clip(
                    "Object-level authorization may be missing on "
                    f"{param_request.method} {param_request.path}"
                )
            )
        if has_write:
            hypotheses.append(
                "State-changing requests may accept unexpected input or skip authorization checks"
            )
        for tag in tags:
            hypotheses.append(_clip(f"Area tagged '{tag}' may have weaker access control"))

        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            security_hypotheses=hypotheses,
            rationale="Deterministic mock assessment from methods, path parameters, tags and length.",
            predicted_discovery_score=min(discovery, 90) / 100,
            expected_impact_score=min(impact, 90) / 100,
            confidence=0.3,
            missing_information=["mock provider performs no semantic analysis"],
            provider_id=self.provider_id,
            prompt_version=prompt_version,
        )
