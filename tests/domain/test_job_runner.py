from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradelab.domain.backtest import (
    BacktestEngine,
    BacktestPeriod,
    InstrumentMetadata,
    MarketSnapshot,
    StrategySpec,
)
from tradelab.domain.dispatcher import Dispatcher
from tradelab.domain.job import (
    BacktestJobConfig,
    InvalidTransition,
    Job,
    JobFailure,
    JobState,
)
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
        **run_options,
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
            **run_options,
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
    runner = JobRunner(
        lifecycle=lifecycle,
        registry=registry,
        engine=engine,
        clock=lambda: NOW,
    )
    return runner, lifecycle


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


def assert_running_failure(job, code, finished_at=NOW):
    assert job.state is JobState.FAILED
    assert job.error.code == code
    assert job.error.message.strip()
    assert job.result is None
    assert job.worker_id == WORKER_ID
    assert job.started_at == NOW
    assert job.finished_at == finished_at
    assert [entry.to_state for entry in job.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.FAILED,
    ]


def test_snapshot_ticker_mismatch_fails_after_start_before_any_tool(
    snapshot, registry, tool
):
    repository = InMemoryJobRepository()
    config = replace(make_config(snapshot), ticker="OTHER")
    add_queued_job(repository, config)
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert_running_failure(failed, "invalid_market_data")
    assert failed.snapshot_id is None
    assert tool.calls == []


def test_fresh_missing_snapshot_is_market_data_failure_after_start(
    snapshot, registry, tool
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, None)

    assert_running_failure(failed, "market_data_unavailable")
    assert failed.snapshot_id is None
    assert tool.calls == []


def test_empty_snapshot_id_fails_after_start_before_record_and_tool(
    snapshot, registry, tool
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, replace(snapshot, snapshot_id=""))

    assert_running_failure(failed, "invalid_market_data")
    assert failed.snapshot_id is None
    assert tool.calls == []


class EmptyResultEngine:
    version = BacktestEngine.version

    def run(self, *args, **kwargs):
        return None


def test_none_engine_result_becomes_internal_error_instead_of_staying_running(
    snapshot, registry, tool
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry, EmptyResultEngine())

    failed = run(runner, snapshot)

    assert_running_failure(failed, "internal_error")
    assert tool.calls == []


@pytest.mark.parametrize(
    "code",
    [
        "market_data_unavailable",
        "invalid_market_data",
        "insufficient_data",
        "tool_failed",
        "internal_error",
    ],
)
def test_allowed_execution_tool_error_codes_are_preserved(
    snapshot, registry, tool, code
):
    tool.error = ToolError(code, f"Доменное сообщение: {code}", "tool.result", "domain")
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert_running_failure(failed, code)
    assert failed.error.message == f"Доменное сообщение: {code}"


@pytest.mark.parametrize(
    "code",
    [
        "unknown_operation",
        "invalid_params",
        "strategy_unavailable",
        "engine_unavailable",
        "snapshot_not_found",
        "job_timeout",
        "worker_lost",
        "made_up",
    ],
)
def test_forbidden_unknown_and_reaper_tool_error_codes_become_tool_failed(
    snapshot, registry, tool, code
):
    tool.error = ToolError(code, "Недопустимая ошибка Tool", "tool.result", "domain")
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert_running_failure(failed, "tool_failed")
    assert code in failed.error.message


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("", "Код ошибки не задан"),
        ("tool_failed", ""),
        ("insufficient_data", " "),
        ("made_up", ""),
    ],
)
def test_malformed_tool_error_never_leaves_job_running(
    snapshot, registry, tool, code, message
):
    tool.error = ToolError(code, message, "tool.result", "domain")
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)

    failed = run(runner, snapshot)

    assert_running_failure(failed, "tool_failed")


def test_configless_job_remains_explicit_programming_error(snapshot, registry):
    repository = InMemoryJobRepository()
    queued = add_queued_job(repository, None)
    runner, _ = make_runner(repository, registry)

    with pytest.raises(ValueError, match="конфигурация"):
        run(runner, snapshot)

    assert repository.get(JOB_ID) is queued
    assert queued.state is JobState.QUEUED


