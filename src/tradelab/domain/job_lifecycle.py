from collections.abc import Callable
from datetime import datetime

from .backtest_types import BacktestResult
from .job import InvalidTransition, Job, JobFailure, JobState
from .job_repository import JobRepository


class JobLifecycle:
    def __init__(self, repository: JobRepository) -> None:
        self.repository = repository

    def _move(
        self,
        job_id: str,
        target: JobState,
        change: Callable[[Job], Job],
        *,
        worker_id: str | None = None,
    ) -> Job:
        while True:
            job = self.repository.get(job_id)
            job.require_transition(target)
            if worker_id is not None and job.worker_id != worker_id:
                raise InvalidTransition(job_id, job.state, target)
            updated = self.repository.compare_and_set(
                job_id, job.state, change, worker_id=worker_id
            )
            if updated is not None:
                return updated
            # При гонке, например между отменой и захватом, проверяем новое состояние.

    def create_and_enqueue(self, job: Job, now: datetime) -> Job:
        queued = job.enqueue(now)
        self.repository.add(queued)
        return queued

    def enqueue(self, job_id: str, now: datetime) -> Job:
        return self._move(job_id, JobState.QUEUED, lambda job: job.enqueue(now))

    def start(
        self,
        job_id: str,
        worker_id: str,
        now: datetime,
        *,
        lease_expires_at: datetime,
        deadline_at: datetime,
    ) -> Job:
        return self._move(
            job_id,
            JobState.RUNNING,
            lambda job: job.start(
                worker_id,
                now,
                lease_expires_at=lease_expires_at,
                deadline_at=deadline_at,
            ),
        )

    def complete(
        self, job_id: str, worker_id: str, result: BacktestResult, now: datetime
    ) -> Job:
        return self._move(
            job_id,
            JobState.COMPLETED,
            lambda job: job.complete(worker_id, result, now),
            worker_id=worker_id,
        )

    def fail_to_start(self, job_id: str, error: JobFailure, now: datetime) -> Job:
        return self._move(
            job_id, JobState.FAILED, lambda job: job.fail_to_start(error, now)
        )

    def fail(
        self, job_id: str, worker_id: str, error: JobFailure, now: datetime
    ) -> Job:
        return self._move(
            job_id,
            JobState.FAILED,
            lambda job: job.fail(worker_id, error, now),
            worker_id=worker_id,
        )

    def cancel(self, job_id: str, now: datetime) -> Job:
        return self._move(job_id, JobState.CANCELED, lambda job: job.cancel(now))

    def renew_lease(
        self, job_id: str, worker_id: str, lease_expires_at: datetime
    ) -> Job | None:
        return self.repository.compare_and_set(
            job_id,
            JobState.RUNNING,
            lambda job: job.renew_lease(worker_id, lease_expires_at),
            worker_id=worker_id,
        )

    def record_snapshot(
        self, job_id: str, worker_id: str, snapshot_id: str
    ) -> Job | None:
        return self.repository.compare_and_set(
            job_id,
            JobState.RUNNING,
            lambda job: job.record_snapshot(worker_id, snapshot_id),
            worker_id=worker_id,
        )

    def recover_expired(self, job_id: str, now: datetime) -> Job | None:
        job = self.repository.get(job_id)
        if job.state is not JobState.RUNNING:
            return None
        return self.repository.compare_and_set(
            job_id,
            JobState.RUNNING,
            lambda current: current.recover_expired(now),
            worker_id=job.worker_id,
        )
