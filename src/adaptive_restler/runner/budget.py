"""Deterministic request/time budget accounting.

Enforced in plain code. The LLM never sees or modifies these limits.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from adaptive_restler.models import BudgetState, ExecutionBudget

REASON_REQUEST_LIMIT = "request_limit"
REASON_TIME_LIMIT = "time_limit"

class BudgetExceededError(Exception):
    """Raised when an action would exceed, or already exceeds, the budget."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(message or f"Budget exceeded: {reason}")


@dataclass
class BudgetTracker:
    max_requests: int
    max_seconds: float
    clock: Callable[[], float] = time.monotonic
    requests_used: int = 0
    _start: float = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_requests < 0:
            raise ValueError("max_requests must be >= 0")
        if self.max_seconds < 0:
            raise ValueError("max_seconds must be >= 0")
        if self.requests_used < 0:
            raise ValueError("requests_used must be >= 0")
        self._start = self.clock()

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.clock() - self._start)

    @property
    def remaining_requests(self) -> int:
        return max(0, self.max_requests - self.requests_used)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed_seconds)

    def exhausted_reason(self) -> Optional[str]:
        if self.requests_used >= self.max_requests:
            return REASON_REQUEST_LIMIT
        if self.elapsed_seconds >= self.max_seconds:
            return REASON_TIME_LIMIT
        return None

    @property
    def is_exhausted(self) -> bool:
        return self.exhausted_reason() is not None

    def can_afford(self, requests: int) -> bool:
        if requests < 0:
            raise ValueError("requests must be >= 0")
        return (
            requests <= self.remaining_requests
            and self.elapsed_seconds < self.max_seconds
        )

    def consume_requests(self, requests: int = 1) -> None:
        """Record requests. All-or-nothing: nothing is consumed on failure."""
        if requests < 0:
            raise ValueError("requests must be >= 0")
        if self.elapsed_seconds >= self.max_seconds:
            raise BudgetExceededError(REASON_TIME_LIMIT)
        if requests > self.remaining_requests:
            raise BudgetExceededError(
                REASON_REQUEST_LIMIT,
                f"Need {requests} requests, only {self.remaining_requests} left",
            )
        self.requests_used += requests

    def snapshot(self) -> dict:
        """Plain dict for logging/artifacts (no secrets)."""
        return {
            "max_requests": self.max_requests,
            "max_seconds": self.max_seconds,
            "requests_used": self.requests_used,
            "elapsed_seconds": round(self.elapsed_seconds, 6),
            "exhausted_reason": self.exhausted_reason(),
        }
    # --- Helpers operating on the shared, runner-owned BudgetState (models.py) ---


def elapsed_runtime(state: BudgetState, now: datetime) -> float:
    return max(0.0, (now - state.started_at).total_seconds())


def check_limits(
    state: BudgetState, budget: ExecutionBudget, now: datetime
) -> Optional[str]:
    """Return a stop reason if a limit is reached, else None."""
    if state.requests_used >= budget.max_requests:
        return REASON_REQUEST_LIMIT
    if elapsed_runtime(state, now) >= budget.max_runtime_seconds:
        return REASON_TIME_LIMIT
    return None


def record_dispatch(
    state: BudgetState, budget: ExecutionBudget, count: int = 1
) -> None:
    """Record dispatched requests. Never exceeds the limit or goes negative."""
    if count < 0:
        raise ValueError("count must be >= 0")
    if state.requests_used + count > budget.max_requests:
        raise BudgetExceededError(REASON_REQUEST_LIMIT)
    state.requests_used += count