@pytest.mark.parametrize("terminal", ["canceled", "failed", "worker_lost"])
@pytest.mark.parametrize("outcome", ["success", "tool_failure"])
def test_real_terminal_transition_inside_tool_discards_late_response(
    snapshot, registry, tool, terminal, outcome, monkeypatch
):
    if outcome == "tool_failure":
        tool.error = RuntimeError("Поздняя ошибка из Tool")
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, lifecycle = make_runner(repository, registry)
    terminal_attempts = []
    terminal_jobs = []
    complete_attempts = []
    fail_attempts = []

    def make_terminal():
        terminal_attempts.append(terminal)
        if terminal == "canceled":
            terminal_jobs.append(lifecycle.cancel(JOB_ID, NOW))
        elif terminal == "failed":
            terminal_jobs.append(
                lifecycle.fail(
                    JOB_ID,
                    WORKER_ID,
                    JobFailure("internal_error", "Внешний сбой выполнения"),
                    NOW,
                )
            )
        else:
            terminal_jobs.append(
                lifecycle.recover_expired(JOB_ID, LEASE + timedelta(microseconds=1))
            )

    complete = lifecycle.complete
    fail = lifecycle.fail

    def counted_complete(*args):
        complete_attempts.append(len(tool.calls))
        return complete(*args)

    def counted_fail(*args):
        fail_attempts.append(args[2].code)
        return fail(*args)

    monkeypatch.setattr(lifecycle, "complete", counted_complete)
    monkeypatch.setattr(lifecycle, "fail", counted_fail)

    execute = tool.execute

    def execute_with_real_transition(*args):
        if not terminal_attempts:
            make_terminal()
        return execute(*args)

    monkeypatch.setattr(tool, "execute", execute_with_real_transition)

    returned = run(runner, snapshot)

    assert terminal_attempts == [terminal]
    assert len(terminal_jobs) == 1
    terminal_job = terminal_jobs[0]
    assert returned is terminal_job
    assert repository.get(JOB_ID) is terminal_job
    assert returned.result is None
    assert returned.history == terminal_job.history
    assert len(returned.history) == 4
    assert returned.history[-1].to_state is {
        "canceled": JobState.CANCELED,
        "failed": JobState.FAILED,
        "worker_lost": JobState.FAILED,
    }[terminal]
    if terminal == "canceled":
        assert returned.error is None
    elif terminal == "failed":
        assert returned.error.message == "Внешний сбой выполнения"
    else:
        assert returned.error.code == "worker_lost"

    expected_failures = ["tool_failed"] if outcome == "tool_failure" else []
    if terminal == "failed":
        expected_failures.insert(0, "internal_error")
    assert fail_attempts == expected_failures
    if outcome == "success":
        assert len(tool.calls) == len(snapshot.bars)
        assert complete_attempts == [len(snapshot.bars)]
    else:
        assert len(tool.calls) == 1
        assert complete_attempts == []


