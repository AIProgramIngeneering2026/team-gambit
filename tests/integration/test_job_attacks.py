from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest

from tradelab.domain.job import InvalidTransition, Job, JobFailure, JobState
from tradelab.domain.job_lifecycle import JobLifecycle
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import BuyAndHoldTool, SmaCrossoverTool
from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Signal,
    SignalReason,
    ToolResult,
)

WAIT_TIMEOUT = 5


class BlockingHoldTool:
    name = "blocking_hold"
    version = "1.0.0"
    title = "Блокируемая тестовая стратегия"
    description = "Позволяет синхронно проверить поздние ответы и гонки worker"

    def __init__(self):
        self.calls = 0
        self.entered = Event()
        self.release = Event()
        self.error = None

    def params_schema(self):
        return {"type": "object"}

    def validate_params(self, params):
        return NormalizedParams(dict(params))

    def required_bars(self, normalized_params):
        return 1

    def execute(self, closed_bars, position, normalized_params):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=WAIT_TIMEOUT):
            raise TimeoutError("Тест не освободил блокированный StrategyTool")
        if self.error is not None:
            raise self.error
        return ToolResult(
            Signal.HOLD,
            closed_bars[-1].close_time,
            SignalReason("blocking_hold", "Блокированный Tool завершил работу"),
            {},
        )


class CountingSmaTool(SmaCrossoverTool):
    def __init__(self):
        self.calls = 0

    def execute(self, closed_bars, position, normalized_params):
        self.calls += 1
        return super().execute(closed_bars, position, normalized_params)


def _make_blocking_core(core_factory, one_bar_snapshot):
    registry = Registry()
    tool = BlockingHoldTool()
    registry.register(tool)
    registry.register(BuyAndHoldTool())
    core = core_factory(registry=registry, snapshot=one_bar_snapshot)
    return core, tool


def _submit_blocked_run(pool, core, tool, job_id):
    future = pool.submit(core.run_job, job_id)
    assert tool.entered.wait(timeout=WAIT_TIMEOUT)
    running = core.repository.get(job_id)
    assert running.state is JobState.RUNNING
    assert running.worker_id == "worker-1"
    return future, running


def test_z1_concurrent_runners_claim_job_and_run_primary_tool_once(
    core_factory, one_bar_snapshot
):
    core, tool = _make_blocking_core(core_factory, one_bar_snapshot)
    queued = core.create_job(strategy=tool.name, params={})
    second_runner = core.new_runner(lifecycle=JobLifecycle(core.repository))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(core.run_job, queued.id, worker_id="worker-1")
        try:
            assert tool.entered.wait(timeout=WAIT_TIMEOUT)
            running = core.repository.get(queued.id)
            assert running.state is JobState.RUNNING
            second = pool.submit(
                core.run_job,
                queued.id,
                worker_id="worker-2",
                runner=second_runner,
            )
            with pytest.raises(InvalidTransition):
                second.result(timeout=WAIT_TIMEOUT)
            assert tool.calls == 1
            assert core.repository.get(queued.id) is running
        finally:
            tool.release.set()
        completed = first.result(timeout=WAIT_TIMEOUT)

    assert completed.state is JobState.COMPLETED
    assert tool.calls == 1
    assert completed.result.strategy.name == tool.name
    assert len(completed.result.signals) == 1
    assert [entry.to_state for entry in completed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.COMPLETED,
    ]


def test_z2_restarted_lifecycle_recovers_expired_worker_and_discards_late_result(
    core_factory, one_bar_snapshot
):
    """Пересоздаются объекты над сохранённым repository, а не память процесса."""
    core, tool = _make_blocking_core(core_factory, one_bar_snapshot)
    queued = core.create_job(strategy=tool.name, params={})

    with ThreadPoolExecutor(max_workers=1) as pool:
        first, running = _submit_blocked_run(pool, core, tool, queued.id)
        try:
            expired_at = core.clock.advance(timedelta(seconds=31))
            restarted_lifecycle = JobLifecycle(core.repository)
            restarted_runner = core.new_runner(lifecycle=restarted_lifecycle)
            with pytest.raises(InvalidTransition):
                core.run_job(
                    queued.id,
                    worker_id="worker-2",
                    now=expired_at,
                    runner=restarted_runner,
                )
            assert core.repository.get(queued.id) is running

            recovered = restarted_lifecycle.recover_expired(queued.id, expired_at)
            assert recovered is not None
            assert recovered.state is JobState.FAILED
            assert recovered.error.code == "worker_lost"
            assert recovered.result is None
            terminal_history = recovered.history
            terminal_error = recovered.error
        finally:
            tool.release.set()
        late = first.result(timeout=WAIT_TIMEOUT)

    assert late is recovered
    assert core.repository.get(queued.id) is recovered
    assert late.state is JobState.FAILED
    assert late.error is terminal_error
    assert late.error.code == "worker_lost"
    assert late.result is None
    assert late.history == terminal_history
    assert len(late.history) == 4
    assert tool.calls == 1


