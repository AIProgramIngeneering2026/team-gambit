from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from tradelab.domain.registry import Registry
from tradelab.domain.strategy_tool import (
    Candle,
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
    StrategyTool,
    ToolError,
    ToolResult,
)


class ExampleTool:
    name = "example"
    title = "Пример"

    def __init__(self, version):
        self.version = version

    @property
    def description(self):
        return f"Стратегия {self.version}"

    def params_schema(self):
        return {
            "type": "object",
            "properties": {"window": {"type": "integer", "default": 1}},
        }

    def validate_params(self, params):
        value = params.get("window", 1)
        if type(value) is not int:
            raise ToolError(
                "invalid_params",
                "Окно должно быть целым числом",
                "params.window",
                "wrong_type",
            )
        return NormalizedParams({"window": value})

    def required_bars(self, normalized_params):
        return normalized_params["window"]

    def execute(self, closed_bars, position, normalized_params):
        return ToolResult(
            signal=Signal.HOLD,
            bar_time=closed_bars[-1].close_time,
            reason=SignalReason("hold", "Позиция сохранена"),
            indicators={"test": "1"},
        )


def test_registry_registers_and_resolves_an_arbitrary_tool():
    registry = Registry()
    tool: StrategyTool = ExampleTool("1.0.0")
    registry.register(tool)

    found = registry.get(tool.name)
    assert found is tool
    params = found.validate_params({})
    assert found.required_bars(params) == 1
    bar = Candle(
        trading_date=date(2026, 11, 20),
        open_time=datetime(2026, 11, 20, 7, tzinfo=UTC),
        close_time=datetime(2026, 11, 20, 18, tzinfo=UTC),
        open=Decimal(10),
        high=Decimal(11),
        low=Decimal(9),
        close=Decimal(10),
        volume=Decimal(30),
    )
    result = found.execute((bar,), Position.FLAT, params)
    assert result.signal is Signal.HOLD
    assert result.bar_time == bar.close_time
    with pytest.raises(FrozenInstanceError):
        bar.close = Decimal(20)
    with pytest.raises(FrozenInstanceError):
        result.signal = Signal.BUY
    with pytest.raises(TypeError):
        result.indicators["test"] = "2"


def test_unknown_operation_lists_available_strategies():
    registry = Registry()
    registry.register(ExampleTool("1.0.0"))

    with pytest.raises(ToolError) as raised:
        registry.get("unknown")
    assert raised.value.code == "unknown_operation"
    assert raised.value.details == {
        "field": "strategy",
        "reason": "unknown_operation",
        "allowed": ("example",),
    }


def test_registry_resolves_saved_versions_without_switching_to_current():
    registry = Registry()
    first = ExampleTool("1.0.0")
    second = ExampleTool("2.0.0")
    registry.register(first)
    registry.register(second, current=True)
    registry.register(ExampleTool("3.0.0"))

    assert registry.get("example") is second
    assert registry.get("example", "1.0.0") is first
    for name in ("example", "removed_strategy"):
        with pytest.raises(ToolError) as raised:
            registry.get(name, "0.9.0")
        assert raised.value.code == "strategy_unavailable"
        assert raised.value.details["field"] == "strategy.version"


def test_duplicate_pair_does_not_replace_registration_or_current_version():
    registry = Registry()
    first = ExampleTool("1.0.0")
    second = ExampleTool("2.0.0")
    registry.register(first)
    registry.register(second, current=True)

    with pytest.raises(ValueError, match="example.*1.0.0"):
        registry.register(ExampleTool("1.0.0"), current=True)
    assert registry.get("example") is second
    assert registry.get("example", "1.0.0") is first


def test_catalog_reads_current_tool_schema_and_description():
    registry = Registry()
    registry.register(ExampleTool("1.0.0"))
    current = ExampleTool("2.0.0")
    registry.register(current, current=True)

    (tool,) = registry.catalog()
    assert tool is current
    schema = tool.params_schema()
    assert tool.title == "Пример"
    assert tool.description == "Стратегия 2.0.0"
    assert schema["properties"]["window"]["default"] == 1


def test_normalized_params_are_immutable_even_for_nested_inputs():
    raw = {"filters": [{"window": 3}]}
    params = NormalizedParams(raw)
    raw["filters"][0]["window"] = 9
    assert params["filters"][0]["window"] == 3

    with pytest.raises(TypeError):
        params["filters"][0]["window"] = 4
    with pytest.raises(TypeError):
        params["window"] = 4
    with pytest.raises(FrozenInstanceError):
        params._values = {}


def test_tool_error_preserves_field_and_reason():
    with pytest.raises(ToolError) as raised:
        ExampleTool("1.0.0").validate_params({"window": "1"})
    assert str(raised.value) == "Окно должно быть целым числом"
    assert raised.value.message == "Окно должно быть целым числом"
    assert raised.value.code == "invalid_params"
    assert raised.value.details == {"field": "params.window", "reason": "wrong_type"}


@pytest.mark.parametrize("field", ["code", "message", "details"])
def test_tool_error_fields_are_read_only(field):
    error = ToolError("invalid_params", "Неверное окно", "params.window", "wrong_type")
    original = getattr(error, field)

    with pytest.raises(AttributeError):
        setattr(error, field, "новое значение")

    assert getattr(error, field) == original
