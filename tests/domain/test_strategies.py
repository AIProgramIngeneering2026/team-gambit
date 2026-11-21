import json
from pathlib import Path

import pytest

from tradelab.domain.registry import Registry
from tradelab.domain.strategies import (
    BuyAndHoldTool,
    SmaCrossoverTool,
    register_builtin_strategies,
)
from tradelab.domain.strategy_tool import Position, Signal, ToolError


def test_i32_1_builtin_tools_register_and_expose_schemas(contract_cases):
    registry = Registry()
    register_builtin_strategies(registry)
    tools = {tool.name: tool for tool in registry.catalog()}

    assert set(tools) == {"buy_and_hold", "sma_crossover"}
    for name, tool in tools.items():
        assert registry.get(name, "1.0.0") is tool
        assert tool.version == "1.0.0"
        assert tool.title
        assert tool.description
        schema_path = (
            Path(__file__).parents[2]
            / "docs"
            / "schemas"
            / f"{name.replace('_', '-')}-params.schema.json"
        )
        assert tool.params_schema() == json.loads(
            schema_path.read_text(encoding="utf-8")
        )
    assert contract_cases["backtests"]


def test_i32_2_defaults_and_required_bars():
    sma = SmaCrossoverTool()
    baseline = BuyAndHoldTool()

    assert dict(sma.validate_params({})) == {"fast": 20, "slow": 50}
    assert sma.required_bars(sma.validate_params({})) == 51
    assert sma.required_bars(sma.validate_params({"fast": 2, "slow": 3})) == 4
    assert dict(baseline.validate_params({})) == {}
    assert baseline.required_bars(baseline.validate_params({})) == 1


def test_i32_2_contract_invalid_params(contract_cases):
    registry = Registry()
    register_builtin_strategies(registry)

    for case in contract_cases["invalid_params"]:
        tool = registry.get(case["strategy"], "1.0.0")
        with pytest.raises(ToolError) as raised:
            tool.validate_params(case["params"])
        assert raised.value.code == "invalid_params"
        assert raised.value.details["field"] == case["field"]
        assert raised.value.details["reason"] == case["reason"]


@pytest.mark.parametrize("params", [None, [], "fast=2", 1, True])
@pytest.mark.parametrize("tool", [SmaCrossoverTool(), BuyAndHoldTool()])
def test_i32_2_params_must_be_object(tool, params):
    with pytest.raises(ToolError) as raised:
        tool.validate_params(params)
    assert raised.value.code == "invalid_params"
    assert raised.value.details == {"field": "params", "reason": "wrong_type"}


@pytest.mark.parametrize(
    ("params", "field", "reason"),
    [
        ({"fast": -1}, "params.fast", "out_of_range"),
        ({"fast": 500, "slow": 500}, "params.fast", "out_of_range"),
        ({"slow": 1}, "params.slow", "out_of_range"),
        ({"slow": 501}, "params.slow", "out_of_range"),
        ({"slow": "50"}, "params.slow", "wrong_type"),
        ({"slow": True}, "params.slow", "wrong_type"),
        ({"slow": 50.5}, "params.slow", "wrong_type"),
        ({"fast": 2, "slow": 2}, "params.fast", "must_be_less_than_slow"),
        ({"slow": 2}, "params.fast", "must_be_less_than_slow"),
        ({"fast": 51, "slow": 50}, "params.fast", "must_be_less_than_slow"),
    ],
)
def test_i32_2_additional_sma_validation(params, field, reason):
    with pytest.raises(ToolError) as raised:
        SmaCrossoverTool().validate_params(params)
    assert raised.value.code == "invalid_params"
    assert raised.value.details["field"] == field
    assert raised.value.details["reason"] == reason


def test_i32_2_buy_and_hold_rejects_any_field():
    with pytest.raises(ToolError) as raised:
        BuyAndHoldTool().validate_params({"extra": 1})
    assert raised.value.details == {
        "field": "params.extra",
        "reason": "unknown_field",
    }