def _assert_cancel_discards_late_response(core_factory, one_bar_snapshot, error):
    core, tool = _make_blocking_core(core_factory, one_bar_snapshot)
    tool.error = error
    queued = core.create_job(strategy=tool.name, params={})

    with ThreadPoolExecutor(max_workers=1) as pool:
        first, _ = _submit_blocked_run(pool, core, tool, queued.id)
        try:
            canceled = core.lifecycle.cancel(
                queued.id,
                core.clock.advance(timedelta(seconds=1)),
            )
            assert canceled.state is JobState.CANCELED
            assert canceled.result is None
            assert canceled.error is None
            terminal_history = canceled.history
        finally:
            tool.release.set()
        late = first.result(timeout=WAIT_TIMEOUT)

    assert late is canceled
    assert core.repository.get(queued.id) is canceled
    assert late.state is JobState.CANCELED
    assert late.result is None
    assert late.error is None
    assert late.history == terminal_history
    assert len(late.history) == 4
    assert tool.calls == 1


def test_z3_cancel_during_tool_discards_late_success(
    core_factory, one_bar_snapshot
):
    _assert_cancel_discards_late_response(core_factory, one_bar_snapshot, None)


def test_z3_cancel_during_tool_discards_late_failure(
    core_factory, one_bar_snapshot
):
    _assert_cancel_discards_late_response(
        core_factory,
        one_bar_snapshot,
        RuntimeError("Поздняя ошибка Tool"),
    )


def test_z4_failure_during_tool_preserves_original_failure_after_late_success(
    core_factory, one_bar_snapshot
):
    core, tool = _make_blocking_core(core_factory, one_bar_snapshot)
    queued = core.create_job(strategy=tool.name, params={})

    with ThreadPoolExecutor(max_workers=1) as pool:
        first, _ = _submit_blocked_run(pool, core, tool, queued.id)
        try:
            failed = core.lifecycle.fail(
                queued.id,
                "worker-1",
                JobFailure("internal_error", "Исходный сбой выполнения"),
                core.clock.advance(timedelta(seconds=1)),
            )
            original_history = failed.history
            original_error = failed.error
            assert failed.result is None
        finally:
            tool.release.set()
        late = first.result(timeout=WAIT_TIMEOUT)

    assert late is failed
    assert core.repository.get(queued.id) is failed
    assert late.state is JobState.FAILED
    assert late.error is original_error
    assert late.error.code == "internal_error"
    assert late.error.message == "Исходный сбой выполнения"
    assert late.result is None
    assert late.history == original_history
    assert len(late.history) == 4
    assert tool.calls == 1


def _assert_complete_before_start_is_rejected(core, initial_state):
    queued = core.create_job(
        strategy="sma_crossover",
        params={"fast": 2, "slow": 3},
    )
    config = queued.config
    result = core.engine.run(
        snapshot=core.snapshot,
        period=config.period,
        strategy=config.strategy,
        baseline=config.baseline,
        initial_capital=config.initial_capital,
        commission_rate=config.commission_rate,
    )

    if initial_state is JobState.CREATED:
        repository = InMemoryJobRepository()
        candidate = Job(queued.id, queued.created_at, config=config)
        repository.add(candidate)
        lifecycle = JobLifecycle(repository)
    else:
        repository = core.repository
        candidate = queued
        lifecycle = core.lifecycle

    before = repository.get(candidate.id)
    with pytest.raises(InvalidTransition) as raised:
        lifecycle.complete(candidate.id, "worker-1", result, core.clock())

    assert raised.value.details == {
        "job_id": candidate.id,
        "from_state": initial_state.value,
        "to_state": JobState.COMPLETED.value,
    }
    after = repository.get(candidate.id)
    assert after is before
    assert after.state is initial_state
    assert after.history == before.history
    assert after.result is None
    assert after.error is None


def test_z5_complete_from_created_is_explicitly_rejected(core):
    _assert_complete_before_start_is_rejected(core, JobState.CREATED)


def test_z5_complete_from_queued_is_explicitly_rejected(core):
    _assert_complete_before_start_is_rejected(core, JobState.QUEUED)


def test_z6_completed_job_does_not_rerun_and_new_job_is_reproducible(core_factory):
    registry = Registry()
    tool = CountingSmaTool()
    registry.register(tool)
    registry.register(BuyAndHoldTool())
    core = core_factory(registry=registry)
    params = {"fast": 2, "slow": 3}
    first = core.create_job(strategy=tool.name, params=params)
    completed = core.run_job(first.id)
    original_result = completed.result
    original_history = completed.history
    calls_after_first_run = tool.calls

    with pytest.raises(InvalidTransition):
        core.run_job(completed.id, worker_id="worker-2")

    assert tool.calls == calls_after_first_run
    assert core.repository.get(completed.id) is completed
    assert completed.result is original_result
    assert completed.history == original_history

    second = core.create_job(strategy=tool.name, params=params)
    repeated = core.run_job(second.id, worker_id="worker-2")

    assert repeated.id != completed.id
    assert repeated.state is JobState.COMPLETED
    assert repeated.result.metrics == original_result.metrics
    assert repeated.result.equity_curve == original_result.equity_curve
    assert repeated.result.trades == original_result.trades
    assert repeated.result.signals == original_result.signals
    assert repeated.result.final_position == original_result.final_position
    assert repeated.result.final_cash == original_result.final_cash
    assert repeated.result.final_quantity == original_result.final_quantity
    assert repeated.result.snapshot_id == original_result.snapshot_id
    assert repeated.result.period == original_result.period
    assert repeated.result.engine_version == original_result.engine_version
    assert dict(repeated.result.strategy.normalized_params) == dict(
        original_result.strategy.normalized_params
    )
    assert tool.calls == calls_after_first_run * 2
    assert core.repository.get(completed.id) is completed
    assert completed.result is original_result
    assert completed.history == original_history
