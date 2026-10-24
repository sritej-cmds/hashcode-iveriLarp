import pytest

from adaptive_restler.models import CandidateRequest, CandidateSequence, TargetConfig
from adaptive_restler.selection.contract import SelectionContext, SelectionResult
from adaptive_restler.selection.selector import DeterministicSelector
from adaptive_restler.selection.summary import summarize_history

TARGET = TargetConfig(base_url="http://localhost:8080", allowed_hosts=["localhost"], dry_run=False)


def cand(cid, steps):
    return CandidateSequence(
        candidate_id=cid,
        sequence=[CandidateRequest(method="GET", path=f"/{cid}/{i}") for i in range(steps)],
        source="generated",
        estimated_request_count=steps,
    )


def test_empty_history():
    s = summarize_history([])
    assert s["selection_rounds"] == 0 and s["decisions_total"] == 0
    assert s["fallbacks_used"] == 0 and s["selector_latency_seconds_total"] == 0.0


def test_counts_rejections_and_fallbacks():
    ctx = SelectionContext(target=TARGET, remaining_requests=2)
    r1 = DeterministicSelector().rank([cand("a", 1), cand("big", 5)], ctx)
    r2 = SelectionResult(
        selector_name="s",
        ranked_ids=[],
        decisions=[],
        fallback_used=True,
        fallback_reason="primary_raised:RuntimeError",
        latency_seconds=0.5,
    )
    s = summarize_history([r1, r2])
    assert s["selection_rounds"] == 2
    assert s["decisions_total"] == 2
    assert s["decisions_valid"] == 1
    assert s["decisions_rejected"] == 1
    assert s["fallbacks_used"] == 1
    assert s["rejected_exceeds_remaining_budget"] == 1
    assert s["selector_latency_seconds_total"] == pytest.approx(0.5 + r1.latency_seconds, abs=1e-5)