import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext
from pathlib import Path

import pytest

from tradelab.domain.backtest_types import BacktestPeriod
from tradelab.domain.instrument_catalog import InMemoryInstrumentCatalog
from tradelab.domain.job import Job, JobState
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.job_service import JobService
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import register_builtin_strategies
from tradelab.domain.strategy_tool import ToolError

NOW = datetime(2026, 11, 21, 10, tzinfo=UTC)
JOB_ID = "job-1"
PERIOD = BacktestPeriod(date(2023, 1, 1), date(2024, 1, 1))


@pytest.fixture
def service_parts():
    registry = Registry()
    register_builtin_strategies(registry)
    repository = InMemoryJobRepository()
    service = JobService(
        repository=repository,
        registry=registry,
        instruments=InMemoryInstrumentCatalog({"EXMPL"}),
        clock=lambda: NOW,
        id_factory=lambda: JOB_ID,
    )
    return service, repository


def create(service, **changes):
    request = {
        "ticker": "EXMPL",
        "strategy": "sma_crossover",
        "params": {"fast": 20, "slow": 50},
        "period": PERIOD,
    }
    request.update(changes)
    return service.create_backtest(**request)


def assert_rejected_without_job(
    service_parts, code, field, reason, **changes
):
    service, repository = service_parts
    with pytest.raises(ToolError) as raised:
        create(service, **changes)
    assert raised.value.code == code
    assert raised.value.details["field"] == field
    assert raised.value.details["reason"] == reason
    with pytest.raises(KeyError):
        repository.get(JOB_ID)


def test_c5_unknown_strategy_lists_catalog_and_does_not_create_job(service_parts):
    service, repository = service_parts
    with pytest.raises(ToolError) as raised:
        create(service, strategy="rsi_magic")
    assert raised.value.code == "unknown_operation"
    assert raised.value.details == {
        "field": "strategy",
        "reason": "not_found",
        "allowed": ("buy_and_hold", "sma_crossover"),
    }
    with pytest.raises(KeyError):
        repository.get(JOB_ID)


def test_c5_unknown_ticker_does_not_create_job(service_parts):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "ticker",
        "unknown_ticker",
        ticker="NOPE",
    )


def test_c5_fast_zero_has_field_range_reason_and_no_job(service_parts):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "params.fast",
        "out_of_range",
        params={"fast": 0, "slow": 50},
    )


@pytest.mark.parametrize("fast", [50, 51])
def test_c5_fast_must_be_less_than_slow_without_creating_job(service_parts, fast):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "params.fast",
        "must_be_less_than_slow",
        params={"fast": fast, "slow": 50},
    )


def test_c5_buy_and_hold_rejects_extra_params_without_creating_job(service_parts):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "params.extra",
        "unknown_field",
        strategy="buy_and_hold",
        params={"extra": True},
    )


@pytest.mark.parametrize(
    "period",
    [
        BacktestPeriod(date(2024, 1, 2), date(2024, 1, 1)),
        BacktestPeriod(date(2024, 1, 1), date(2024, 1, 1)),
    ],
)
def test_c5_start_must_be_before_end_without_creating_job(service_parts, period):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "period.start",
        "must_be_before_end",
        period=period,
    )


def test_c1_1_period_with_3653_included_days_is_accepted(service_parts):
    service, repository = service_parts
    period = BacktestPeriod(
        date(2016, 1, 1), date(2016, 1, 1) + timedelta(days=3652)
    )

    job = create(service, period=period)

    assert job.state is JobState.QUEUED
    assert job.config is not None
    assert job.config.period == period
    assert repository.get(JOB_ID) is job


def test_c5_period_with_3654_included_days_does_not_create_job(service_parts):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "period",
        "too_long",
        period=BacktestPeriod(
            date(2016, 1, 1), date(2016, 1, 1) + timedelta(days=3653)
        ),
    )


def test_c5_future_period_does_not_create_job(service_parts):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "period.end",
        "in_future",
        period=BacktestPeriod(date(2026, 11, 20), date(2026, 11, 22)),
    )


@pytest.mark.parametrize("capital", ["999.99", "1000000000.01"])
def test_c5_capital_out_of_range_does_not_create_job(service_parts, capital):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "initial_capital",
        "out_of_range",
        initial_capital=capital,
    )


