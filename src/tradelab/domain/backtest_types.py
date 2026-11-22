from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
    ToolResult,
)


@dataclass(frozen=True, slots=True)
class BacktestPeriod:
    start: date
    end: date


@dataclass(frozen=True, slots=True)
class StrategySpec:
    name: str
    version: str
    normalized_params: NormalizedParams


@dataclass(frozen=True, slots=True)
class EquityPoint:
    date: date
    equity: Decimal


@dataclass(frozen=True, slots=True)
class Trade:
    side: Signal
    signal_time: datetime
    execution_time: datetime
    price: Decimal
    quantity: int
    commission: Decimal
    cash_after: Decimal
    reason: SignalReason
    pnl: Decimal | None

    @property
    def signal_date(self) -> date:
        return self.signal_time.date()

    @property
    def execution_date(self) -> date:
        return self.execution_time.date()


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_return: Decimal
    buy_and_hold_return: Decimal | None
    max_drawdown: Decimal
    sharpe_ratio: Decimal | None
    final_equity: Decimal
    trades_count: int
    closed_positions_count: int
    win_rate: Decimal | None
    avg_position_return: Decimal | None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    snapshot_id: str
    strategy: StrategySpec
    period: BacktestPeriod
    engine_version: str
    metrics: BacktestMetrics
    final_position: Position
    final_cash: Decimal
    final_quantity: int
    equity_curve: tuple[EquityPoint, ...]
    trades: tuple[Trade, ...]
    signals: tuple[ToolResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "equity_curve", tuple(self.equity_curve))
        object.__setattr__(self, "trades", tuple(self.trades))
        object.__setattr__(self, "signals", tuple(self.signals))
