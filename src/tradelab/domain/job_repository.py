from collections.abc import Callable
from threading import Lock
from typing import Protocol

from .job import Job, JobState

JobChange = Callable[[Job], Job | None]


class JobRepository(Protocol):
    def add(self, job: Job) -> None: ...

    def get(self, job_id: str) -> Job:
        """Возвращает неизменяемый снимок задания. Неизвестный id даёт KeyError."""
        ...

    def compare_and_set(
        self,
        job_id: str,
        expected_state: JobState,
        change: JobChange,
        *,
        worker_id: str | None = None,
    ) -> Job | None:
        """Применяет change к текущей версии под блокировкой или в транзакции.

        None означает несовпадение состояния/владельца или отказ change от записи.
        """
        ...


class InMemoryJobRepository:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = Lock()

    def add(self, job: Job) -> None:
        with self._lock:
            if job.id in self._jobs:
                raise ValueError(f"Задание уже существует: {job.id}")
            self._jobs[job.id] = job

    def get(self, job_id: str) -> Job:
        with self._lock:
            return self._jobs[job_id]

    def compare_and_set(
        self,
        job_id: str,
        expected_state: JobState,
        change: JobChange,
        *,
        worker_id: str | None = None,
    ) -> Job | None:
        with self._lock:
            job = self._jobs[job_id]
            if job.state is not expected_state or (
                worker_id is not None and job.worker_id != worker_id
            ):
                return None
            updated = change(job)
            if updated is not None:
                self._jobs[job_id] = updated
            return updated
