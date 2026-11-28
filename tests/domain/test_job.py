from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier
from types import SimpleNamespace

import pytest

from tradelab.domain.backtest_types import (
    BacktestMetrics,
    BacktestPeriod,
    BacktestResult,
    EquityPoint,
    StrategySpec,
    Trade,
)
from tradelab.domain.job import (
    InvalidTransition,
    Job,
    JobFailure,
    JobState,
)
from tradelab.domain.job_lifecycle import JobLifecycle
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
)

NOW = datetime(2026, 11, 21, 10, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=30)
DEADLINE = NOW + timedelta(minutes=5)
START_FAILURE = JobFailure("strategy_unavailable", "Сохранённая версия недоступна")
RUN_FAILURE = JobFailure("tool_failed", "Стратегия завершилась ошибкой")
CAPITAL = Decimal("1000.00")
RESULT = BacktestResult(
    snapshot_id="snapshot-1",
    strategy=StrategySpec("example", "1.0.0", NormalizedParams({})),
    period=BacktestPeriod(date(2026, 11, 19), date(2026, 11, 20)),
    engine_version="1.0.0",
    metrics=BacktestMetrics(
        total_return=Decimal(0),
        buy_and_hold_return=Decimal(0),
        max_drawdown=Decimal(0),
        sharpe_ratio=None,
        final_equity=CAPITAL,
        trades_count=1,
        closed_positions_count=0,
        win_rate=None,
        avg_position_return=None,
    ),
    final_position=Position.LONG,
    final_cash=Decimal(900),
    final_quantity=10,
    equity_curve=(EquityPoint(date(2026, 11, 20), CAPITAL),),
    trades=(
        Trade(
            side=Signal.BUY,
            signal_time=NOW - timedelta(days=2),
            execution_time=NOW - timedelta(days=1),
            price=Decimal(10),
            quantity=10,
            commission=Decimal(0),
            cash_after=Decimal(900),
            reason=SignalReason("enter", "Вход в позицию"),
            pnl=None,
        ),
    ),
    signals=(),
)


@pytest.mark.parametrize(
    ("worker_id", "lease_expires_at", "deadline_at"),
    [
        pytest.param("", LEASE, DEADLINE, id="missing-worker"),
        pytest.param("worker-1", NOW, DEADLINE, id="expired-lease"),
        pytest.param("worker-1", LEASE, NOW, id="expired-deadline"),
    ],
)
def test_job_start_with_invalid_claim_keeps_job_queued(worker_id, lease_expires_at, deadline_at):
    queued = Job("job-1", NOW).enqueue(NOW)

    with pytest.raises(ValueError, match="worker_id"):
        queued.start(worker_id, NOW, lease_expires_at=lease_expires_at, deadline_at=deadline_at)

    assert queued.state is JobState.QUEUED
    assert queued.worker_id is None
    assert queued.started_at is None
    assert tuple(entry.to_state for entry in queued.history) == (JobState.CREATED, JobState.QUEUED)


def test_job_start_with_valid_claim_records_worker_and_deadlines():
    queued = Job("job-1", NOW).enqueue(NOW)

    running = queued.start("worker-1", NOW, lease_expires_at=LEASE, deadline_at=DEADLINE)

    assert running.state is JobState.RUNNING
    assert (running.worker_id, running.started_at) == ("worker-1", NOW)
    assert (running.lease_expires_at, running.deadline_at) == (LEASE, DEADLINE)
    assert running.history[:-1] == queued.history
    assert running.history[-1].from_state is JobState.QUEUED
    assert running.history[-1].actor == "worker"
    assert queued.state is JobState.QUEUED


@pytest.fixture
def lifecycle():
    repository = InMemoryJobRepository()
    job = Job("job-1", NOW, snapshot_id="snapshot-1")
    repository.add(job)
    return JobLifecycle(repository), repository, job.id


def start(service, job_id, worker_id="worker-1"):
    return service.start(
        job_id, worker_id, NOW, lease_expires_at=LEASE, deadline_at=DEADLINE
    )


