from datetime import datetime

from .backtest import BacktestEngine, MarketSnapshot
from .job import InvalidTransition, Job, JobFailure
from .job_lifecycle import JobLifecycle
from .job_repository import JobRepository
from .registry import Registry
from .strategy_tool import ToolError


class JobRunner:
    def __init__(
        self,
        *,
        lifecycle: JobLifecycle,
        registry: Registry,
        engine: BacktestEngine,
    ) -> None:
        self._lifecycle = lifecycle
        self._repository: JobRepository = lifecycle.repository
        self._registry = registry
        self._engine = engine

    def run(
        self,
        job_id: str,
        snapshot: MarketSnapshot | None,
        worker_id: str,
        now: datetime,
        *,
        lease_expires_at: datetime,
        deadline_at: datetime,
    ) -> Job:
        job = self._repository.get(job_id)
        config = job.config
        if config is None:
            raise ValueError("Для выполнения задания нужна конфигурация бэктеста")

        try:
            self._registry.get(config.strategy.name, config.strategy.version)
            self._registry.get(config.baseline.name, config.baseline.version)
        except ToolError as error:
            return self._lifecycle.fail_to_start(
                job_id,
                JobFailure("strategy_unavailable", error.message),
                now,
            )

        if config.engine_version != self._engine.version:
            return self._lifecycle.fail_to_start(
                job_id,
                JobFailure(
                    "engine_unavailable",
                    f"Сохранённая версия {config.engine_version} движка недоступна",
                ),
                now,
            )

        if snapshot is None or (
            job.snapshot_id is not None
            and snapshot.snapshot_id != job.snapshot_id
        ):
            expected = job.snapshot_id or "неизвестный"
            return self._lifecycle.fail_to_start(
                job_id,
                JobFailure(
                    "snapshot_not_found",
                    f"Сохранённый снимок {expected} недоступен",
                ),
                now,
            )

        self._lifecycle.start(
            job_id,
            worker_id,
            now,
            lease_expires_at=lease_expires_at,
            deadline_at=deadline_at,
        )
        recorded = self._lifecycle.record_snapshot(
            job_id, worker_id, snapshot.snapshot_id
        )
        if recorded is None:
            return self._repository.get(job_id)

        try:
            result = self._engine.run(
                snapshot=snapshot,
                period=config.period,
                strategy=config.strategy,
                baseline=config.baseline,
                initial_capital=config.initial_capital,
                commission_rate=config.commission_rate,
            )
        except ToolError as error:
            return self._fail_late_aware(
                job_id, worker_id, JobFailure(error.code, error.message), now
            )
        except Exception:
            return self._fail_late_aware(
                job_id,
                worker_id,
                JobFailure("internal_error", "Неожиданная ошибка выполнения задания"),
                now,
            )

        try:
            return self._lifecycle.complete(job_id, worker_id, result, now)
        except InvalidTransition:
            return self._repository.get(job_id)

    def _fail_late_aware(
        self,
        job_id: str,
        worker_id: str,
        error: JobFailure,
        now: datetime,
    ) -> Job:
        try:
            return self._lifecycle.fail(job_id, worker_id, error, now)
        except InvalidTransition:
            return self._repository.get(job_id)
