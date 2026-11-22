from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tradelab.domain.backtest import (
    BacktestEngine,
    BacktestPeriod,
    InstrumentMetadata,
    MarketSnapshot,
    StrategySpec,
)
from tradelab.domain.dispatcher import Dispatcher
from tradelab.domain.job import BacktestJobConfig, InvalidTransition, Job, JobState
from tradelab.domain.job_lifecycle import JobLifecycle
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.job_runner import JobRunner
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import register_builtin_strategies
from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
    ToolError,
    ToolResult,
)

NOW = datetime(2026, 11, 21, 10, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=30)
DEADLINE = NOW + timedelta(minutes=5)
JOB_ID = "job-1"
WORKER_ID = "worker-1"


class RecordingTool:
    name = "arbitrary_tool"
    version = "4.5.6"
    title = "Произвольная стратегия"
    description = "Подключается одной регистрацией без изменений ядра"

    def __init__(self):
        self.calls = []
        self.error = None

    def execute(self, closed_bars, position, normalized_params):
        self.calls.append((closed_bars, position, normalized_params))
        if self.error is not None:
            raise self.error
        return ToolResult(
            Signal.HOLD,
            closed_bars[-1].close_time,
            SignalReason("hold", "Позиция сохранена"),
            {"window": str(normalized_params["window"])},
        )


class RecordingEngine:
    def __init__(self, delegate, before_run=None):
        self.delegate = delegate
        self.version = delegate.version
        self.before_run = before_run
        self.calls = []

    def run(
        self,
        snapshot,
        period,
        strategy,
        baseline,
        initial_capital,
        commission_rate,
    ):
        self.calls.append(
            (
                snapshot,
                period,
                strategy,
                baseline,
                initial_capital,
                commission_rate,
            )
        )
        if self.before_run is not None:
            self.before_run()
        return self.delegate.run(
            snapshot,
            period,
            strategy,
            baseline,
            initial_capital,
            commission_rate,
        )


class ExplodingEngine:
    version = BacktestEngine.version

    def run(self, *args, **kwargs):
        raise RuntimeError("Сбой вне StrategyTool")


@pytest.fixture
def snapshot(contract_bars):
    return MarketSnapshot(
        "snapshot-1",
        InstrumentMetadata("TEST", "RUB", 10),
        contract_bars[:2],
    )


@pytest.fixture
def registry():
    result = Registry()
    register_builtin_strategies(result)
    return result


@pytest.fixture
def tool(registry):
    result = RecordingTool()
    registry.register(result)
    return result


def make_config(
    snapshot,
    *,
    strategy_version=RecordingTool.version,
    baseline_version="1.0.0",
    engine_version=BacktestEngine.version,
):
    return BacktestJobConfig(
        ticker=snapshot.instrument.ticker,
        period=BacktestPeriod(
            snapshot.bars[0].trading_date,
            snapshot.bars[-1].trading_date,
        ),
        strategy=StrategySpec(
            RecordingTool.name,
            strategy_version,
            NormalizedParams({"window": 3}),
        ),
        baseline=StrategySpec(
            "buy_and_hold", baseline_version, NormalizedParams({})
        ),
        initial_capital=Decimal("1000.00"),
        commission_rate=Decimal("0.001"),
        engine_version=engine_version,
    )


def add_queued_job(repository, config, *, snapshot_id=None):
    job = Job(JOB_ID, NOW, snapshot_id=snapshot_id, config=config)
    repository.add(job.enqueue(NOW))
    return repository.get(JOB_ID)


def make_runner(repository, registry, engine=None):
    lifecycle = JobLifecycle(repository)
    if engine is None:
        engine = BacktestEngine(Dispatcher(registry))
    return JobRunner(lifecycle=lifecycle, registry=registry, engine=engine), lifecycle


def run(runner, snapshot):
    return runner.run(
        JOB_ID,
        snapshot,
        WORKER_ID,
        NOW,
        lease_expires_at=LEASE,
        deadline_at=DEADLINE,
    )


def assert_start_failure(job, code):
    assert job.state is JobState.FAILED
    assert job.error.code == code
    assert job.error.message
    assert job.result is None
    assert job.worker_id is None
    assert job.lease_expires_at is None
    assert job.deadline_at is None
    assert job.started_at is None
    assert [entry.to_state for entry in job.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.FAILED,
    ]


def test_success_runs_config_through_generic_dispatcher_and_completes(
    snapshot, registry, tool
):
    repository = InMemoryJobRepository()
    config = make_config(snapshot)
    add_queued_job(repository, config)
    engine = RecordingEngine(BacktestEngine(Dispatcher(registry)))
    runner, _ = make_runner(repository, registry, engine)

    completed = run(runner, snapshot)

    assert repository.get(JOB_ID) is completed
    assert completed.state is JobState.COMPLETED
    assert completed.error is None
    assert completed.worker_id == WORKER_ID
    assert completed.lease_expires_at == LEASE
    assert completed.deadline_at == DEADLINE
    assert completed.started_at == completed.finished_at == NOW
    assert [entry.to_state for entry in completed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.COMPLETED,
    ]
    assert engine.calls == [
        (
            snapshot,
            config.period,
            config.strategy,
            config.baseline,
            config.initial_capital,
            config.commission_rate,
        )
    ]
    result = completed.result
    assert result.snapshot_id == snapshot.snapshot_id
    assert result.strategy == config.strategy
    assert result.period == config.period
    assert result.engine_version == config.engine_version
    assert result.metrics.buy_and_hold_return is not None
    assert len(tool.calls) == len(snapshot.bars)
    for index, (closed_bars, position, params) in enumerate(tool.calls):
        assert closed_bars == snapshot.bars[: index + 1]
        assert position is Position.FLAT
        assert params is config.strategy.normalized_params


