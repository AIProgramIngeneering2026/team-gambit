from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, TypeAlias, get_args


class JobState(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


_TRANSITIONS = {
    JobState.CREATED: frozenset({JobState.QUEUED, JobState.CANCELED}),
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.FAILED, JobState.CANCELED}),
    JobState.RUNNING: frozenset(
        {JobState.COMPLETED, JobState.FAILED, JobState.CANCELED}
    ),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELED: frozenset(),
}

Actor = Literal["user", "worker", "reaper"]
StartFailureCode: TypeAlias = Literal[
    "strategy_unavailable", "engine_unavailable", "snapshot_not_found"
]
WorkerFailureCode: TypeAlias = Literal[
    "market_data_unavailable",
    "invalid_market_data",
    "insufficient_data",
    "tool_failed",
    "internal_error",
    "job_timeout",
]
RecoveryFailureCode: TypeAlias = Literal["worker_lost", "job_timeout"]
JobFailureCode: TypeAlias = StartFailureCode | WorkerFailureCode | RecoveryFailureCode

_START_FAILURE_CODES = get_args(StartFailureCode)
_WORKER_FAILURE_CODES = get_args(WorkerFailureCode)
_RECOVERY_FAILURE_CODES = get_args(RecoveryFailureCode)


class InvalidTransition(Exception):
    code = "invalid_transition"

    def __init__(self, job_id: str, from_state: JobState, to_state: JobState):
        self.details = {
            "job_id": job_id,
            "from_state": from_state.value,
            "to_state": to_state.value,
        }
        self.message = (
            f"Недопустимый переход задания {job_id}: {from_state} → {to_state}"
        )
        super().__init__(self.message)


@dataclass(frozen=True, slots=True)
class StateTransition:
    from_state: JobState | None
    to_state: JobState
    at: datetime
    actor: Actor
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class JobFailure:
    code: JobFailureCode
    message: str


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return: Decimal
    buy_and_hold_return: Decimal
    max_drawdown: Decimal
    sharpe_ratio: Decimal | None
    final_equity: Decimal
    trades_count: int
    closed_positions_count: int
    win_rate: Decimal | None
    avg_position_return: Decimal | None


@dataclass(frozen=True, slots=True)
class EquityPoint:
    date: date
    equity: Decimal