def test_i32_3_sma_control_signals(contract_bars, contract_cases):
    case = contract_cases["backtests"][0]
    tool = SmaCrossoverTool()
    params = tool.validate_params(case["params"])
    positions = (
        Position.FLAT,
        Position.FLAT,
        Position.FLAT,
        Position.FLAT,
        Position.FLAT,
        Position.LONG,
        Position.FLAT,
    )

    for index, position in enumerate(positions):
        result = tool.execute(contract_bars[: index + 1], position, params)
        assert result.signal.value == case["expected_signals"][index]
        assert result.reason.code == case["expected_reason_codes"][index]
        assert result.reason.message
        assert result.bar_time == contract_bars[index].close_time


def test_i32_3_sma_cross_up_indicators_match_contract_example(contract_bars):
    tool = SmaCrossoverTool()
    params = tool.validate_params({"fast": 2, "slow": 3})

    result = tool.execute(contract_bars[:5], Position.FLAT, params)

    assert result.signal is Signal.BUY
    assert result.reason.code == "cross_up"
    assert result.reason.message == "Быстрая SMA пересекла медленную снизу вверх"
    assert result.indicators == {
        "fast_previous": "10",
        "slow_previous": "10",
        "fast_current": "13",
        "slow_current": "11.3333333333",
    }


def test_i32_3_contract_additional_tool_calls(contract_bars, contract_cases):
    registry = Registry()
    register_builtin_strategies(registry)

    for case in contract_cases["tool_calls"]:
        tool = registry.get(case["strategy"], "1.0.0")
        params = tool.validate_params(case["params"])
        result = tool.execute(
            contract_bars[: case["bar_count"]], Position(case["position"]), params
        )
        assert result.signal.value == case["expected_signal"]
        assert result.reason.code == case["expected_reason_code"]


def test_i32_4_short_history_is_not_hold_and_has_null_indicators(contract_bars):
    tool = SmaCrossoverTool()
    params = tool.validate_params({"fast": 2, "slow": 3})

    empty = tool.execute((), Position.FLAT, params)
    short = tool.execute(contract_bars[:3], Position.LONG, params)

    for result, bar_time, actual in (
        (empty, None, 0),
        (short, contract_bars[2].close_time, 3),
    ):
        assert result.signal is Signal.INSUFFICIENT_DATA
        assert result.reason.code == "insufficient_history"
        assert "4" in result.reason.message
        assert str(actual) in result.reason.message
        assert result.bar_time == bar_time
        assert result.indicators == {
            "fast_previous": None,
            "slow_previous": None,
            "fast_current": None,
            "slow_current": None,
        }


def test_i32_3_buy_and_hold_uses_actual_position(contract_bars):
    tool = BuyAndHoldTool()
    params = tool.validate_params({})

    entered = tool.execute(contract_bars[:1], Position.FLAT, params)
    kept = tool.execute(contract_bars[:2], Position.LONG, params)
    empty = tool.execute((), Position.FLAT, params)

    assert (entered.signal, entered.reason.code) == (Signal.BUY, "enter_baseline")
    assert (kept.signal, kept.reason.code) == (Signal.HOLD, "keep_baseline")
    assert empty.signal is Signal.INSUFFICIENT_DATA
    assert empty.reason.code == "insufficient_history"
    assert empty.bar_time is None
    assert "1" in empty.reason.message and "0" in empty.reason.message
    assert entered.indicators == kept.indicators == empty.indicators == {}


def test_i32_5_repeated_execution_is_deterministic_and_preserves_input(contract_bars):
    tool = SmaCrossoverTool()
    params = tool.validate_params({"fast": 2, "slow": 3})
    bars = list(contract_bars[:5])
    original = tuple(bars)

    first = tool.execute(bars, Position.FLAT, params)
    second = tool.execute(contract_bars[:5], Position.FLAT, params)

    assert first == second == tool.execute(bars, Position.FLAT, params)
    assert tuple(bars) == original
