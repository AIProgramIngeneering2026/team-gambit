import pytest

from tradelab.domain.dispatcher import Dispatcher
from tradelab.domain.registry import Registry
from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
    ToolError,
    ToolResult,
)


class ArbitraryTool:
    name = "arbitrary_tool"
    version = "4.5.6"
    title = "Произвольная стратегия"
    description = "Тестовая стратегия без специальной обработки в ядре"

    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def execute(self, closed_bars, position, normalized_params):
        self.calls.append((closed_bars, position, normalized_params))
        if self.error is not None:
            raise self.error
        return ToolResult(
            Signal.HOLD,
            closed_bars[-1].close_time,
            SignalReason("hold", "Позиция сохранена"),
            {},
        )


def test_dispatcher_executes_arbitrary_tool_by_exact_version(contract_bars):
    registry = Registry()
    tool = ArbitraryTool()
    registry.register(tool)
    dispatcher = Dispatcher(registry)
    bars = contract_bars[:1]
    params = NormalizedParams({"window": 3})

    result = dispatcher.execute(
        tool.name, tool.version, bars, Position.FLAT, params
    )

    assert result.signal is Signal.HOLD
    assert tool.calls == [(bars, Position.FLAT, params)]


def test_dispatcher_preserves_unknown_operation_from_registry(contract_bars):
    error = ToolError(
        "unknown_operation",
        "Стратегия не зарегистрирована",
        "strategy",
        "not_found",
    )

    class UnknownRegistry:
        def get(self, name, version):
            assert (name, version) == ("missing", "1.0.0")
            raise error

    with pytest.raises(ToolError) as raised:
        Dispatcher(UnknownRegistry()).execute(
            "missing",
            "1.0.0",
            contract_bars[:1],
            Position.FLAT,
            NormalizedParams({}),
        )

    assert raised.value is error


def test_dispatcher_preserves_domain_tool_error(contract_bars):
    error = ToolError(
        "insufficient_data",
        "Недостаточно данных для правила",
        "period",
        "insufficient_data",
    )
    registry = Registry()
    registry.register(ArbitraryTool(error))

    with pytest.raises(ToolError) as raised:
        Dispatcher(registry).execute(
            ArbitraryTool.name,
            ArbitraryTool.version,
            contract_bars[:1],
            Position.FLAT,
            NormalizedParams({}),
        )

    assert raised.value is error


def test_dispatcher_wraps_unexpected_tool_exception_with_cause(contract_bars):
    failure = RuntimeError("boom")
    registry = Registry()
    registry.register(ArbitraryTool(failure))

    with pytest.raises(ToolError) as raised:
        Dispatcher(registry).execute(
            ArbitraryTool.name,
            ArbitraryTool.version,
            contract_bars[:1],
            Position.FLAT,
            NormalizedParams({}),
        )

    error = raised.value
    assert error.code == "tool_failed"
    assert error.details == {
        "field": "tool.result",
        "reason": "unexpected_exception",
    }
    assert error.__cause__ is failure
