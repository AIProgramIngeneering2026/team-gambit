from collections.abc import Sequence

from .registry import Registry
from .strategy_tool import (
    Candle,
    NormalizedParams,
    Position,
    ToolError,
    ToolResult,
)


class Dispatcher:
    def __init__(self, registry: Registry) -> None:
        self._registry = registry

    def execute(
        self,
        name: str,
        version: str,
        closed_bars: Sequence[Candle],
        position: Position,
        normalized_params: NormalizedParams,
    ) -> ToolResult:
        tool = self._registry.get(name, version)
        try:
            return tool.execute(closed_bars, position, normalized_params)
        except ToolError:
            raise
        except Exception as error:
            raise ToolError(
                "tool_failed",
                "Вызов StrategyTool завершился ошибкой",
                "tool.result",
                "unexpected_exception",
            ) from error
