from decimal import Decimal

import pytest

from tradelab.domain.backtest import BacktestEngine
from tradelab.domain.dispatcher import Dispatcher
from tradelab.domain.job import JobState
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import BuyAndHoldTool
from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Signal,
    SignalReason,
    ToolError,
    ToolResult,
)


class CallbackTool:
    title = "Тестовая стратегия"
    description = "Управляемая реализация общего контракта StrategyTool"

    def __init__(self, name="recording_hold", version="1.0.0", error=None):
        self.name = name
        self.version = version
        self.error = error
        self.calls = []

    def params_schema(self):
        return {"type": "object"}

    def validate_params(self, params):
        return NormalizedParams(dict(params))

    def required_bars(self, normalized_params):
        return 1

    def execute(self, closed_bars, position, normalized_params):
        self.calls.append((closed_bars, position, normalized_params))
        if self.error is not None:
            raise self.error
        return ToolResult(
            Signal.HOLD,
            closed_bars[-1].close_time,
            SignalReason("integration_hold", "Тестовая стратегия сохраняет позицию"),
            {},
        )


def _expected_case(contract_cases, strategy):
    return next(
        case for case in contract_cases["backtests"] if case["strategy"] == strategy
    )


def _run_builtin_and_assert_contract(core, contract_cases, strategy):
    expected = _expected_case(contract_cases, strategy)
    queued = core.create_job(strategy=strategy, params=expected["params"])

    assert queued.state is JobState.QUEUED
    assert queued.created_at == core.clock()
    assert [entry.to_state for entry in queued.history] == [
        JobState.CREATED,
        JobState.QUEUED,
    ]

    completed = core.run_job(queued.id)

    assert core.repository.get(queued.id) is completed
    assert completed.state is JobState.COMPLETED
    assert completed.error is None
    assert completed.started_at == completed.finished_at == core.clock()
    assert [entry.to_state for entry in completed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.COMPLETED,
    ]
    assert [entry.actor for entry in completed.history] == [
        "user",
        "user",
        "worker",
        "worker",
    ]

    config = completed.config
    result = completed.result
    assert config is not None
    assert result is not None
    assert completed.snapshot_id == contract_cases["snapshot_id"]
    assert result.snapshot_id == contract_cases["snapshot_id"]
    assert config.strategy.name == expected["strategy"]
    assert config.strategy.version == expected["strategy_version"]
    assert dict(config.strategy.normalized_params) == expected["params"]
    assert result.strategy.name == expected["strategy"]
    assert result.strategy.version == expected["strategy_version"]
    assert dict(result.strategy.normalized_params) == expected["params"]
    assert result.engine_version == contract_cases["engine_version"]
    assert config.engine_version == result.engine_version
    assert config.baseline.name == "buy_and_hold"
    assert config.baseline.version == "1.0.0"
    assert dict(config.baseline.normalized_params) == {}
    assert config.initial_capital == Decimal(contract_cases["initial_capital"])
    assert config.commission_rate == Decimal(contract_cases["commission_rate"])

    assert result.metrics.final_equity == Decimal(expected["expected_final_equity"])
    assert result.equity_curve[-1].equity == Decimal(
        expected["expected_final_equity"]
    )
    assert result.metrics.trades_count == expected["expected_trades_count"]
    assert len(result.trades) == expected["expected_trades_count"]
    assert [trade.side.value for trade in result.trades] == [
        execution["side"] for execution in expected["expected_executions"]
    ]
    assert result.metrics.buy_and_hold_return == Decimal(
        expected["expected_buy_and_hold_return"]
    )
    return completed


def test_c1_sma_created_to_completed_with_contract_result(core, contract_cases):
    completed = _run_builtin_and_assert_contract(core, contract_cases, "sma_crossover")

    assert completed.result.metrics.final_equity == Decimal("518.68")
    assert completed.result.metrics.trades_count == 2


def test_c1_buy_and_hold_uses_same_generic_flow(core, contract_cases):
    completed = _run_builtin_and_assert_contract(core, contract_cases, "buy_and_hold")

    assert completed.result.metrics.final_equity == Decimal("729.10")
    assert completed.result.metrics.trades_count == 1


def test_c5_unknown_operation_does_not_create_job(core):
    with pytest.raises(ToolError) as raised:
        core.create_job(strategy="not_registered", params={})

    assert raised.value.code == "unknown_operation"
    assert raised.value.details["field"] == "strategy"
    assert raised.value.details["reason"] == "not_found"
    assert core.ids.calls == 0
    with pytest.raises(KeyError):
        core.repository.get(core.ids.next_id)


