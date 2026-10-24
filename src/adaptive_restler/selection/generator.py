"""Small-model candidate generator (Phase 3).

Shows the model a bounded list of candidate IDs (method + path only: never
headers, bodies or queries) and parses its JSON into SelectionProposals.
Everything it returns goes through ``build_result`` -> the deterministic
validator, so unknown/duplicate/ineligible IDs are rejected and recorded.

Failure behavior:
- ProviderError / ProviderTimeout / ContextLimitError propagate, so
  ``FallbackSelector`` records the exception type as the fallback reason.
- Malformed JSON, bad items or an empty answer yield an empty result; the
  FallbackSelector then falls back. Details are in ``records[-1].issues``.
Cache hits skip model latency: disable the cache when measuring inference cost.
"""

import hashlib
import json
import logging
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from adaptive_restler.models import CandidateSequence
from adaptive_restler.selection.config import ModelConfig
from adaptive_restler.selection.contract import (
    SelectionContext,
    SelectionProposal,
    SelectionResult,
)
from adaptive_restler.selection.feedback import (
    FeedbackConfig,
    FeedbackSelector,
    build_state,
    score_candidate,
)
from adaptive_restler.selection.provider import ModelProvider
from adaptive_restler.selection.selector import (
    DeterministicSelector,
    FallbackSelector,
    build_result,
)

__all__ = [
    "ContextLimitError",
    "GenerationRecord",
    "ModelCandidateGenerator",
    "ResponseCache",
    "build_generator_selector",
    "build_prompt",
    "parse_proposals",
]

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You prioritize API test sequences. Reply with JSON only, in this exact shape: "
    '{"proposals": [{"candidate_id": "<id>", "priority": <number 0 to 1>, '
    '"rationale": "<one short sentence>"}]}. '
    "Use only candidate_ids from the list you are given, never invent ids, best first. "
    "Priority is your rough preference, not a probability of finding a bug."
)
MAX_RESPONSE_CHARS = 200_000
_MAX_PATH_CHARS = 120
_MAX_STEPS_SHOWN = 8
_MAX_COVERED_OPS = 30


class ContextLimitError(Exception):
    """Even one candidate does not fit in max_prompt_chars."""


@dataclass(frozen=True)
class GenerationRecord:
    model: str
    cache_hit: bool
    latency_seconds: float
    prompt_tokens: int | None
    completion_tokens: int | None
    candidates_shown: int
    proposals_parsed: int
    issues: tuple[str, ...]


class ResponseCache:
    """Bounded in-memory FIFO cache of raw model text."""

    def __init__(self, max_entries: int = 256) -> None:
        self._max = max_entries
        self._data: OrderedDict[str, str] = OrderedDict()

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def put(self, key: str, text: str) -> None:
        self._data[key] = text
        while len(self._data) > self._max:
            self._data.popitem(last=False)


def _describe(candidate: CandidateSequence) -> str:
    steps = " ; ".join(
        f"{r.method} {r.path[:_MAX_PATH_CHARS]}" for r in candidate.sequence[:_MAX_STEPS_SHOWN]
    )
    if len(candidate.sequence) > _MAX_STEPS_SHOWN:
        steps += " ; ..."
    return f"- {candidate.candidate_id} | requests={len(candidate.sequence)} | {steps}"


def build_prompt(
    candidates: Sequence[CandidateSequence], context: SelectionContext, config: ModelConfig
) -> tuple[str, list[str]]:
    """Bounded prompt plus the IDs shown. Raises ContextLimitError if nothing fits."""
    fcfg = FeedbackConfig()
    state = build_state(context.results, context.findings, fcfg)
    ordered = sorted(
        candidates,
        key=lambda c: (-score_candidate(c, state, fcfg)[0], c.estimated_request_count, c.candidate_id),
    )[: config.candidate_limit]
    covered = sorted(state.covered)[:_MAX_COVERED_OPS]
    head = [
        f"Remaining request budget: {context.remaining_requests}",
        f"Already exercised operations: {', '.join(covered) if covered else 'none'}",
        f"Propose up to {config.candidate_limit} candidate_ids from this list, best first:",
    ]
    shown = list(ordered)
    while shown:
        prompt = "\n".join(head + [_describe(c) for c in shown])
        if len(prompt) <= config.max_prompt_chars:
            return prompt, [c.candidate_id for c in shown]
        shown.pop()
    raise ContextLimitError("prompt exceeds max_prompt_chars")


