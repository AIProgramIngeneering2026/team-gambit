from collections.abc import Mapping, Sequence
from copy import deepcopy
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from typing import cast

from tradelab.domain.strategy_tool import (
    Candle,
    NormalizedParams,
    ParamValue,
    Position,
    Signal,
    SignalReason,
    ToolError,
    ToolResult,
)

_PARAMS_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "urn:tradelab:sma-crossover:1.0.0:params",
    "title": "SMA crossover parameters",
    "description": "validate_params additionally enforces fast < slow and applies defaults.",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "fast": {"type": "integer", "minimum": 1, "maximum": 499, "default": 20},
        "slow": {"type": "integer", "minimum": 2, "maximum": 500, "default": 50},
    },
}
_SMA_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
_INDICATOR_QUANTUM = Decimal("0.0000000001")
_EMPTY_INDICATORS = {
    "fast_previous": None,
    "slow_previous": None,
    "fast_current": None,
    "slow_current": None,
}


def _invalid_params(message: str, field: str, reason: str) -> ToolError:
    return ToolError("invalid_params", message, field, reason)


def _average(values: Sequence[Decimal]) -> Decimal:
    total = Decimal(0)
    for value in values:
        total += value
    return total / Decimal(len(values))


def _format_indicator(value: Decimal) -> str:
    required_precision = value.adjusted() - _INDICATOR_QUANTUM.as_tuple().exponent + 2
    with localcontext(_SMA_CONTEXT) as context:
        context.prec = max(context.prec, required_precision)
        rounded = value.quantize(_INDICATOR_QUANTUM, rounding=ROUND_HALF_EVEN)
        if not rounded.is_finite():
            raise ValueError("Индикатор SMA не является конечным числом")
        if rounded == 0:
            return "0"
        return format(rounded.normalize(), "f")


class SmaCrossoverTool:
    name = "sma_crossover"
    version = "1.0.0"
    title = "SMA crossover"
    description = "Пересечение быстрой и медленной простых скользящих средних"

    def params_schema(self) -> Mapping[str, ParamValue]:
        return deepcopy(_PARAMS_SCHEMA)

    def validate_params(self, params: object) -> NormalizedParams:
        if not isinstance(params, Mapping):
            raise _invalid_params("Параметры должны быть объектом", "params", "wrong_type")
        for key in params:
            if key not in {"fast", "slow"}:
                raise _invalid_params(
                    f"Неизвестный параметр {key}", f"params.{key}", "unknown_field"
                )

        normalized = {"fast": params.get("fast", 20), "slow": params.get("slow", 50)}
        limits = {"fast": (1, 499), "slow": (2, 500)}
        for field in ("fast", "slow"):
            value = normalized[field]
            if type(value) is not int:
                raise _invalid_params(
                    f"Параметр {field} должен быть целым числом",
                    f"params.{field}",
                    "wrong_type",
                )
            minimum, maximum = limits[field]
            if not minimum <= value <= maximum:
                raise _invalid_params(
                    f"Параметр {field} должен быть от {minimum} до {maximum}",
                    f"params.{field}",
                    "out_of_range",
                )
        if normalized["fast"] >= normalized["slow"]:
            raise _invalid_params(
                "fast должен быть меньше slow",
                "params.fast",
                "must_be_less_than_slow",
            )
        return NormalizedParams(normalized)

    def required_bars(self, normalized_params: NormalizedParams) -> int:
        return cast(int, normalized_params["slow"]) + 1

    def execute(
        self,
        closed_bars: Sequence[Candle],
        position: Position,
        normalized_params: NormalizedParams,
    ) -> ToolResult:
        fast = cast(int, normalized_params["fast"])
        slow = cast(int, normalized_params["slow"])
        required = slow + 1
        bar_time = closed_bars[-1].close_time if closed_bars else None
        if len(closed_bars) < required:
            return ToolResult(
                signal=Signal.INSUFFICIENT_DATA,
                bar_time=bar_time,
                reason=SignalReason(
                    "insufficient_history",
                    f"Недостаточно истории: требуется {required} закрытых баров, доступно {len(closed_bars)}",
                ),
                indicators=_EMPTY_INDICATORS,
            )

        with localcontext(_SMA_CONTEXT):
            current_closes = tuple(bar.close for bar in closed_bars[-required:])
            previous_closes = current_closes[:-1]
            fast_previous = _average(previous_closes[-fast:])
            slow_previous = _average(previous_closes[-slow:])
            fast_current = _average(current_closes[-fast:])
            slow_current = _average(current_closes[-slow:])
            indicators = {
                "fast_previous": _format_indicator(fast_previous),
                "slow_previous": _format_indicator(slow_previous),
                "fast_current": _format_indicator(fast_current),
                "slow_current": _format_indicator(slow_current),
            }

            if fast_previous <= slow_previous and fast_current > slow_current:
                if position is Position.FLAT:
                    return ToolResult(
                        Signal.BUY,
                        bar_time,
                        SignalReason(
                            "cross_up", "Быстрая SMA пересекла медленную снизу вверх"
                        ),
                        indicators,
                    )
                return ToolResult(
                    Signal.HOLD,
                    bar_time,
                    SignalReason(
                        "cross_up_while_long",
                        "Быстрая SMA пересекла медленную снизу вверх, позиция уже открыта",
                    ),
                    indicators,
                )
            if fast_previous >= slow_previous and fast_current < slow_current:
                if position is Position.LONG:
                    return ToolResult(
                        Signal.SELL,
                        bar_time,
                        SignalReason(
                            "cross_down", "Быстрая SMA пересекла медленную сверху вниз"
                        ),
                        indicators,
                    )
                return ToolResult(
                    Signal.HOLD,
                    bar_time,
                    SignalReason(
                        "cross_down_while_flat",
                        "Быстрая SMA пересекла медленную сверху вниз, позиция не открыта",
                    ),
                    indicators,
                )

        return ToolResult(
            Signal.HOLD,
            bar_time,
            SignalReason("no_cross", "Пересечение скользящих средних отсутствует"),
            indicators,
        )