def test_c5_invalid_params_identifies_field_and_does_not_create_job(core):
    with pytest.raises(ToolError) as raised:
        core.create_job(
            strategy="sma_crossover",
            params={"fast": 0, "slow": 3},
        )

    assert raised.value.code == "invalid_params"
    assert raised.value.details["field"] == "params.fast"
    assert raised.value.details["reason"] == "out_of_range"
    assert core.ids.calls == 0
    with pytest.raises(KeyError):
        core.repository.get(core.ids.next_id)


def test_c3_unexpected_tool_exception_fails_job_without_result(core_factory):
    registry = Registry()
    tool = CallbackTool("exploding_tool", error=RuntimeError("boom"))
    registry.register(tool)
    registry.register(BuyAndHoldTool())
    core = core_factory(registry=registry)
    queued = core.create_job(strategy=tool.name, params={})

    failed = core.run_job(queued.id)

    assert failed.state is JobState.FAILED
    assert failed.error.code == "tool_failed"
    assert failed.error.message == "Вызов StrategyTool завершился ошибкой"
    assert failed.result is None
    assert len(tool.calls) == 1
    assert [entry.to_state for entry in failed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.FAILED,
    ]


def test_c3_new_tool_runs_after_single_registration(core_factory):
    registry = Registry()
    registry.register(BuyAndHoldTool())
    core = core_factory(registry=registry)
    tool = CallbackTool("new_integration_tool")
    core.registry.register(tool)
    queued = core.create_job(strategy=tool.name, params={"window": 3})

    completed = core.run_job(queued.id)

    assert completed.state is JobState.COMPLETED
    assert completed.result.strategy.name == tool.name
    assert completed.result.strategy.version == tool.version
    assert dict(completed.result.strategy.normalized_params) == {"window": 3}
    assert completed.result.metrics.trades_count == 0
    assert len(tool.calls) == len(core.snapshot.bars)
    for index, (closed_bars, _, params) in enumerate(tool.calls):
        assert closed_bars == core.snapshot.bars[: index + 1]
        assert dict(params) == {"window": 3}


def test_c3_baseline_failure_after_primary_success_has_no_partial_result(core_factory):
    registry = Registry()
    primary = CallbackTool("successful_primary")
    baseline = CallbackTool("buy_and_hold", error=RuntimeError("baseline boom"))
    registry.register(primary)
    registry.register(baseline)
    core = core_factory(registry=registry)
    queued = core.create_job(strategy=primary.name, params={})

    failed = core.run_job(queued.id)

    assert failed.state is JobState.FAILED
    assert failed.error.code == "tool_failed"
    assert failed.result is None
    assert len(primary.calls) == len(core.snapshot.bars)
    assert len(baseline.calls) == 1
    assert [entry.to_state for entry in failed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.RUNNING,
        JobState.FAILED,
    ]
    assert sum(entry.to_state is JobState.FAILED for entry in failed.history) == 1


def test_c4_unavailable_saved_version_fails_from_queued(core_factory):
    initial_registry = Registry()
    old_tool = CallbackTool("versioned_tool", version="1.0.0")
    initial_registry.register(old_tool)
    initial_registry.register(BuyAndHoldTool())
    core = core_factory(registry=initial_registry)
    queued = core.create_job(strategy=old_tool.name, params={})

    replacement_registry = Registry()
    current_tool = CallbackTool("versioned_tool", version="2.0.0")
    replacement_registry.register(current_tool)
    replacement_registry.register(BuyAndHoldTool())
    replacement_engine = BacktestEngine(Dispatcher(replacement_registry))
    restarted_runner = core.new_runner(
        registry=replacement_registry,
        engine=replacement_engine,
    )

    failed = core.run_job(queued.id, runner=restarted_runner)

    assert failed.state is JobState.FAILED
    assert failed.error.code == "strategy_unavailable"
    assert "1.0.0" in failed.error.message
    assert failed.result is None
    assert failed.snapshot_id is None
    assert failed.worker_id is None
    assert old_tool.calls == []
    assert current_tool.calls == []
    assert [entry.to_state for entry in failed.history] == [
        JobState.CREATED,
        JobState.QUEUED,
        JobState.FAILED,
    ]