def move(service, job_id, state):
    if state is JobState.QUEUED:
        return service.enqueue(job_id, NOW)
    if state is JobState.RUNNING:
        return start(service, job_id)
    if state is JobState.COMPLETED:
        return service.complete(job_id, "worker-1", RESULT, NOW)
    if state is JobState.FAILED:
        job = service.repository.get(job_id)
        if job.state is JobState.QUEUED:
            return service.fail_to_start(job_id, START_FAILURE, NOW)
        return service.fail(job_id, "worker-1", RUN_FAILURE, NOW)
    return service.cancel(job_id, NOW)


ALLOWED = {
    JobState.CREATED: {JobState.QUEUED, JobState.CANCELED},
    JobState.QUEUED: {JobState.RUNNING, JobState.FAILED, JobState.CANCELED},
    JobState.RUNNING: {JobState.COMPLETED, JobState.FAILED, JobState.CANCELED},
    JobState.COMPLETED: set(),
    JobState.FAILED: set(),
    JobState.CANCELED: set(),
}


@pytest.mark.parametrize("from_state", JobState)
@pytest.mark.parametrize("to_state", JobState)
def test_job_itself_enforces_the_state_table(from_state, to_state):
    job = Job("job-1", NOW, snapshot_id="snapshot-1")
    if from_state in (
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.COMPLETED,
        JobState.FAILED,
    ):
        job = job.enqueue(NOW)
    if from_state in (JobState.RUNNING, JobState.COMPLETED, JobState.FAILED):
        job = job.start("worker-1", NOW, lease_expires_at=LEASE, deadline_at=DEADLINE)
    if from_state is JobState.COMPLETED:
        job = job.complete("worker-1", RESULT, NOW)
    if from_state is JobState.FAILED:
        job = job.fail("worker-1", RUN_FAILURE, NOW)
    if from_state is JobState.CANCELED:
        job = job.cancel(NOW)

    if to_state in ALLOWED[from_state]:
        job.require_transition(to_state)
    else:
        with pytest.raises(InvalidTransition) as raised:
            job.require_transition(to_state)
        assert raised.value.code == "invalid_transition"
        assert raised.value.details == {
            "job_id": job.id,
            "from_state": from_state.value,
            "to_state": to_state.value,
        }


STATE_PATHS = {
    JobState.CREATED: (),
    JobState.QUEUED: (JobState.QUEUED,),
    JobState.RUNNING: (JobState.QUEUED, JobState.RUNNING),
    JobState.COMPLETED: (JobState.QUEUED, JobState.RUNNING, JobState.COMPLETED),
    JobState.FAILED: (JobState.QUEUED, JobState.RUNNING, JobState.FAILED),
    JobState.CANCELED: (JobState.QUEUED, JobState.RUNNING, JobState.CANCELED),
}


def reach_state(service, job_id, state):
    for step in STATE_PATHS[state]:
        move(service, job_id, step)


LIFECYCLE_OPERATIONS = (
    ("enqueue", JobState.QUEUED, {JobState.CREATED}, (NOW,)),
    ("start", JobState.RUNNING, {JobState.QUEUED}, ("worker-1", NOW)),
    ("complete", JobState.COMPLETED, {JobState.RUNNING}, ("worker-1", RESULT, NOW)),
    ("fail_to_start", JobState.FAILED, {JobState.QUEUED}, (START_FAILURE, NOW)),
    ("fail", JobState.FAILED, {JobState.RUNNING}, ("worker-1", RUN_FAILURE, NOW)),
    (
        "cancel",
        JobState.CANCELED,
        {JobState.CREATED, JobState.QUEUED, JobState.RUNNING},
        (NOW,),
    ),
)