@dataclass(frozen=True, slots=True)
class Trade:
    side: Literal["BUY", "SELL"]
    signal_date: date
    execution_date: date
    price: Decimal
    quantity: int
    commission: Decimal
    reason_code: str
    reason_message: str
    pnl: Decimal | None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    metrics: BacktestMetrics
    equity_curve: tuple[EquityPoint, ...]
    trades: tuple[Trade, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity_curve", tuple(self.equity_curve))
        object.__setattr__(self, "trades", tuple(self.trades))


@dataclass(frozen=True, slots=True)
class Job:
    id: str
    created_at: datetime
    snapshot_id: str | None = None
    state: JobState = JobState.CREATED
    history: tuple[StateTransition, ...] = ()
    worker_id: str | None = None
    lease_expires_at: datetime | None = None
    deadline_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: BacktestResult | None = None
    error: JobFailure | None = None

    def __post_init__(self) -> None:
        history = tuple(self.history)
        if not history:
            history = (
                StateTransition(None, JobState.CREATED, self.created_at, "user"),
            )
        if history[-1].to_state is not self.state:
            raise ValueError(
                "Состояние задания должно совпадать с последней записью истории"
            )
        if (self.state is JobState.COMPLETED) != (self.result is not None):
            raise ValueError("Результат допустим и обязателен только для completed")
        if (self.state is JobState.FAILED) != (self.error is not None):
            raise ValueError("Ошибка допустима и обязательна только для failed")
        object.__setattr__(self, "history", history)

    def require_transition(self, to_state: JobState) -> None:
        if to_state not in _TRANSITIONS[self.state]:
            raise InvalidTransition(self.id, self.state, to_state)

    def _transition(
        self,
        to_state: JobState,
        now: datetime,
        actor: Actor,
        *,
        error: JobFailure | None = None,
        **changes,
    ) -> "Job":
        self.require_transition(to_state)
        entry = StateTransition(
            self.state, to_state, now, actor, error.code if error else None
        )
        return replace(
            self, state=to_state, history=(*self.history, entry), error=error, **changes
        )

    def enqueue(self, now: datetime) -> "Job":
        return self._transition(JobState.QUEUED, now, "user")

    def start(
        self,
        worker_id: str,
        now: datetime,
        *,
        lease_expires_at: datetime,
        deadline_at: datetime,
    ) -> "Job":
        self.require_transition(JobState.RUNNING)
        if not worker_id or min(lease_expires_at, deadline_at) <= now:
            raise ValueError(
                "Для захвата нужны worker_id, действующая аренда и срок выполнения"
            )
        return self._transition(
            JobState.RUNNING,
            now,
            "worker",
            worker_id=worker_id,
            lease_expires_at=lease_expires_at,
            deadline_at=deadline_at,
            started_at=now,
        )

    def _require_worker(self, worker_id: str, to_state: JobState) -> None:
        if self.state is not JobState.RUNNING or self.worker_id != worker_id:
            raise InvalidTransition(self.id, self.state, to_state)

    def complete(self, worker_id: str, result: BacktestResult, now: datetime) -> "Job":
        self._require_worker(worker_id, JobState.COMPLETED)
        if self.snapshot_id is None:
            raise ValueError(
                "Перед завершением задания нужно зафиксировать снимок данных"
            )
        if result is None:
            raise ValueError("Для завершения задания нужен результат бэктеста")
        return self._transition(
            JobState.COMPLETED, now, "worker", finished_at=now, result=result
        )

    def fail_to_start(self, error: JobFailure, now: datetime) -> "Job":
        if self.state is not JobState.QUEUED:
            raise InvalidTransition(self.id, self.state, JobState.FAILED)
        return self._fail(error, now, "worker", _START_FAILURE_CODES)

    def fail(self, worker_id: str, error: JobFailure, now: datetime) -> "Job":
        self._require_worker(worker_id, JobState.FAILED)
        return self._fail(error, now, "worker", _WORKER_FAILURE_CODES)

    def _fail(
        self,
        error: JobFailure,
        now: datetime,
        actor: Actor,
        allowed_codes: tuple[JobFailureCode, ...],
    ) -> "Job":
        if error is None or error.code not in allowed_codes:
            raise ValueError("Код ошибки недопустим для этого этапа задания")
        if not error.message.strip():
            raise ValueError("Для ошибки задания нужно описание")
        return self._transition(
            JobState.FAILED, now, actor, error=error, finished_at=now
        )

    def cancel(self, now: datetime) -> "Job":
        return self._transition(JobState.CANCELED, now, "user", finished_at=now)

    def renew_lease(self, worker_id: str, lease_expires_at: datetime) -> "Job":
        self._require_worker(worker_id, JobState.RUNNING)
        if (
            self.lease_expires_at is not None
            and lease_expires_at <= self.lease_expires_at
        ):
            return self
        return replace(self, lease_expires_at=lease_expires_at)

    def record_snapshot(self, worker_id: str, snapshot_id: str) -> "Job":
        self._require_worker(worker_id, JobState.RUNNING)
        if not snapshot_id:
            raise ValueError("Нужен идентификатор снимка")
        if self.snapshot_id is not None and self.snapshot_id != snapshot_id:
            raise ValueError("Зафиксированный снимок задания нельзя заменить")
        return replace(self, snapshot_id=snapshot_id)

    def recover_expired(self, now: datetime) -> "Job | None":
        if self.state is not JobState.RUNNING:
            return None
        lease = self.lease_expires_at
        deadline = self.deadline_at
        # Если истекли оба срока, причина определяется более ранним из них.
        if (
            deadline is not None
            and deadline < now
            and (lease is None or deadline <= lease)
        ):
            return self._fail(
                JobFailure("job_timeout", "Истёк срок выполнения задания"),
                now,
                "reaper",
                _RECOVERY_FAILURE_CODES,
            )
        if lease is not None and lease < now:
            return self._fail(
                JobFailure("worker_lost", "Истекла аренда задания"),
                now,
                "reaper",
                _RECOVERY_FAILURE_CODES,
            )
        return None
