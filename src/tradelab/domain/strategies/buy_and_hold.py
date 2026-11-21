from collections.abc import Mapping, Sequence
from copy import deepcopy

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
    "$id": "urn:tradelab:buy-and-hold:1.0.0:params",
    "title": "Buy and hold parameters",
    "type": "object",
    "additionalProperties": False,
    "properties": {},
}


class BuyAndHoldTool:
    name = "buy_and_hold"
    version = "1.0.0"
    title = "Buy & hold"
    description = "Покупка на первом закрытом баре и удержание позиции"

    def params_schema(self) -> Mapping[str, ParamValue]:
        return deepcopy(_PARAMS_SCHEMA)

    def validate_params(self, params: object) -> NormalizedParams:
        if not isinstance(params, Mapping):
            raise ToolError(
                "invalid_params", "Параметры должны быть объектом", "params", "wrong_type"
            )
        for key in params:
            raise ToolError(
                "invalid_params",
                f"Неизвестный параметр {key}",
                f"params.{key}",
                "unknown_field",
            )
        return NormalizedParams({})

    def required_bars(self, normalized_params: NormalizedParams) -> int:
        return 1

    def execute(
        self,
        closed_bars: Sequence[Candle],
        position: Position,
        normalized_params: NormalizedParams,
    ) -> ToolResult:
        if not closed_bars:
            return ToolResult(
                Signal.INSUFFICIENT_DATA,
                None,
                SignalReason(
                    "insufficient_history",
                    "Недостаточно истории: требуется 1 закрытый бар, доступно 0",
                ),
                {},
            )
        if position is Position.FLAT:
            return ToolResult(
                Signal.BUY,
                closed_bars[-1].close_time,
                SignalReason("enter_baseline", "Открытие базовой длинной позиции"),
                {},
            )
        return ToolResult(
            Signal.HOLD,
            closed_bars[-1].close_time,
            SignalReason("keep_baseline", "Базовая длинная позиция сохраняется"),
            {},
        )