@pytest.mark.parametrize(
    ("from_state", "to_state", "operation", "arguments"),
    [
        pytest.param(state, target, name, args, id=f"{state}-{name}")
        for name, target, allowed_states, args in LIFECYCLE_OPERATIONS
        for state in JobState
        if state not in allowed_states
    ],
)
def test_forbidden_lifecycle_operations_leave_the_saved_job_unchanged(
    lifecycle, from_state, to_state, operation, arguments
):
    service, repository, job_id = lifecycle
    reach_state(service, job_id, from_state)
    params = RESULT.strategy.normalized_params
    before = deepcopy(repository.get(job_id), {id(params): NormalizedParams(params)})
    assert before.state is from_state
    kwargs = (
        {"lease_expires_at": LEASE, "deadline_at": DEADLINE}
        if operation == "start"
        else {}
    )

    with pytest.raises(InvalidTransition) as raised:
        getattr(service, operation)(job_id, *arguments, **kwargs)

    assert raised.value.code == "invalid_transition"
    assert raised.value.details == {
        "job_id": job_id,
        "from_state": from_state.value,
        "to_state": to_state.value,
    }
    assert repository.get(job_id) == before


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (JobState.QUEUED, "tool_failed"),
        (JobState.QUEUED, "job_timeout"),
        (JobState.QUEUED, "unknown_failure"),
        (JobState.QUEUED, ""),
        (JobState.QUEUED, " \t "),
        (JobState.RUNNING, "strategy_unavailable"),
        (JobState.RUNNING, "worker_lost"),
        (JobState.RUNNING, "unknown_failure"),
        (JobState.RUNNING, ""),
        (JobState.RUNNING, " \t "),
    ],
)
def test_failure_code_of_another_stage_or_unknown_code_is_rejected(
    lifecycle, state, code
):
    service, repository, job_id = lifecycle
    reach_state(service, job_id, state)
    before = deepcopy(repository.get(job_id))
    failure = JobFailure(code, "Описание ошибки")

    with pytest.raises(ValueError, match="Код ошибки"):
        if state is JobState.QUEUED:
            service.fail_to_start(job_id, failure, NOW)
        else:
            service.fail(job_id, "worker-1", failure, NOW)

    assert repository.get(job_id) == before


def test_worker_can_fail_job_when_dispatcher_detects_a_timeout(lifecycle):
    service, repository, job_id = lifecycle
    reach_state(service, job_id, JobState.RUNNING)
    failure = JobFailure("job_timeout", "Истёк срок выполнения задания")

    job = service.fail(job_id, "worker-1", failure, DEADLINE + timedelta(seconds=1))

    assert repository.get(job_id) == job
    assert job.error == failure
    assert job.history[-1].actor == "worker"
    assert job.history[-1].reason == failure.code


@pytest.mark.parametrize(
    ("path", "actor"),
    [
        ((JobState.CANCELED,), "user"),
        ((JobState.QUEUED, JobState.CANCELED), "user"),
        ((JobState.QUEUED, JobState.FAILED), "worker"),
        ((JobState.QUEUED, JobState.RUNNING, JobState.CANCELED), "user"),
        ((JobState.QUEUED, JobState.RUNNING, JobState.FAILED), "worker"),
        ((JobState.QUEUED, JobState.RUNNING, JobState.COMPLETED), "worker"),
    ],
)
def test_allowed_transitions_store_history_and_payload_together(lifecycle, path, actor):
    service, repository, job_id = lifecycle
    for state in path:
        job = move(service, job_id, state)
        assert repository.get(job_id) == job
    assert [entry.to_state for entry in job.history] == [JobState.CREATED, *path]
    assert job.history[-1].actor == actor
    assert job.history[-1].from_state is job.history[-2].to_state
    assert job.history[-1].at == NOW
    assert (job.result == RESULT) == (job.state is JobState.COMPLETED)
    if job.state is JobState.FAILED:
        expected = START_FAILURE if len(path) == 2 else RUN_FAILURE
        assert job.error == expected
        assert job.history[-1].reason == expected.code
    else:
        assert job.error is None


def test_z1_two_workers_cannot_claim_one_job(lifecycle):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    barrier = Barrier(2)

    def claim(worker_id):
        barrier.wait(timeout=5)
        try:
            return start(service, job_id, worker_id)
        except InvalidTransition as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(claim, ("worker-1", "worker-2")))
    (winner,) = [outcome for outcome in outcomes if isinstance(outcome, Job)]
    (rejected,) = [
        outcome for outcome in outcomes if isinstance(outcome, InvalidTransition)
    ]
    assert rejected.details["from_state"] == "running"
    assert rejected.details["to_state"] == "running"
    assert repository.get(job_id) == winner
    assert len(winner.history) == 3


