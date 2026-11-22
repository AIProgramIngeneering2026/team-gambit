from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from threading import Barrier

import pytest

from tradelab.domain.job import (
    BacktestMetrics,
    BacktestResult,
    EquityPoint,
    InvalidTransition,
    Job,
    JobFailure,
    JobState,
    Trade,
)
from tradelab.domain.job_lifecycle import JobLifecycle
from tradelab.domain.job_repository import InMemoryJobRepository

NOW = datetime(2026, 11, 21, 10, tzinfo=UTC)
LEASE = NOW + timedelta(seconds=30)
DEADLINE = NOW + timedelta(minutes=5)
START_FAILURE = JobFailure("strategy_unavailable", "Сохранённая версия недоступна")
RUN_FAILURE = JobFailure("tool_failed", "Стратегия завершилась ошибкой")
CAPITAL = Decimal("1000.00")
RESULT = BacktestResult(
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
    equity_curve=(EquityPoint(date(2026, 11, 20), CAPITAL),),
    trades=(
        Trade(
            side="BUY",
            signal_date=date(2026, 11, 19),
            execution_date=date(2026, 11, 20),
            price=Decimal(10),
            quantity=10,
            commission=Decimal(0),
            reason_code="enter",
            reason_message="Вход в позицию",
            pnl=None,
        ),
    ),
)


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

    completed = service.complete(job_id, "worker-1", RESULT, NOW)
    assert completed.snapshot_id == "snapshot-2"
    assert completed.result == RESULT
