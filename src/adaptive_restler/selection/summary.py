"""Summarize selection rounds for run records (plain, JSON-safe values)."""

from collections import Counter
from collections.abc import Sequence

from adaptive_restler.selection.contract import CandidateStatus, SelectionResult

__all__ = ["summarize_history"]


def summarize_history(history: Sequence[SelectionResult]) -> dict[str, int | float]:
    """Counts across all rounds. ``decisions_total`` counts a candidate once per round."""
    rejections: Counter[str] = Counter()
    valid = 0
    for result in history:
        for decision in result.decisions:
            if decision.status is CandidateStatus.VALID:
                valid += 1
            elif decision.rejection_code is not None:
                rejections[decision.rejection_code.value] += 1

    summary: dict[str, int | float] = {
        "selection_rounds": len(history),
        "decisions_total": sum(len(r.decisions) for r in history),
        "decisions_valid": valid,
        "decisions_rejected": sum(rejections.values()),
        "fallbacks_used": sum(1 for r in history if r.fallback_used),
        "selector_latency_seconds_total": round(sum(r.latency_seconds for r in history), 6),
    }
    for code in sorted(rejections):
        summary[f"rejected_{code}"] = rejections[code]
    return summary