@pytest.mark.parametrize("unavailable", ["primary", "baseline"])
def test_unavailable_exact_strategy_version_fails_before_start_without_current_fallback(
    snapshot, registry, tool, unavailable
):
    versions = {
        "strategy_version": RecordingTool.version,
        "baseline_version": "1.0.0",
    }
    versions["strategy_version" if unavailable == "primary" else "baseline_version"] = (
        "0.0.1"
    )
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot, **versions))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert_start_failure(failed, "strategy_unavailable")
    assert "0.0.1" in failed.error.message
    assert tool.calls == []


def test_unavailable_engine_version_fails_before_start(snapshot, registry, tool):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot, engine_version="0.0.1"))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert_start_failure(failed, "engine_unavailable")
    assert "0.0.1" in failed.error.message
    assert tool.calls == []


@pytest.mark.parametrize("provided", ["different", "missing"])
def test_saved_snapshot_mismatch_or_absence_fails_before_start(
    snapshot, registry, tool, provided
):
    repository = InMemoryJobRepository()
    add_queued_job(
        repository,
        make_config(snapshot),
        snapshot_id=snapshot.snapshot_id,
    )
    runner, _ = make_runner(repository, registry)
    supplied = (
        replace(snapshot, snapshot_id="other-snapshot")
        if provided == "different"
        else None
    )

    failed = run(runner, supplied)

    assert_start_failure(failed, "snapshot_not_found")
    assert failed.snapshot_id == snapshot.snapshot_id
    assert tool.calls == []


def test_unexpected_tool_failure_fails_running_job_without_result(
    snapshot, registry, tool
):
    tool.error = RuntimeError("boom")
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert failed.state is JobState.FAILED
    assert failed.error.code == "tool_failed"
    assert failed.error.message == "Вызов StrategyTool завершился ошибкой"
    assert failed.result is None
    assert [entry.to_state for entry in failed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.FAILED,
    ]


def test_domain_tool_error_preserves_code_and_message(snapshot, registry, tool):
    tool.error = ToolError(
        "insufficient_data",
        "Доменная причина остановки",
        "period",
        "insufficient_data",
    )
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert failed.state is JobState.FAILED
    assert failed.error.code == "insufficient_data"
    assert failed.error.message == "Доменная причина остановки"
    assert failed.result is None


def test_unexpected_exception_outside_tool_becomes_internal_error(
    snapshot, registry, tool
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry, ExplodingEngine())

    failed = run(runner, snapshot)

    assert failed.state is JobState.FAILED
    assert failed.error.code == "internal_error"
    assert failed.error.message
    assert failed.result is None
    assert tool.calls == []


@pytest.mark.parametrize("terminal", ["canceled", "worker_lost"])
@pytest.mark.parametrize("outcome", ["success", "tool_failure"])
def test_late_engine_response_does_not_change_terminal_job(
    snapshot, registry, tool, terminal, outcome
):
    if outcome == "tool_failure":
        tool.error = RuntimeError("Поздняя ошибка")
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    terminal_jobs = []

    def make_terminal():
        if terminal == "canceled":
            terminal_jobs.append(lifecycle.cancel(JOB_ID, NOW))
        else:
            terminal_jobs.append(
                lifecycle.recover_expired(JOB_ID, LEASE + timedelta(microseconds=1))
            )

    engine = RecordingEngine(
        BacktestEngine(Dispatcher(registry)), before_run=make_terminal
    )
    runner, lifecycle = make_runner(repository, registry, engine)

    returned = run(runner, snapshot)

    terminal_job = terminal_jobs[0]
    assert returned is terminal_job
    assert repository.get(JOB_ID) is terminal_job
    assert returned.state is (
        JobState.CANCELED if terminal == "canceled" else JobState.FAILED
    )
    assert returned.history == terminal_job.history
    assert returned.result is None
    if terminal == "worker_lost":
        assert returned.error.code == "worker_lost"
    else:
        assert returned.error is None


def test_cancellation_during_snapshot_record_skips_engine(
    snapshot, registry, tool, monkeypatch
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, lifecycle = make_runner(repository, registry)
    record_snapshot = lifecycle.record_snapshot
    canceled = []

    def cancel_before_record(job_id, worker_id, snapshot_id):
        canceled.append(lifecycle.cancel(job_id, NOW))
        return record_snapshot(job_id, worker_id, snapshot_id)

    monkeypatch.setattr(lifecycle, "record_snapshot", cancel_before_record)

    returned = run(runner, snapshot)

    assert returned is canceled[0]
    assert returned.state is JobState.CANCELED
    assert returned.result is None
    assert tool.calls == []


def test_wrong_worker_start_claim_is_not_masked(snapshot, registry, tool):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, lifecycle = make_runner(repository, registry)
    running = lifecycle.start(
        JOB_ID,
        "owner-worker",
        NOW,
        lease_expires_at=LEASE,
        deadline_at=DEADLINE,
    )

    with pytest.raises(InvalidTransition):
        run(runner, snapshot)

    assert repository.get(JOB_ID) is running
    assert tool.calls == []


def test_terminal_start_claim_is_not_masked(snapshot, registry, tool):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, lifecycle = make_runner(repository, registry)
    canceled = lifecycle.cancel(JOB_ID, NOW)

    with pytest.raises(InvalidTransition):
        run(runner, snapshot)

    assert repository.get(JOB_ID) is canceled
    assert tool.calls == []
