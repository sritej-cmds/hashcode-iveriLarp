from datetime import datetime, timedelta, timezone

import pytest

from adaptive_restler.models import BudgetState, ExecutionBudget
from adaptive_restler.runner.budget import (
    BudgetExceededError,
    check_limits,
    record_dispatch,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
BUDGET = ExecutionBudget(max_requests=2, max_runtime_seconds=10.0, request_timeout_seconds=1.0)


def state():
    return BudgetState(run_id="r", started_at=T0)


def test_no_limit_reached():
    assert check_limits(state(), BUDGET, T0) is None


def test_request_limit_and_time_limit():
    s = state()
    record_dispatch(s, BUDGET, 2)
    assert check_limits(s, BUDGET, T0) == "request_limit"
    assert check_limits(state(), BUDGET, T0 + timedelta(seconds=10)) == "time_limit"


def test_record_dispatch_refuses_overrun_and_negative():
    s = state()
    with pytest.raises(BudgetExceededError):
        record_dispatch(s, BUDGET, 3)
    assert s.requests_used == 0
    with pytest.raises(ValueError):
        record_dispatch(s, BUDGET, -1)