@pytest.mark.parametrize(
    ("now", "lease", "code"),
    [
        (LEASE + timedelta(microseconds=1), LEASE, "worker_lost"),
        (DEADLINE + timedelta(seconds=1), LEASE, "worker_lost"),
        (DEADLINE + timedelta(seconds=1), DEADLINE, "job_timeout"),
        (
            DEADLINE + timedelta(microseconds=1),
            DEADLINE + timedelta(minutes=1),
            "job_timeout",
        ),
    ],
)
def test_z2_recovery_of_expired_jobs_uses_supplied_time(lifecycle, now, lease, code):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    service.start(job_id, "worker-1", NOW, lease_expires_at=lease, deadline_at=DEADLINE)
    assert service.recover_expired(job_id, min(lease, DEADLINE)) is None

    recovered = JobLifecycle(repository).recover_expired(job_id, now)

    assert recovered.state is JobState.FAILED
    assert recovered.error.code == code
    assert recovered.history[-1].actor == "reaper"
    assert recovered.history[-1].at == now
    assert repository.get(job_id) == recovered
    with pytest.raises(InvalidTransition):
        service.complete(job_id, "worker-1", RESULT, now)
    assert repository.get(job_id) == recovered


def test_recovery_rechecks_lease_at_write_time(lifecycle, monkeypatch):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    start(service, job_id)
    compare_and_set = repository.compare_and_set
    now = LEASE + timedelta(seconds=1)

    def renew_before_write(*args, **kwargs):
        monkeypatch.setattr(repository, "compare_and_set", compare_and_set)
        assert service.renew_lease(job_id, "worker-1", now + timedelta(seconds=30))
        return compare_and_set(*args, **kwargs)

    monkeypatch.setattr(repository, "compare_and_set", renew_before_write)

    assert service.recover_expired(job_id, now) is None
    assert repository.get(job_id).state is JobState.RUNNING
    assert repository.get(job_id).lease_expires_at > now


def test_cancel_retries_if_worker_claimed_after_initial_read(lifecycle, monkeypatch):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    compare_and_set = repository.compare_and_set

    def start_before_write(*args, **kwargs):
        monkeypatch.setattr(repository, "compare_and_set", compare_and_set)
        start(service, job_id)
        return compare_and_set(*args, **kwargs)

    monkeypatch.setattr(repository, "compare_and_set", start_before_write)

    canceled = service.cancel(job_id, NOW)
    assert canceled.history[-1].from_state is JobState.RUNNING
    assert repository.get(job_id) == canceled


def test_fail_to_start_does_not_fail_a_job_claimed_before_write(lifecycle, monkeypatch):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    compare_and_set = repository.compare_and_set
    running = None

    def start_before_write(*args, **kwargs):
        nonlocal running
        monkeypatch.setattr(repository, "compare_and_set", compare_and_set)
        running = deepcopy(start(service, job_id))
        return compare_and_set(*args, **kwargs)

    monkeypatch.setattr(repository, "compare_and_set", start_before_write)

    with pytest.raises(InvalidTransition) as raised:
        service.fail_to_start(job_id, START_FAILURE, NOW)

    assert raised.value.code == "invalid_transition"
    assert raised.value.details == {
        "job_id": job_id,
        "from_state": "running",
        "to_state": "failed",
    }
    assert running is not None
    assert running.worker_id == "worker-1"
    assert repository.get(job_id) == running