@pytest.mark.parametrize("commission", ["-0.0001", "0.0501"])
def test_c5_commission_out_of_range_does_not_create_job(service_parts, commission):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        "commission_rate",
        "out_of_range",
        commission_rate=commission,
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("initial_capital", 100000, "wrong_type"),
        ("initial_capital", "1e5", "invalid_format"),
        ("commission_rate", 0.0005, "wrong_type"),
        ("commission_rate", "NaN", "invalid_format"),
    ],
)
def test_c5_decimal_fields_require_plain_decimal_strings(
    service_parts, field, value, reason
):
    assert_rejected_without_job(
        service_parts,
        "invalid_params",
        field,
        reason,
        **{field: value},
    )


def test_c1_1_valid_job_stores_normalized_config_and_exact_versions(service_parts):
    service, repository = service_parts
    job = create(
        service,
        ticker="exmpl",
        params={"fast": 10},
        initial_capital="100000.009",
        commission_rate="0.0005",
    )

    assert job.id == JOB_ID
    assert job.created_at == NOW
    assert job.state is JobState.QUEUED
    assert [entry.to_state for entry in job.history] == [
        JobState.CREATED,
        JobState.QUEUED,
    ]
    assert repository.get(JOB_ID) is job

    config = job.config
    assert config is not None
    assert config.ticker == "EXMPL"
    assert config.period == PERIOD
    assert config.strategy.name == "sma_crossover"
    assert config.strategy.version == "1.0.0"
    assert dict(config.strategy.normalized_params) == {"fast": 10, "slow": 50}
    assert config.baseline.name == "buy_and_hold"
    assert config.baseline.version == "1.0.0"
    assert dict(config.baseline.normalized_params) == {}
    assert config.initial_capital == Decimal("100000.01")
    assert config.commission_rate == Decimal("0.0005")
    assert config.engine_version == "1.0.0"
    with pytest.raises(FrozenInstanceError):
        config.ticker = "OTHER"


def test_job_config_remains_optional_for_existing_job_fixtures():
    assert Job(JOB_ID, NOW).config is None


def test_c1_1_capital_quantization_isolates_caller_decimal_context(service_parts):
    service, repository = service_parts
    with localcontext() as context:
        context.prec = 5
        job = create(service, initial_capital="100000.009")

        assert job.state is JobState.QUEUED
        assert job.config is not None
        assert job.config.initial_capital == Decimal("100000.01")
        assert context.prec == 5
    assert repository.get(JOB_ID) is job


def test_c1_1_expected_capital_rounding_ignores_caller_decimal_limits(service_parts):
    service, repository = service_parts
    with localcontext() as context:
        context.Emax = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True

        job = create(service, initial_capital="100000.009")

        assert job.state is JobState.QUEUED
        assert job.config is not None
        assert job.config.initial_capital == Decimal("100000.01")
        assert context.Emax == 3
        assert context.traps[Inexact]
        assert context.traps[Rounded]
    assert repository.get(JOB_ID) is job


def test_c1_1_import_and_moscow_midnight_do_not_require_iana_tzdata(tmp_path):
    script = """
from datetime import UTC, date, datetime

from tradelab.domain.backtest_types import BacktestPeriod
from tradelab.domain.instrument_catalog import InMemoryInstrumentCatalog
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.job_service import JobService
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import register_builtin_strategies

registry = Registry()
register_builtin_strategies(registry)
service = JobService(
    repository=InMemoryJobRepository(),
    registry=registry,
    instruments=InMemoryInstrumentCatalog({"EXMPL"}),
    clock=lambda: datetime(2026, 11, 21, 21, tzinfo=UTC),
    id_factory=lambda: "job-1",
)
job = service.create_backtest(
    ticker="EXMPL",
    strategy="sma_crossover",
    params={},
    period=BacktestPeriod(date(2026, 11, 21), date(2026, 11, 22)),
)
assert job.state == "queued"
print(job.state)
"""
    environment = os.environ.copy()
    environment["PYTHONTZPATH"] = str(tmp_path / "missing-iana-tzdata")
    environment["PYTHONPATH"] = str(Path(__file__).parents[2] / "src")

    completed = subprocess.run(
        [sys.executable, "-S", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout.strip() == "queued"
