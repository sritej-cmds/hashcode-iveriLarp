"""Candidate execution (contract 5.4) with a fake sender for tests.

No real network code here. Real RESTler comes later via a RequestSender.
Never classifies findings. Errors are summarized by type only so no
secret-bearing message text reaches results or artifacts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from adaptive_restler.models import (
    BudgetState,
    CandidateRequest,
    CandidateSequence,
    ExecutionResult,
    ExecutionStatus,
    ExperimentContext,
)
from adaptive_restler.runner.artifacts import redact, write_json_artifact
from adaptive_restler.runner.budget import (
    check_limits,
    elapsed_runtime,
    record_dispatch,
)
from adaptive_restler.runner.target_guard import (
    TargetNotAllowedError,
    check_target_url,
)

_SAFE_RESPONSE_KEYS = ("status_code", "elapsed_seconds", "content_length")


class RequestSender(Protocol):
    def send(
        self,
        *,
        method: str,
        url: str,
        headers: dict[str, str],
        query: dict[str, Any],
        body: Any,
        timeout_seconds: float,
    ) -> dict[str, Any]: ...


class FakeSender:
    """Scripted sender. Items are response dicts or exceptions to raise."""

    def __init__(self, script: Optional[list] = None) -> None:
        self._script = list(script or [])
        self.calls: list[dict[str, str]] = []

    def send(self, *, method, url, headers, query, body, timeout_seconds):
        self.calls.append({"method": method, "url": url})
        item = self._script.pop(0) if self._script else {"status_code": 200}
        if isinstance(item, BaseException):
            raise item
        return item


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _summarize(index: int, req: CandidateRequest, raw: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"step": index, "method": req.method, "path": req.path}
    for key in _SAFE_RESPONSE_KEYS:
        if key in raw:
            summary[key] = raw[key]
    return redact(summary)


def _save_evidence(context, candidate, status, responses, error, sent):
    try:
        path = write_json_artifact(
            context.artifact_dir,
            context.run_id,
            candidate.candidate_id,
            "execution",
            {
                "run_id": context.run_id,
                "candidate_id": candidate.candidate_id,
                "status": status.value,
                "requests_sent": sent,
                "error": error,
                "responses": responses,
            },
        )
        return [path.relative_to(Path(context.artifact_dir)).as_posix()], None
    except (OSError, ValueError):
        return [], "evidence could not be saved"


def execute_candidate(
    candidate: CandidateSequence,
    context: ExperimentContext,
    budget_state: BudgetState,
    sender: Optional[RequestSender] = None,
    now: Callable[[], datetime] = _utc_now,
) -> ExecutionResult:
    """Contract 5.4. `sender` and `now` are optional, for tests and later wiring."""
    started_at = now()
    budget = context.execution_budget
    target = context.target_config
    responses: list[dict[str, Any]] = []
    sent = 0

    def finish(status: ExecutionStatus, error: Optional[str] = None) -> ExecutionResult:
        evidence, note = _save_evidence(
            context, candidate, status, responses, error, sent
        )
        if note:
            error = f"{error}; {note}" if error else note
        return ExecutionResult(
            run_id=context.run_id,
            candidate_id=candidate.candidate_id,
            status=status,
            started_at=started_at,
            duration_seconds=max(0.0, (now() - started_at).total_seconds()),
            request_count=sent,
            responses=responses,
            error_summary=error,
            evidence_paths=evidence,
            budget_exhausted=status is ExecutionStatus.BUDGET_EXHAUSTED,
        )

    # Preflight: everything is validated before any request is sent.
    if budget_state.run_id != context.run_id:
        return finish(ExecutionStatus.FAILED, "budget_state run_id mismatch")
    profile = candidate.auth_profile_id
    if profile is not None and profile != target.auth_profile_id:
        return finish(ExecutionStatus.FAILED, "unauthorized auth_profile_id")
    urls = [target.base_url.rstrip("/") + r.path for r in candidate.sequence]
    try:
        for url in urls:
            check_target_url(url, target.allowed_hosts)
    except TargetNotAllowedError as exc:
        return finish(ExecutionStatus.FAILED, f"target rejected: {exc}")
    except ValueError:
        return finish(ExecutionStatus.FAILED, "invalid target configuration")

    if target.dry_run:
        return finish(ExecutionStatus.SKIPPED, "dry_run: no requests sent")
    if sender is None:
        return finish(ExecutionStatus.FAILED, "no request sender configured")

    for index, (req, url) in enumerate(zip(candidate.sequence, urls)):
        reason = check_limits(budget_state, budget, now())
        if reason:
            budget_state.stop_reason = reason
            return finish(
                ExecutionStatus.BUDGET_EXHAUSTED,
                f"stopped before step {index}: {reason}",
            )
        remaining = budget.max_runtime_seconds - elapsed_runtime(budget_state, now())
        timeout = min(budget.request_timeout_seconds, remaining)
        # Count before sending: conservative if the send fails before dispatch.
        record_dispatch(budget_state, budget)
        sent += 1
        try:
            raw = sender.send(
                method=req.method,
                url=url,
                headers=dict(req.headers),
                query=dict(req.query),
                body=req.body,
                timeout_seconds=timeout,
            )
        except TimeoutError:
            return finish(ExecutionStatus.TIMED_OUT, f"step {index} timed out")
        except Exception as exc:  # message deliberately not recorded
            return finish(
                ExecutionStatus.FAILED, f"step {index} failed: {type(exc).__name__}"
            )
        responses.append(_summarize(index, req, raw))

    return finish(ExecutionStatus.COMPLETED)