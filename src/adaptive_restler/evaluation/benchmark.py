"""Benchmark orchestration (contract 5.6).

Runs `repetitions` runs of one strategy under an identical budget, applying a
registered reset strategy before each run. Expected operational failures are
recorded in the report's `errors`, never raised and never silently dropped.
"""

from __future__ import annotations

import platform
import random
import sys
from datetime import datetime, timezone
from importlib import metadata
from typing import Callable, Optional

import pydantic

from adaptive_restler.evaluation.report import generate_report, persist_run
from adaptive_restler.models import (
    BenchmarkConfig,
    BenchmarkReport,
    BudgetState,
    CandidateSequence,
    ExecutionResult,
    ExecutionStatus,
    ExperimentContext,
    Finding,
    ValidationContext,
)
from adaptive_restler.runner.artifacts import write_json_artifact
from adaptive_restler.runner.budget import check_limits
from adaptive_restler.runner.executor import RequestSender, execute_candidate
from adaptive_restler.validation.findings import validate_findings

# Picks the next candidate from `remaining`, given what has happened so far.
Selector = Callable[
    [list[CandidateSequence], list[ExecutionResult], list[Finding]], CandidateSequence
]
ResetFn = Callable[[], None]

# Only registered, trusted callables may reset the target.
_RESET_STRATEGIES: dict[str, ResetFn] = {"none": lambda: None}


def register_reset_strategy(strategy_id: str, fn: ResetFn) -> None:
    _RESET_STRATEGIES[strategy_id] = fn


def _baseline_selector(remaining, results, findings):
    return remaining[0]


def _heuristic_selector(remaining, results, findings):
    # Placeholder baseline: cheapest estimated sequence first.
    return min(remaining, key=lambda c: (c.estimated_request_count, c.candidate_id))


def _random_selector(seed: str) -> Selector:
    rng = random.Random(seed)  # str seeds are deterministic across processes

    def select(remaining, results, findings):
        return remaining[rng.randrange(len(remaining))]

    return select


def default_selector(strategy: str, repetition: int) -> Optional[Selector]:
    if strategy == "restler_baseline":
        return _baseline_selector
    if strategy == "heuristic":
        return _heuristic_selector
    if strategy == "random":
        return _random_selector(f"seed-{repetition}")
    return None  # llm_ranker / llm_feedback need an injected selector


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def software_versions() -> dict[str, str]:
    try:
        package = metadata.version("adaptive-restler-planner")
    except metadata.PackageNotFoundError:
        package = "unknown"
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pydantic": pydantic.VERSION,
        "adaptive_restler_planner": package,
    }


def _run_one(
    config: BenchmarkConfig,
    run_id: str,
    candidates: list[CandidateSequence],
    selector: Selector,
    sender: Optional[RequestSender],
    now: Callable[[], datetime],
    policy: str,
) -> list[str]:
    context = ExperimentContext(
        run_id=run_id,
        target_config=config.target_config,
        execution_budget=config.execution_budget,
        artifact_dir=config.artifact_root,
        benchmark_id=config.benchmark_id,
        configuration_version="1",
    )
    validation = ValidationContext(
        run_id=run_id,
        artifact_dir=config.artifact_root,
        benchmark_id=config.benchmark_id,
        expected_authorization_policy=policy,
    )
    state = BudgetState(run_id=run_id, started_at=now())
    results: list[ExecutionResult] = []
    findings: list[Finding] = []
    errors: list[str] = []
    remaining = list(candidates)

    try:
        while remaining:
            if check_limits(state, config.execution_budget, now()):
                break
            candidate = selector(remaining, results, findings)
            if candidate not in remaining:
                raise ValueError("selector returned a candidate that is not remaining")
            remaining.remove(candidate)
            result = execute_candidate(candidate, context, state, sender=sender, now=now)
            results.append(result)
            findings.extend(validate_findings(result, validation))
            if result.status is ExecutionStatus.BUDGET_EXHAUSTED:
                break
    except Exception as exc:  # partial results are still persisted below
        errors.append(f"run {run_id} incomplete: {type(exc).__name__}")

    try:
        persist_run(config.artifact_root, run_id, results, findings)
    except (OSError, ValueError) as exc:
        errors.append(f"run {run_id}: could not persist results ({type(exc).__name__})")
    return errors


def run_benchmark(
    config: BenchmarkConfig,
    *,
    candidates: Optional[list[CandidateSequence]] = None,
    sender: Optional[RequestSender] = None,
    selector: Optional[Selector] = None,
    now: Callable[[], datetime] = _utc_now,
    expected_authorization_policy: str = "not-specified",
) -> BenchmarkReport:
    """Contract 5.6. Extra keyword arguments supply what BenchmarkConfig cannot."""
    errors: list[str] = []
    run_ids = [f"{config.benchmark_id}-r{i + 1:03d}" for i in range(config.repetitions)]

    reset = _RESET_STRATEGIES.get(config.reset_strategy_id)
    if reset is None:
        errors.append(f"unknown reset_strategy_id: {config.reset_strategy_id}")
    if not candidates:
        errors.append("no candidates provided")
    elif len({c.candidate_id for c in candidates}) != len(candidates):
        errors.append("duplicate candidate_id in candidates")
    if selector is None and default_selector(config.strategy, 0) is None:
        errors.append(f"no selector available for strategy {config.strategy}")

    if errors:
        return generate_report(config, run_ids, errors)

    try:
        write_json_artifact(
            config.artifact_root, config.benchmark_id, None, "config",
            config.model_dump(mode="json"),
        )
        write_json_artifact(
            config.artifact_root, config.benchmark_id, None, "environment",
            software_versions(),
        )
    except (OSError, ValueError) as exc:
        errors.append(f"could not record configuration ({type(exc).__name__})")

    for repetition, run_id in enumerate(run_ids):
        try:
            reset()
        except Exception as exc:
            errors.append(f"run {run_id}: reset failed ({type(exc).__name__})")
            continue
        chosen = selector or default_selector(config.strategy, repetition)
        errors.extend(
            _run_one(
                config, run_id, candidates, chosen, sender, now,
                expected_authorization_policy,
            )
        )

    return generate_report(config, run_ids, errors)