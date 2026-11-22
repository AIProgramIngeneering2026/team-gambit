from collections.abc import Callable
from datetime import UTC, datetime

from .backtest import BacktestEngine, MarketSnapshot
from .backtest_types import BacktestResult
from .job import InvalidTransition, Job, JobFailure
from .job_lifecycle import JobLifecycle
from .job_repository import JobRepository
from .registry import Registry
from .strategy_tool import ToolError


_TIMEOUT_FAILURE = JobFailure("job_timeout", "Истёк срок выполнения задания")


class _DeadlineExceeded(Exception):
    def __init__(self, at: datetime) -> None:
        super().__init__()
        self.at = at


def _utc_now() -> datetime:
    return datetime.now(UTC)


class JobRunner:
    def __init__(
        self,
        *,
        lifecycle: JobLifecycle,
        registry: Registry,
        engine: BacktestEngine,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._lifecycle = lifecycle
        self._repository: JobRepository = lifecycle.repository
        self._registry = registry
        self._engine = engine
        self._clock = clock

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

        if job.snapshot_id is not None and (
            snapshot is None or snapshot.snapshot_id != job.snapshot_id
        ):
            return self._lifecycle.fail_to_start(
                job_id,
                JobFailure(
                    "snapshot_not_found",
                    f"Сохранённый снимок {job.snapshot_id} недоступен",
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
        if snapshot is None:
            return self._fail_late_aware(
                job_id,
                worker_id,
                JobFailure(
                    "market_data_unavailable",
                    "Поставщик данных не вернул снимок для нового задания",
                ),
                now,
            )
        if not snapshot.snapshot_id.strip():
            return self._fail_late_aware(
                job_id,
                worker_id,
                JobFailure(
                    "invalid_market_data",
                    "Снимок данных имеет пустой идентификатор",
                ),
                now,
            )
        if config.ticker != snapshot.instrument.ticker:
            return self._fail_late_aware(
                job_id,
                worker_id,
                JobFailure(
                    "invalid_market_data",
                    f"Тикер снимка {snapshot.instrument.ticker} не совпадает "
                    f"с тикером задания {config.ticker}",
                ),
                now,
            )

        recorded = self._lifecycle.record_snapshot(
            job_id, worker_id, snapshot.snapshot_id
        )
        if recorded is None:
            return self._repository.get(job_id)

        def check_deadline() -> None:
            checked_at = self._clock()
            if checked_at > deadline_at:
                raise _DeadlineExceeded(checked_at)

        result = None
        failure = None
        try:
            result = self._engine.run(
                snapshot=snapshot,
                period=config.period,
                strategy=config.strategy,
                baseline=config.baseline,
                initial_capital=config.initial_capital,
                commission_rate=config.commission_rate,
                before_tool_call=check_deadline,
            )
        except _DeadlineExceeded as error:
            return self._fail_late_aware(
                job_id, worker_id, _TIMEOUT_FAILURE, error.at
            )
        except ToolError as error:
            failure = self._tool_failure(error)
        except Exception:
            failure = JobFailure(
                "internal_error", "Неожиданная ошибка выполнения задания"
            )

        finished_at = self._clock()
        if finished_at > deadline_at:
            failure = _TIMEOUT_FAILURE
        if failure is not None:
            return self._fail_late_aware(job_id, worker_id, failure, finished_at)
        if not isinstance(result, BacktestResult) or result.snapshot_id != snapshot.snapshot_id:
            return self._fail_late_aware(
                job_id,
                worker_id,
                JobFailure("internal_error", "Движок вернул некорректный результат бэктеста"),
                finished_at,
            )

        try:
            return self._lifecycle.complete(job_id, worker_id, result, finished_at)
        except InvalidTransition:
            return self._repository.get(job_id)

    @staticmethod
    def _tool_failure(error: ToolError) -> JobFailure:
        code = error.code.strip() if isinstance(error.code, str) else ""
        message = error.message if isinstance(error.message, str) else ""
        if code in {
            "market_data_unavailable",
            "invalid_market_data",
            "insufficient_data",
            "tool_failed",
            "internal_error",
        } and message.strip():
            return JobFailure(code, message)

        label = code or "<пустой>"
        if not message.strip():
            return JobFailure(
                "tool_failed",
                f"StrategyTool вернул ошибку ({label}) без описания",
            )
        return JobFailure(
            "tool_failed",
            f"StrategyTool вернул недопустимый код ошибки {label}: {message}",
        )

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
