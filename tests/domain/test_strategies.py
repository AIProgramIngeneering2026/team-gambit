import json
from dataclasses import replace
from decimal import ROUND_UP, Decimal, Inexact, Rounded, localcontext
from pathlib import Path

import pytest

from tradelab.domain.registry import Registry
from tradelab.domain.strategies import (
    BuyAndHoldTool,
    SmaCrossoverTool,
    register_builtin_strategies,
)
from tradelab.domain.strategies.sma_crossover import _format_indicator
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


@pytest.mark.parametrize(
    "case_index", [0, 1], ids=["sma_crossover", "buy_and_hold"]
)
def test_i32_3_contract_control_signals(contract_bars, contract_cases, case_index):
    case = contract_cases["backtests"][case_index]
    registry = Registry()
    register_builtin_strategies(registry)
    tool = registry.get(case["strategy"], case["strategy_version"])
    params = tool.validate_params(case["params"])
    assert tool.required_bars(params) == case["required_bars"]

    position = Position.FLAT
    expected_results = zip(
        case["expected_signals"],
        case["expected_reason_codes"],
        contract_bars,
        strict=True,
    )
    for index, (expected_signal, expected_reason, bar) in enumerate(expected_results):
        result = tool.execute(contract_bars[: index + 1], position, params)
        assert result.signal.value == expected_signal
        assert result.reason.code == expected_reason
        assert result.reason.message
        assert result.bar_time == bar.close_time
        if expected_signal == "BUY":
            position = Position.LONG
        elif expected_signal == "SELL":
            position = Position.FLAT


def test_i32_5_sma_large_prices_format_all_indicators(contract_bars):
    price = Decimal("999999999999999999.9999999999")
    bars = tuple(
        replace(bar, open=price, high=price, low=price, close=price)
        for bar in contract_bars[:4]
    )
    tool = SmaCrossoverTool()
    params = tool.validate_params({"fast": 2, "slow": 3})

    result = tool.execute(bars, Position.FLAT, params)

    assert result.signal is Signal.HOLD
    assert result.reason.code == "no_cross"
    assert result.bar_time == bars[-1].close_time
    assert result.indicators == {
        "fast_previous": "1000000000000000000",
        "slow_previous": "1000000000000000000",
        "fast_current": "1000000000000000000",
        "slow_current": "1000000000000000000",
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("123.45678901234"), "123.4567890123"),
        (Decimal("0.000000000049"), "0"),
        (Decimal("0.000000000051"), "0.0000000001"),
        (Decimal("1.00000000005"), "1"),
        (Decimal("1.00000000015"), "1.0000000002"),
        (
            Decimal("999999999999999999.99999999996"),
            "1000000000000000000",
        ),
    ],
    ids=[
        "ordinary",
        "tiny_below_half",
        "tiny_above_half",
        "half_even_down",
        "half_even_up",
        "carry_into_integer",
    ],
)
def test_i32_5_indicator_formatting_uses_decimal_rounding_rules(value, expected):
    assert _format_indicator(value) == expected


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


@pytest.mark.parametrize(
    ("tool", "raw_params"),
    [
        pytest.param(SmaCrossoverTool(), {"fast": 2, "slow": 3}, id="sma"),
        pytest.param(BuyAndHoldTool(), {}, id="buy_and_hold"),
    ],
)
@pytest.mark.parametrize("position", [Position.FLAT, Position.LONG])
def test_i32_5_repeated_execution_is_deterministic_and_preserves_input(
    contract_bars, tool, raw_params, position
):
    params = tool.validate_params(raw_params)
    bars = list(contract_bars[:5])
    original = tuple(bars)

    first = tool.execute(bars, position, params)
    second = tool.execute(original, position, params)
    window = original[-tool.required_bars(params) :]

    assert first == second == tool.execute(bars, position, params)
    assert first == tool.execute(window, position, params)
    assert tuple(bars) == original


def test_i32_5_sma_is_independent_of_callers_decimal_context(contract_bars):
    tool = SmaCrossoverTool()
    params = tool.validate_params({"fast": 2, "slow": 3})
    bars = contract_bars[:5]
    expected = tool.execute(bars, Position.FLAT, params)
    assert expected.signal is Signal.BUY

    with localcontext() as caller:
        caller.prec = 6
        caller.rounding = ROUND_UP
        caller.traps[Inexact] = True
        caller.traps[Rounded] = True

        assert tool.execute(bars, Position.FLAT, params) == expected
        assert caller.prec == 6
        assert caller.rounding == ROUND_UP
        assert caller.traps[Inexact] and caller.traps[Rounded]
