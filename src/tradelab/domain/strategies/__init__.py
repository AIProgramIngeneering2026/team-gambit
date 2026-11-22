from tradelab.domain.registry import Registry
from tradelab.domain.strategies.buy_and_hold import BuyAndHoldTool
from tradelab.domain.strategies.sma_crossover import SmaCrossoverTool

__all__ = ["BuyAndHoldTool", "SmaCrossoverTool", "register_builtin_strategies"]


def register_builtin_strategies(registry: Registry) -> None:
    registry.register(BuyAndHoldTool())
    registry.register(SmaCrossoverTool())