def test_z3_late_result_and_error_leave_canceled_job_unchanged(lifecycle):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    start(service, job_id)
    canceled = service.cancel(job_id, NOW)

    with pytest.raises(InvalidTransition):
        service.complete(job_id, "worker-1", RESULT, NOW)
    with pytest.raises(InvalidTransition):
        service.fail(job_id, "worker-1", RUN_FAILURE, NOW)
    with pytest.raises(InvalidTransition):
        service.cancel(job_id, NOW)
    assert service.renew_lease(job_id, "worker-1", DEADLINE) is None
    assert service.record_snapshot(job_id, "worker-1", "other") is None
    assert repository.get(job_id) == canceled
    assert canceled.result is None


def test_z4_failure_cannot_be_replaced_by_late_success(lifecycle):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    start(service, job_id)
    failed = service.fail(job_id, "worker-1", RUN_FAILURE, NOW)

    with pytest.raises(InvalidTransition):
        service.complete(job_id, "worker-1", RESULT, NOW)
    assert repository.get(job_id) == failed


def test_z5_success_without_a_worker_does_not_store_a_result(lifecycle):
    service, repository, job_id = lifecycle
    for state in (JobState.CREATED, JobState.QUEUED):
        before = repository.get(job_id)
        assert before.state is state
        with pytest.raises(InvalidTransition):
            service.complete(job_id, "worker-1", RESULT, NOW)
        assert repository.get(job_id) == before
        if state is JobState.CREATED:
            service.enqueue(job_id, NOW)


def test_z6_completed_job_cannot_be_started_again(lifecycle):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    start(service, job_id)
    completed = service.complete(job_id, "worker-1", RESULT, NOW)

    with pytest.raises(InvalidTransition):
        start(service, job_id)
    with pytest.raises(InvalidTransition):
        service.fail(job_id, "worker-1", RUN_FAILURE, NOW)
    with pytest.raises(InvalidTransition):
        service.cancel(job_id, NOW)
    assert repository.get(job_id) == completed


def test_wrong_worker_cannot_write_any_running_job_data(lifecycle):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    running = start(service, job_id)

    with pytest.raises(InvalidTransition):
        service.complete(job_id, "worker-2", RESULT, NOW)
    with pytest.raises(InvalidTransition):
        service.fail(job_id, "worker-2", RUN_FAILURE, NOW)
    assert service.renew_lease(job_id, "worker-2", DEADLINE) is None
    assert service.record_snapshot(job_id, "worker-2", "other") is None
    assert repository.get(job_id) == running


def test_failed_completion_without_snapshot_does_not_partially_write(lifecycle):
    service, repository, _ = lifecycle
    job_id = "without-snapshot"
    repository.add(Job(job_id, NOW))
    service.enqueue(job_id, NOW)
    running = start(service, job_id)

    with pytest.raises(ValueError, match="снимок"):
        service.complete(job_id, "worker-1", RESULT, NOW)
    assert repository.get(job_id) == running

    snapshot_recorded = service.record_snapshot(job_id, "worker-1", "snapshot-2")
    with pytest.raises(ValueError, match="снимок"):
        service.record_snapshot(job_id, "worker-1", "other")
    assert repository.get(job_id) == snapshot_recorded

    result = replace(RESULT, snapshot_id="snapshot-2")
    completed = service.complete(job_id, "worker-1", result, NOW)
    assert completed.snapshot_id == "snapshot-2"
    assert completed.result == result


def test_result_for_another_snapshot_cannot_complete_a_job(lifecycle):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    before = deepcopy(start(service, job_id))
    result = replace(RESULT, snapshot_id="other-snapshot")

    with pytest.raises(ValueError, match="снимок"):
        service.complete(job_id, "worker-1", result, NOW)

    assert repository.get(job_id) == before


@pytest.mark.parametrize(
    "result", [None, SimpleNamespace(snapshot_id="snapshot-1")]
)
def test_complete_requires_canonical_result_without_partial_write(lifecycle, result):
    service, repository, job_id = lifecycle
    service.enqueue(job_id, NOW)
    before = deepcopy(start(service, job_id))

    with pytest.raises(ValueError, match="[Рр]езультат"):
        service.complete(job_id, "worker-1", result, NOW)

    current = repository.get(job_id)
    assert current == before
    assert current.history == before.history
    assert current.result is None
    assert current.state is JobState.RUNNING
