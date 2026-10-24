"""Feedback-driven prioritization (Phase 2).

Stateless and reproducible: each round the state is rebuilt from the run
history (results + findings), then remaining candidates are scored.

Operation key = "METHOD normalized-path-template", taken from the executor's
response summaries, so only steps actually sent count as exercised.

Rules (weights in FeedbackConfig; heuristic, NOT probabilities):
  score = base_score
        + novelty_bonus * (share of the candidate's operations not yet exercised)
        - novelty_bonus   if ALL its operations were already exercised
        + productive_bonus * n / max_count  (n = most productive-execution count
                                             among its operations)
        - quiet_penalty    * q / max_count  (q = most quiet-execution count)
  clamped to [0, 1].

An execution is "productive" only if it yielded a lead/needs_review/confirmed
finding with a fingerprint not seen before in this run (duplicates earn no
reward). Completed/failed/timed-out executions without a new fingerprint are
"quiet". SKIPPED and BUDGET_EXHAUSTED results are ignored: not executed (or
only partly) is not the same as "no finding observed". A 5xx is only a lead.
Counts are capped at max_count, so state stays bounded.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from adaptive_restler.models import (
    CandidateSequence,
    ExecutionResult,
    ExecutionStatus,
    Finding,
    FindingStatus,
    PosInt,
    Score,
)
from adaptive_restler.selection.contract import (
    MAX_RATIONALE_CHARS,
    SelectionContext,
    SelectionProposal,
    SelectionResult,
)
from adaptive_restler.selection.selector import build_result
from adaptive_restler.validation.findings import normalize_path_template

__all__ = [
    "FeedbackConfig",
    "FeedbackSelector",
    "FeedbackState",
    "build_state",
    "operation_key",
    "score_candidate",
]

_COUNTED = frozenset(
    {ExecutionStatus.COMPLETED, ExecutionStatus.FAILED, ExecutionStatus.TIMED_OUT}
)
_PRODUCTIVE = frozenset(
    {FindingStatus.LEAD, FindingStatus.NEEDS_REVIEW, FindingStatus.CONFIRMED}
)


class FeedbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_score: Score = 0.5
    novelty_bonus: Score = 0.2
    productive_bonus: Score = 0.15
    quiet_penalty: Score = 0.1
    max_count: PosInt = 5


@dataclass(frozen=True)
class FeedbackState:
    covered: frozenset[str]
    productive: dict[str, int]
    quiet: dict[str, int]
    seen_fingerprints: frozenset[str]


def operation_key(method: str, path: str) -> str:
    return f"{method.upper()} {normalize_path_template(path)}"


def _result_ops(result: ExecutionResult) -> set[str]:
    ops: set[str] = set()
    for response in result.responses:
        method, path = response.get("method"), response.get("path")
        if isinstance(method, str) and isinstance(path, str) and method and path:
            ops.add(operation_key(method, path))
    return ops


def _fingerprint(finding: Finding) -> str:
    if finding.fingerprint is not None:
        return finding.fingerprint
    return f"{finding.category}|{finding.candidate_id}"


def build_state(
    results: Sequence[ExecutionResult],
    findings: Sequence[Finding],
    config: FeedbackConfig,
) -> FeedbackState:
    by_candidate: dict[str, list[Finding]] = {}
    for finding in findings:
        if finding.status in _PRODUCTIVE:
            by_candidate.setdefault(finding.candidate_id, []).append(finding)

    covered: set[str] = set()
    productive: dict[str, int] = {}
    quiet: dict[str, int] = {}
    seen: set[str] = set()

    for result in results:
        if result.status not in _COUNTED:
            continue
        ops = _result_ops(result)
        covered |= ops
        is_new = False
        for finding in by_candidate.get(result.candidate_id, []):
            fp = _fingerprint(finding)
            if fp not in seen:
                seen.add(fp)
                is_new = True
        bucket = productive if is_new else quiet
        for op in ops:
            bucket[op] = min(config.max_count, bucket.get(op, 0) + 1)

    return FeedbackState(frozenset(covered), productive, quiet, frozenset(seen))


def score_candidate(
    candidate: CandidateSequence, state: FeedbackState, config: FeedbackConfig
) -> tuple[float, list[str]]:
    ops = {operation_key(r.method, r.path) for r in candidate.sequence}
    score = config.base_score
    reasons = [f"base {config.base_score:.3f}"]

    new_ops = ops - state.covered
    if new_ops:
        delta = config.novelty_bonus * len(new_ops) / len(ops)
        score += delta
        reasons.append(f"{delta:+.3f} {len(new_ops)} of {len(ops)} operations not yet exercised")
    else:
        score -= config.novelty_bonus
        reasons.append(f"{-config.novelty_bonus:+.3f} all operations already exercised")

    n = max((state.productive.get(op, 0) for op in ops), default=0)
    if n:
        delta = config.productive_bonus * n / config.max_count
        score += delta
        reasons.append(f"{delta:+.3f} shares operations with {n} productive execution(s)")

    q = max((state.quiet.get(op, 0) for op in ops), default=0)
    if q:
        delta = -config.quiet_penalty * q / config.max_count
        score += delta
        reasons.append(f"{delta:+.3f} shares operations with {q} quiet execution(s)")

    return round(max(0.0, min(1.0, score)), 6), reasons


class FeedbackSelector:
    """Ranks remaining candidates by the explainable feedback score."""

    name = "feedback_priority"

    def __init__(self, config: FeedbackConfig | None = None) -> None:
        self.config = config or FeedbackConfig()

    def rank(
        self, candidates: Sequence[CandidateSequence], context: SelectionContext
    ) -> SelectionResult:
        start = time.perf_counter()
        state = build_state(context.results, context.findings, self.config)
        scored = []
        for candidate in candidates:
            score, reasons = score_candidate(candidate, state, self.config)
            scored.append((candidate, score, reasons))
        scored.sort(key=lambda t: (-t[1], t[0].estimated_request_count, t[0].candidate_id))
        proposals = [
            SelectionProposal(
                candidate_id=c.candidate_id,
                priority=score,
                rationale="; ".join(reasons)[:MAX_RATIONALE_CHARS],
            )
            for c, score, reasons in scored
        ]
        return build_result(
            self.name,
            proposals,
            candidates,
            context,
            latency_seconds=time.perf_counter() - start,
        )