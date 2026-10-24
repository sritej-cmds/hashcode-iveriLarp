from datetime import datetime, timedelta, timezone

from adaptive_restler.models import (
    BudgetState,
    CandidateRequest,
    CandidateSequence,
    ExecutionBudget,
    ExecutionResult,
    ExecutionStatus,
    ExperimentContext,
    TargetConfig,
)
from adaptive_restler.runner.executor import FakeSender, execute_candidate


class Clock:
    def __init__(self):
        self.t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += timedelta(seconds=seconds)


class SlowSender:
    def __init__(self, clock, step):
        self.clock, self.step, self.calls = clock, step, []

    def send(self, **kw):
        self.calls.append(kw)
        self.clock.advance(self.step)
        return {"status_code": 200}


def make_context(tmp_path, dry_run=False, max_requests=10, max_runtime=60.0):
    return ExperimentContext(
        run_id="run1",
        target_config=TargetConfig(
            base_url="http://localhost:8080",
            allowed_hosts=["localhost"],
            dry_run=dry_run,
        ),
        execution_budget=ExecutionBudget(
            max_requests=max_requests,
            max_runtime_seconds=max_runtime,
            request_timeout_seconds=5.0,
        ),
        artifact_dir=str(tmp_path),
        benchmark_id="bench1",
        configuration_version="v1",
    )


def make_candidate(steps=3, auth=None):
    return CandidateSequence(
        candidate_id="c1",
        sequence=[CandidateRequest(method="GET", path=f"/items/{i}") for i in range(steps)],
        source="generated",
        estimated_request_count=steps,
        auth_profile_id=auth,
    )


def run(tmp_path, candidate=None, sender=None, clock=None, **ctx_kw):
    clock = clock or Clock()
    ctx = make_context(tmp_path, **ctx_kw)
    state = BudgetState(run_id="run1", started_at=clock())
    cand = candidate or make_candidate()
    result = execute_candidate(cand, ctx, state, sender=sender, now=clock)
    return result, state, ctx


def test_completed_counts_and_evidence(tmp_path):
    sender = FakeSender([{"status_code": 200, "authorization": "x"}])
    result, state, _ = run(tmp_path, sender=sender)
    assert result.status is ExecutionStatus.COMPLETED
    assert result.request_count == 3 == state.requests_used == len(sender.calls)
    assert "authorization" not in result.responses[0]
    assert result.evidence_paths == ["run1/c1/execution.json"]
    assert (tmp_path / "run1" / "c1" / "execution.json").exists()
    assert not result.budget_exhausted


def test_result_json_round_trip(tmp_path):
    result, _, _ = run(tmp_path, sender=FakeSender())
    assert ExecutionResult.model_validate_json(result.model_dump_json()) == result


def test_request_budget_exhausted_keeps_partial_results(tmp_path):
    sender = FakeSender()
    result, state, _ = run(tmp_path, sender=sender, max_requests=2)
    assert result.status is ExecutionStatus.BUDGET_EXHAUSTED
    assert result.budget_exhausted
    assert result.request_count == 2 == len(sender.calls)
    assert len(result.responses) == 2
    assert state.stop_reason == "request_limit"


def test_time_budget_exhausted(tmp_path):
    clock = Clock()
    sender = SlowSender(clock, 40)
    result, state, _ = run(tmp_path, sender=sender, clock=clock, max_runtime=50.0)
    assert result.status is ExecutionStatus.BUDGET_EXHAUSTED
    assert result.request_count == 2
    assert state.stop_reason == "time_limit"


def test_per_request_timeout_is_capped(tmp_path):
    clock = Clock()
    sender = SlowSender(clock, 0)
    run(tmp_path, sender=sender, clock=clock)
    assert all(c["timeout_seconds"] <= 5.0 for c in sender.calls)


def test_timeout_recorded(tmp_path):
    sender = FakeSender([{"status_code": 200}, TimeoutError()])
    result, _, _ = run(tmp_path, sender=sender)
    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.request_count == 2
    assert len(result.responses) == 1
    assert not result.budget_exhausted


def test_failure_does_not_leak_exception_message(tmp_path):
    sender = FakeSender([RuntimeError("secret-token-123")])
    result, _, _ = run(tmp_path, sender=sender)
    assert result.status is ExecutionStatus.FAILED
    assert "secret-token-123" not in (result.error_summary or "")
    text = (tmp_path / "run1" / "c1" / "execution.json").read_text(encoding="utf-8")
    assert "secret-token-123" not in text


def test_dry_run_sends_nothing(tmp_path):
    sender = FakeSender()
    result, state, _ = run(tmp_path, sender=sender, dry_run=True)
    assert result.status is ExecutionStatus.SKIPPED
    assert result.request_count == 0 == state.requests_used
    assert sender.calls == []


def test_unauthorized_auth_profile_rejected_before_sending(tmp_path):
    sender = FakeSender()
    result, _, _ = run(tmp_path, candidate=make_candidate(auth="other"), sender=sender)
    assert result.status is ExecutionStatus.FAILED
    assert sender.calls == []


def test_target_outside_allowlist_rejected_before_sending(tmp_path):
    sender = FakeSender()
    ctx = make_context(tmp_path)
    bad_target = ctx.target_config.model_copy(update={"allowed_hosts": ["other.com"]})
    ctx = ctx.model_copy(update={"target_config": bad_target})
    state = BudgetState(run_id="run1", started_at=datetime.now(timezone.utc))
    result = execute_candidate(make_candidate(), ctx, state, sender=sender)
    assert result.status is ExecutionStatus.FAILED
    assert "target rejected" in result.error_summary
    assert sender.calls == []


def test_run_id_mismatch_rejected(tmp_path):
    ctx = make_context(tmp_path)
    state = BudgetState(run_id="other", started_at=datetime.now(timezone.utc))
    result = execute_candidate(make_candidate(), ctx, state, sender=FakeSender())
    assert result.status is ExecutionStatus.FAILED
    assert state.requests_used == 0


def test_no_sender_when_live_fails_safely(tmp_path):
    result, _, _ = run(tmp_path, sender=None)
    assert result.status is ExecutionStatus.FAILED
    assert result.request_count == 0