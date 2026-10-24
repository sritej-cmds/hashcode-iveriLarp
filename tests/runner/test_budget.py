import pytest

from adaptive_restler.runner.budget import (
    REASON_REQUEST_LIMIT,
    REASON_TIME_LIMIT,
    BudgetExceededError,
    BudgetState,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make(max_requests=10, max_seconds=60.0):
    clock = FakeClock()
    return BudgetState(max_requests, max_seconds, clock=clock), clock


def test_fresh_budget_not_exhausted():
    b, _ = make()
    assert not b.is_exhausted
    assert b.remaining_requests == 10
    assert b.remaining_seconds == 60.0


def test_consume_reduces_remaining():
    b, _ = make()
    b.consume_requests(3)
    assert b.requests_used == 3
    assert b.remaining_requests == 7


def test_consume_exactly_to_limit_then_exhausted():
    b, _ = make(max_requests=5)
    b.consume_requests(5)
    assert b.is_exhausted
    assert b.exhausted_reason() == REASON_REQUEST_LIMIT


def test_over_consume_raises_and_consumes_nothing():
    b, _ = make(max_requests=5)
    b.consume_requests(4)
    with pytest.raises(BudgetExceededError) as exc:
        b.consume_requests(2)
    assert exc.value.reason == REASON_REQUEST_LIMIT
    assert b.requests_used == 4


def test_time_limit_enforced():
    b, clock = make(max_seconds=30.0)
    clock.advance(29.9)
    assert not b.is_exhausted
    clock.advance(0.1)
    assert b.exhausted_reason() == REASON_TIME_LIMIT
    with pytest.raises(BudgetExceededError) as exc:
        b.consume_requests(1)
    assert exc.value.reason == REASON_TIME_LIMIT
    assert b.requests_used == 0


def test_can_afford():
    b, clock = make(max_requests=5, max_seconds=10.0)
    assert b.can_afford(5)
    assert not b.can_afford(6)
    clock.advance(10.0)
    assert not b.can_afford(1)


def test_zero_budget_is_immediately_exhausted():
    b, _ = make(max_requests=0)
    assert b.is_exhausted


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        BudgetState(-1, 10.0)
    with pytest.raises(ValueError):
        BudgetState(1, -1.0)
    b, _ = make()
    with pytest.raises(ValueError):
        b.consume_requests(-1)
    with pytest.raises(ValueError):
        b.can_afford(-1)


def test_snapshot_contents():
    b, clock = make()
    b.consume_requests(2)
    clock.advance(5)
    snap = b.snapshot()
    assert snap["requests_used"] == 2
    assert snap["elapsed_seconds"] == 5.0
    assert snap["exhausted_reason"] is None