def test_repository_exception_during_completion_is_not_masked(
    snapshot, registry, tool, monkeypatch
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    runner, _ = make_runner(repository, registry)
    compare_and_set = repository.compare_and_set
    running_writes = 0

    def fail_final_write(job_id, expected_state, change, **kwargs):
        nonlocal running_writes
        if expected_state is JobState.RUNNING:
            running_writes += 1
            if running_writes == 2:
                raise RuntimeError("Репозиторий недоступен")
        return compare_and_set(job_id, expected_state, change, **kwargs)

    monkeypatch.setattr(repository, "compare_and_set", fail_final_write)

    with pytest.raises(RuntimeError, match="Репозиторий недоступен"):
        run(runner, snapshot)


class StaticResultEngine:
    version = BacktestEngine.version

    def __init__(self, result):
        self.result = result
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        return self.result


class ControlledClock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def make_timed_runner(repository, registry, clock, engine=None):
    lifecycle = JobLifecycle(repository)
    if engine is None:
        engine = BacktestEngine(Dispatcher(registry))
    runner = JobRunner(
        lifecycle=lifecycle,
        registry=registry,
        engine=engine,
        clock=clock,
    )
    return runner, lifecycle


def advance_clock_after_tool(tool, clock, at, monkeypatch):
    execute = tool.execute

    def execute_and_advance(*args):
        result = execute(*args)
        clock.now = at
        return result

    monkeypatch.setattr(tool, "execute", execute_and_advance)


def test_runner_rejects_noncanonical_engine_result(snapshot, registry, tool):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    engine = StaticResultEngine(SimpleNamespace(snapshot_id=snapshot.snapshot_id))
    runner, _ = make_runner(repository, registry, engine)

    failed = run(runner, snapshot)

    assert_running_failure(failed, "internal_error")
    assert engine.calls == 1
    assert repository.get(JOB_ID) is failed


def test_runner_rejects_canonical_result_for_another_snapshot(
    snapshot, registry, tool
):
    config = make_config(snapshot)
    result = BacktestEngine(Dispatcher(registry)).run(
        snapshot=snapshot,
        period=config.period,
        strategy=config.strategy,
        baseline=config.baseline,
        initial_capital=config.initial_capital,
        commission_rate=config.commission_rate,
    )
    tool.calls.clear()
    repository = InMemoryJobRepository()
    add_queued_job(repository, config)
    engine = StaticResultEngine(replace(result, snapshot_id="other-snapshot"))
    runner, _ = make_runner(repository, registry, engine)

    failed = run(runner, snapshot)

    assert_running_failure(failed, "internal_error")
    assert engine.calls == 1
    assert tool.calls == []
    assert repository.get(JOB_ID) is failed


def test_successful_completion_uses_actual_clock(
    snapshot, registry, tool, monkeypatch
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    clock = ControlledClock()
    runner, _ = make_timed_runner(repository, registry, clock)
    advanced = NOW + timedelta(seconds=20)
    advance_clock_after_tool(tool, clock, advanced, monkeypatch)

    completed = run(runner, snapshot)

    assert completed.state is JobState.COMPLETED
    assert completed.started_at == NOW
    assert completed.finished_at == advanced
    assert len(tool.calls) == len(snapshot.bars)


def test_deadline_between_tool_calls_prevents_next_tool(
    snapshot, registry, tool, monkeypatch
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    clock = ControlledClock()
    runner, _ = make_timed_runner(repository, registry, clock)
    expired = DEADLINE + timedelta(seconds=1)
    advance_clock_after_tool(tool, clock, expired, monkeypatch)

    failed = run(runner, snapshot)

    assert_running_failure(failed, "job_timeout", expired)
    assert len(tool.calls) == 1


def test_deadline_after_last_tool_prevents_success(
    snapshot, registry, tool, monkeypatch
):
    one_bar = replace(snapshot, bars=snapshot.bars[:1])
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(one_bar))
    clock = ControlledClock()
    runner, _ = make_timed_runner(repository, registry, clock)
    expired = DEADLINE + timedelta(seconds=1)
    baseline = registry.get("buy_and_hold", "1.0.0")
    execute_baseline = baseline.execute
    baseline_calls = []

    def execute_baseline_and_advance(*args):
        result = execute_baseline(*args)
        baseline_calls.append(args[0])
        clock.now = expired
        return result

    monkeypatch.setattr(baseline, "execute", execute_baseline_and_advance)

    failed = run(runner, one_bar)

    assert_running_failure(failed, "job_timeout", expired)
    assert len(tool.calls) == 1
    assert len(baseline_calls) == 1


@pytest.mark.parametrize("terminal", ["canceled", "failed", "worker_lost"])
def test_job_timeout_does_not_replace_existing_terminal_job(
    snapshot, registry, tool, terminal, monkeypatch
):
    repository = InMemoryJobRepository()
    add_queued_job(repository, make_config(snapshot))
    clock = ControlledClock()
    runner, lifecycle = make_timed_runner(repository, registry, clock)
    terminal_jobs = []

    def make_terminal():
        if terminal == "canceled":
            terminal_jobs.append(lifecycle.cancel(JOB_ID, NOW))
        elif terminal == "failed":
            terminal_jobs.append(
                lifecycle.fail(
                    JOB_ID,
                    WORKER_ID,
                    JobFailure("internal_error", "Внешний сбой выполнения"),
                    NOW,
                )
            )
        else:
            terminal_jobs.append(
                lifecycle.recover_expired(JOB_ID, LEASE + timedelta(microseconds=1))
            )

    execute = tool.execute
    expired = DEADLINE + timedelta(seconds=1)

    def execute_terminal_and_advance(*args):
        if not terminal_jobs:
            make_terminal()
            clock.now = expired
        return execute(*args)

    monkeypatch.setattr(tool, "execute", execute_terminal_and_advance)

    returned = run(runner, snapshot)

    terminal_job = terminal_jobs[0]
    assert len(terminal_jobs) == 1
    assert returned is terminal_job
    assert repository.get(JOB_ID) is terminal_job
    assert returned.history == terminal_job.history
    assert returned.result is None
    assert returned.finished_at == terminal_job.finished_at
    assert len(tool.calls) == 1
    if terminal == "canceled":
        assert returned.error is None
    elif terminal == "failed":
        assert returned.error.code == "internal_error"
    else:
        assert returned.error.code == "worker_lost"
