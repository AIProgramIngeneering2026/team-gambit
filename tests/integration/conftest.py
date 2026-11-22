from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

import pytest

from tradelab.domain.backtest import (
    BacktestEngine,
    BacktestPeriod,
    InstrumentMetadata,
    MarketSnapshot,
)
from tradelab.domain.dispatcher import Dispatcher
from tradelab.domain.instrument_catalog import InMemoryInstrumentCatalog
from tradelab.domain.job import Job
from tradelab.domain.job_lifecycle import JobLifecycle
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.job_runner import JobRunner
from tradelab.domain.job_service import JobService
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import register_builtin_strategies

NOW = datetime(2026, 11, 21, 10, tzinfo=UTC)
LEASE_DURATION = timedelta(seconds=30)
JOB_TIMEOUT = timedelta(minutes=5)


class ManualClock:
    def __init__(self, current: datetime = NOW) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> datetime:
        self.current += delta
        return self.current


class SequentialIds:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return f"job-{self.calls:04d}"

    @property
    def next_id(self) -> str:
        return f"job-{self.calls + 1:04d}"


@dataclass(slots=True)
class CoreHarness:
    repository: InMemoryJobRepository
    registry: Registry
    dispatcher: Dispatcher
    engine: BacktestEngine
    lifecycle: JobLifecycle
    runner: JobRunner
    service: JobService
    snapshot: MarketSnapshot
    period: BacktestPeriod
    clock: ManualClock
    ids: SequentialIds
    initial_capital: str
    commission_rate: str

    def create_job(self, *, strategy: str, params: object) -> Job:
        return self.service.create_backtest(
            ticker=self.snapshot.instrument.ticker,
            strategy=strategy,
            params=params,
            period=self.period,
            initial_capital=self.initial_capital,
            commission_rate=self.commission_rate,
        )

    def new_runner(
        self,
        *,
        lifecycle: JobLifecycle | None = None,
        registry: Registry | None = None,
        engine: BacktestEngine | None = None,
    ) -> JobRunner:
        return JobRunner(
            lifecycle=lifecycle or self.lifecycle,
            registry=registry or self.registry,
            engine=engine or self.engine,
        )

    def run_job(
        self,
        job_id: str,
        *,
        worker_id: str = "worker-1",
        now: datetime | None = None,
        lease_expires_at: datetime | None = None,
        deadline_at: datetime | None = None,
        runner: JobRunner | None = None,
    ) -> Job:
        started_at = self.clock() if now is None else now
        selected_runner = self.runner if runner is None else runner
        return selected_runner.run(
            job_id,
            self.snapshot,
            worker_id,
            started_at,
            lease_expires_at=(
                started_at + LEASE_DURATION
                if lease_expires_at is None
                else lease_expires_at
            ),
            deadline_at=(
                started_at + JOB_TIMEOUT if deadline_at is None else deadline_at
            ),
        )


@pytest.fixture
def contract_snapshot(contract_cases, contract_bars):
    instrument = contract_cases["instrument"]
    return MarketSnapshot(
        snapshot_id=contract_cases["snapshot_id"],
        instrument=InstrumentMetadata(
            ticker=instrument["ticker"],
            currency=instrument["currency"],
            lot_size=instrument["lot_size"],
        ),
        bars=contract_bars,
    )


@pytest.fixture
def one_bar_snapshot(contract_snapshot):
    return replace(contract_snapshot, bars=contract_snapshot.bars[:1])


@pytest.fixture
def contract_period(contract_cases):
    return BacktestPeriod(
        start=date.fromisoformat(contract_cases["period"]["start"]),
        end=date.fromisoformat(contract_cases["period"]["end"]),
    )


@pytest.fixture
def core_factory(contract_cases, contract_snapshot, contract_period):
    def build(
        *,
        registry: Registry | None = None,
        repository: InMemoryJobRepository | None = None,
        snapshot: MarketSnapshot | None = None,
        clock: ManualClock | None = None,
        ids: SequentialIds | None = None,
    ) -> CoreHarness:
        selected_registry = Registry() if registry is None else registry
        if registry is None:
            register_builtin_strategies(selected_registry)
        selected_repository = (
            InMemoryJobRepository() if repository is None else repository
        )
        selected_snapshot = contract_snapshot if snapshot is None else snapshot
        selected_clock = ManualClock() if clock is None else clock
        selected_ids = SequentialIds() if ids is None else ids
        dispatcher = Dispatcher(selected_registry)
        engine = BacktestEngine(dispatcher)
        lifecycle = JobLifecycle(selected_repository)
        runner = JobRunner(
            lifecycle=lifecycle,
            registry=selected_registry,
            engine=engine,
        )
        service = JobService(
            repository=selected_repository,
            registry=selected_registry,
            instruments=InMemoryInstrumentCatalog(
                {selected_snapshot.instrument.ticker}
            ),
            clock=selected_clock,
            id_factory=selected_ids,
        )
        return CoreHarness(
            repository=selected_repository,
            registry=selected_registry,
            dispatcher=dispatcher,
            engine=engine,
            lifecycle=lifecycle,
            runner=runner,
            service=service,
            snapshot=selected_snapshot,
            period=contract_period,
            clock=selected_clock,
            ids=selected_ids,
            initial_capital=contract_cases["initial_capital"],
            commission_rate=contract_cases["commission_rate"],
        )

    return build


@pytest.fixture
def core(core_factory):
    return core_factory()