def parse_proposals(raw: str, limit: int) -> tuple[list[SelectionProposal], list[str]]:
    """Structural parsing only. Returns (proposals, issue codes). Never raises.

    Unknown or duplicate IDs are NOT filtered here: the validator rejects them
    and records the rejection.
    """
    if len(raw) > MAX_RESPONSE_CHARS:
        return [], ["response_too_large"]
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return [], ["invalid_json"]
    if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
        return [], ["invalid_structure"]
    items = payload["proposals"]
    issues: list[str] = []
    if len(items) > limit * 2:
        issues.append("truncated")
        items = items[: limit * 2]
    proposals: list[SelectionProposal] = []
    for item in items:
        if not isinstance(item, dict):
            issues.append("invalid_item")
            continue
        try:
            proposals.append(SelectionProposal.model_validate(item))
        except ValidationError:
            issues.append("invalid_item")
    return proposals, issues


def _cache_key(model: str, prompt: str, config: ModelConfig) -> str:
    material = {
        "model": model,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "max_output_tokens": config.max_output_tokens,
        "temperature": config.temperature,
        "seed": config.seed,
        "candidate_limit": config.candidate_limit,
        "context_tokens": config.context_tokens,
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


class ModelCandidateGenerator:
    """SequenceSelector backed by the small model. Wrap it in a FallbackSelector."""

    name = "llm_generator"

    def __init__(
        self, provider: ModelProvider, config: ModelConfig, *, cache: ResponseCache | None = None
    ) -> None:
        self.provider = provider
        self.config = config
        self.cache = (cache if cache is not None else ResponseCache()) if config.cache_enabled else None
        self.records: list[GenerationRecord] = []
        logger.setLevel(config.log_level)

    def rank(
        self, candidates: Sequence[CandidateSequence], context: SelectionContext
    ) -> SelectionResult:
        start = time.perf_counter()
        if not candidates:
            return build_result(self.name, [], candidates, context)

        cfg = self.config
        prompt, shown = build_prompt(candidates, context, cfg)
        model = cfg.small_model
        key = _cache_key(model, prompt, cfg)
        text = self.cache.get(key) if self.cache is not None else None
        hit = text is not None
        prompt_tokens = completion_tokens = None
        latency = 0.0
        if text is None:
            response = self.provider.generate(
                model=model,
                system=SYSTEM_PROMPT,
                prompt=prompt,
                max_output_tokens=cfg.max_output_tokens,
                timeout_seconds=cfg.llm_timeout_seconds,
                temperature=cfg.temperature,
                seed=cfg.seed,
                context_tokens=cfg.context_tokens,
            )
            text = response.text
            prompt_tokens = response.prompt_tokens
            completion_tokens = response.completion_tokens
            latency = response.latency_seconds

        proposals, issues = parse_proposals(text, cfg.candidate_limit)
        shown_set = set(shown)
        if self.cache is not None and not hit and any(p.candidate_id in shown_set for p in proposals):
            self.cache.put(key, text)

        self.records.append(
            GenerationRecord(
                model=model,
                cache_hit=hit,
                latency_seconds=latency,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                candidates_shown=len(shown),
                proposals_parsed=len(proposals),
                issues=tuple(issues),
            )
        )
        logger.info(
            "generator model=%s shown=%d parsed=%d issues=%d cache_hit=%s",
            model, len(shown), len(proposals), len(issues), hit,
        )
        return build_result(
            self.name, proposals, candidates, context, latency_seconds=time.perf_counter() - start
        )


def build_generator_selector(
    provider: ModelProvider, config: ModelConfig, *, cache: ResponseCache | None = None
) -> FallbackSelector:
    """Generator with the configured deterministic fallback. Generator is ``.primary``."""
    fallback = FeedbackSelector() if config.fallback_strategy == "feedback" else DeterministicSelector()
    return FallbackSelector(ModelCandidateGenerator(provider, config, cache=cache), fallback)