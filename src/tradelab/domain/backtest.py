from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import (
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    Context,
    Decimal,
    InvalidOperation,
    localcontext,
)
from itertools import pairwise
from typing import Protocol

from tradelab.domain.backtest_types import (
    BacktestMetrics,
    BacktestPeriod,
    BacktestResult,
    EquityPoint,
    StrategySpec,
    Trade,
)
from tradelab.domain.strategy_tool import (
    Candle,
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
    ToolError,
    ToolResult,
)

_BACKTEST_CONTEXT = Context(prec=28, rounding=ROUND_HALF_EVEN)
_MONEY_QUANTUM = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class InstrumentMetadata:
    ticker: str
    currency: str
    lot_size: int


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    snapshot_id: str
    instrument: InstrumentMetadata
    bars: tuple[Candle, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", tuple(self.bars))


class ToolDispatcher(Protocol):
    def execute(
        self,
        name: str,
        version: str,
        closed_bars: Sequence[Candle],
        position: Position,
        normalized_params: NormalizedParams,
    ) -> ToolResult: ...


@dataclass(frozen=True, slots=True)
class PurchasePlan:
    preliminary_quantity: int
    quantity: int
    commission: Decimal
    cash_after: Decimal


@dataclass(frozen=True, slots=True)
class _OpenPosition:
    quantity: int
    price: Decimal
    commission: Decimal


def _money(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _preliminary_lots(
    cash: Decimal, price: Decimal, commission_rate: Decimal, lot_size: int
) -> int:
    cash_numerator, cash_denominator = cash.as_integer_ratio()
    price_numerator, price_denominator = price.as_integer_ratio()
    rate_numerator, rate_denominator = commission_rate.as_integer_ratio()
    numerator = cash_numerator * price_denominator * rate_denominator
    denominator = (
        cash_denominator
        * lot_size
        * price_numerator
        * (rate_denominator + rate_numerator)
    )
    return numerator // denominator


def calculate_purchase(
    cash: Decimal, price: Decimal, commission_rate: Decimal, lot_size: int
) -> PurchasePlan:
    with localcontext(_BACKTEST_CONTEXT):
        cash = _money(cash)
        preliminary_lots = _preliminary_lots(cash, price, commission_rate, lot_size)

        def affordable(lots: int) -> bool:
            quantity = lots * lot_size
            commission = _money(Decimal(quantity) * price * commission_rate)
            return Decimal(quantity) * price + commission <= cash

        if affordable(preliminary_lots):
            low = preliminary_lots
            high = max(1, preliminary_lots + 1)
            while affordable(high):
                low = high
                high *= 2
        else:
            low = 0
            high = preliminary_lots

        while high - low > 1:
            middle = (low + high) // 2
            if affordable(middle):
                low = middle
            else:
                high = middle

        quantity = low * lot_size
        commission = _money(Decimal(quantity) * price * commission_rate)
        cash_after = _money(cash - Decimal(quantity) * price - commission)
        return PurchasePlan(
            preliminary_quantity=preliminary_lots * lot_size,
            quantity=quantity,
            commission=commission,
            cash_after=cash_after,
        )


def _tool_failed(message: str, reason: str) -> ToolError:
    return ToolError("tool_failed", message, "tool.result", reason)


def _validated_tool_result(
    result: ToolResult, bar: Candle, position: Position
) -> ToolResult:
    signal = result.signal
    bar_time = result.bar_time
    reason = result.reason
    indicators = result.indicators
    if not isinstance(signal, Signal):
        raise _tool_failed("Tool вернул неизвестный сигнал", "invalid_signal")
    if not isinstance(bar_time, datetime) or bar_time != bar.close_time:
        raise _tool_failed("Tool вернул время другого бара", "invalid_bar_time")
    if not isinstance(reason, SignalReason):
        raise _tool_failed("Tool вернул неверное обоснование", "invalid_reason")
    reason = SignalReason(reason.code, reason.message)
    if (
        not isinstance(reason.code, str)
        or not isinstance(reason.message, str)
        or not reason.code.strip()
        or not reason.message.strip()
    ):
        raise _tool_failed("Обоснование Tool должно содержать код и текст", "invalid_reason")
    if not isinstance(indicators, Mapping):
        raise _tool_failed("Индикаторы Tool должны быть объектом", "invalid_indicators")
    indicators = dict(indicators)
    for key, value in indicators.items():
        if not isinstance(key, str):
            raise _tool_failed("Имя индикатора должно быть строкой", "invalid_indicators")
        try:
            invalid_indicator = value is not None and (
                not isinstance(value, str) or not Decimal(value).is_finite()
            )
        except InvalidOperation:
            invalid_indicator = True
        if invalid_indicator:
            raise _tool_failed(
                "Индикатор должен быть конечной десятичной строкой или null",
                "invalid_indicators",
            )
    if signal is Signal.BUY and position is not Position.FLAT:
        raise _tool_failed("Tool вернул BUY при открытой позиции", "invalid_position")
    if signal is Signal.SELL and position is not Position.LONG:
        raise _tool_failed("Tool вернул SELL без открытой позиции", "invalid_position")
    return ToolResult(
        signal=signal, bar_time=bar.close_time, reason=reason, indicators=indicators
    )


class BacktestEngine:
    version = "1.0.0"

    def __init__(self, dispatcher: ToolDispatcher) -> None:
        self._dispatcher = dispatcher

    def run(
        self,
        snapshot: MarketSnapshot,
        period: BacktestPeriod,
        strategy: StrategySpec,
        baseline: StrategySpec,
        initial_capital: Decimal,
        commission_rate: Decimal,
        *,
        clock: Callable[[], datetime] | None = None,
        deadline_at: datetime | None = None,
    ) -> BacktestResult:
        with localcontext(_BACKTEST_CONTEXT):
            initial_cash = _money(initial_capital)
            result = self._run_single(
                snapshot,
                period,
                strategy,
                initial_cash,
                commission_rate,
                clock=clock,
                deadline_at=deadline_at,
            )
            baseline_result = self._run_single(
                snapshot,
                period,
                baseline,
                initial_cash,
                commission_rate,
                clock=clock,
                deadline_at=deadline_at,
            )
            metrics = replace(
                result.metrics,
                buy_and_hold_return=baseline_result.metrics.total_return,
            )
            return replace(result, metrics=metrics)

    def _execute_tool(
        self,
        strategy: StrategySpec,
        closed_bars: tuple[Candle, ...],
        position: Position,
        *,
        clock: Callable[[], datetime] | None,
        deadline_at: datetime | None,
    ) -> ToolResult:
        if clock is not None and deadline_at is not None and deadline_at < clock():
            raise ToolError(
                "job_timeout",
                "Истёк срок выполнения задания",
                "deadline_at",
                "job_timeout",
            )
        try:
            result = self._dispatcher.execute(
                strategy.name,
                strategy.version,
                closed_bars,
                position,
                strategy.normalized_params,
            )
            return _validated_tool_result(result, closed_bars[-1], position)
        except ToolError:
            raise
        except Exception as error:
            raise _tool_failed(
                "Вызов StrategyTool завершился ошибкой", "unexpected_exception"
            ) from error

    def _run_single(
        self,
        snapshot: MarketSnapshot,
        period: BacktestPeriod,
        strategy: StrategySpec,
        initial_cash: Decimal,
        commission_rate: Decimal,
        *,
        clock: Callable[[], datetime] | None = None,
        deadline_at: datetime | None = None,
    ) -> BacktestResult:
        cash = initial_cash
        quantity = 0
        open_position: _OpenPosition | None = None
        pending: ToolResult | None = None
        history: list[Candle] = []
        signals: list[ToolResult] = []
        trades: list[Trade] = []
        equity_curve: list[EquityPoint] = []
        has_usable_bar = False

        for bar in snapshot.bars:
            if bar.trading_date < period.start:
                history.append(bar)
                continue
            if bar.trading_date > period.end:
                break

            if pending is not None:
                if pending.signal is Signal.BUY:
                    purchase = calculate_purchase(
                        cash, bar.open, commission_rate, snapshot.instrument.lot_size
                    )
                    if purchase.quantity:
                        quantity = purchase.quantity
                        cash = purchase.cash_after
                        open_position = _OpenPosition(
                            quantity, bar.open, purchase.commission
                        )
                        trades.append(
                            Trade(
                                Signal.BUY,
                                pending.bar_time,
                                bar.open_time,
                                bar.open,
                                quantity,
                                purchase.commission,
                                cash,
                                pending.reason,
                                None,
                            )
                        )
                elif pending.signal is Signal.SELL:
                    if quantity <= 0 or open_position is None:
                        raise _tool_failed(
                            "Нет позиции для исполнения SELL", "invalid_position"
                        )
                    commission = _money(
                        Decimal(quantity) * bar.open * commission_rate
                    )
                    cash_after = _money(
                        cash + Decimal(quantity) * bar.open - commission
                    )
                    pnl = (
                        Decimal(quantity) * bar.open
                        - commission
                        - Decimal(open_position.quantity) * open_position.price
                        - open_position.commission
                    )
                    trades.append(
                        Trade(
                            Signal.SELL,
                            pending.bar_time,
                            bar.open_time,
                            bar.open,
                            quantity,
                            commission,
                            cash_after,
                            pending.reason,
                            pnl,
                        )
                    )
                    cash = cash_after
                    quantity = 0
                    open_position = None
                pending = None

            equity = cash + Decimal(quantity) * bar.close
            equity_curve.append(EquityPoint(bar.trading_date, equity))
            history.append(bar)
            position = Position.LONG if quantity else Position.FLAT
            result = self._execute_tool(
                strategy,
                tuple(history),
                position,
                clock=clock,
                deadline_at=deadline_at,
            )
            signals.append(result)
            if result.signal is not Signal.INSUFFICIENT_DATA:
                has_usable_bar = True
            pending = result

        if not equity_curve:
            raise ToolError(
                "insufficient_data",
                "В периоде бэктеста нет закрытых баров",
                "period",
                "insufficient_data",
            )
        if not has_usable_bar:
            raise ToolError(
                "insufficient_data",
                "В периоде бэктеста нет бара с достаточной историей",
                "period",
                "insufficient_data",
            )

        metrics = _calculate_metrics(initial_cash, equity_curve, trades)
        return BacktestResult(
            snapshot.snapshot_id,
            strategy,
            period,
            self.version,
            metrics,
            Position.LONG if quantity else Position.FLAT,
            cash,
            quantity,
            tuple(equity_curve),
            tuple(trades),
            tuple(signals),
        )


def _calculate_metrics(
    initial_capital: Decimal,
    equity_curve: Sequence[EquityPoint],
    trades: Sequence[Trade],
) -> BacktestMetrics:
    final_equity = equity_curve[-1].equity
    total_return = final_equity / initial_capital - 1

    peak = initial_capital
    max_drawdown = Decimal(0)
    for point in equity_curve:
        peak = max(peak, point.equity)
        max_drawdown = min(max_drawdown, point.equity / peak - 1)

    equities = (initial_capital, *(point.equity for point in equity_curve))
    returns = tuple(
        current / previous - 1 for previous, current in pairwise(equities)
    )
    sharpe_ratio = None
    if len(returns) >= 2:
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(
            ((item - mean) ** 2 for item in returns), Decimal(0)
        ) / Decimal(len(returns) - 1)
        standard_deviation = variance.sqrt()
        if standard_deviation:
            sharpe_ratio = mean / standard_deviation * Decimal(252).sqrt()

    position_returns: list[Decimal] = []
    profitable = 0
    open_buy: Trade | None = None
    for trade in trades:
        if trade.side is Signal.BUY:
            open_buy = trade
            continue
        if open_buy is None or trade.pnl is None:
            raise ValueError("SELL без соответствующей покупки")
        full_cost = (
            Decimal(open_buy.quantity) * open_buy.price + open_buy.commission
        )
        position_returns.append(trade.pnl / full_cost)
        if trade.pnl > 0:
            profitable += 1
        open_buy = None

    closed_count = len(position_returns)
    win_rate = None
    average_return = None
    if closed_count:
        win_rate = Decimal(profitable) / Decimal(closed_count)
        average_return = sum(position_returns, Decimal(0)) / Decimal(closed_count)

    return BacktestMetrics(
        total_return=total_return,
        buy_and_hold_return=None,
        max_drawdown=max_drawdown,
        sharpe_ratio=sharpe_ratio,
        final_equity=final_equity,
        trades_count=len(trades),
        closed_positions_count=closed_count,
        win_rate=win_rate,
        avg_position_return=average_return,
    )
