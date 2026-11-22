from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, Rounded, localcontext
from types import SimpleNamespace
from typing import get_type_hints

import pytest

from tradelab.domain.backtest import (
    BacktestEngine,
    BacktestPeriod,
    InstrumentMetadata,
    MarketSnapshot,
    StrategySpec,
    calculate_purchase,
)
from tradelab.domain.job import Job
from tradelab.domain.job_lifecycle import JobLifecycle
from tradelab.domain.job_repository import InMemoryJobRepository
from tradelab.domain.registry import Registry
from tradelab.domain.strategies import register_builtin_strategies
from tradelab.domain.strategy_tool import (
    NormalizedParams,
    Position,
    Signal,
    SignalReason,
    ToolError,
    ToolResult,
)


class RegistryDispatcher:
    def __init__(self, registry):
        self.registry = registry
        self.calls = []

    def execute(
        self, name, version, closed_bars, position, normalized_params
    ):
        self.calls.append((name, version, closed_bars, position, normalized_params))
        return self.registry.get(name, version).execute(
            closed_bars, position, normalized_params
        )


class FunctionalDispatcher:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def execute(
        self, name, version, closed_bars, position, normalized_params
    ):
        self.calls.append((name, version, closed_bars, position, normalized_params))
        return self.handler(name, closed_bars, position)


def make_bars(prices, start=date(2026, 11, 2)):
    bars = []
    for index, (open_price, close_price) in enumerate(prices):
        trading_date = start + timedelta(days=index)
        open_value = Decimal(open_price)
        close_value = Decimal(close_price)
        bars.append(
            SimpleNamespace(
                trading_date=trading_date,
                open_time=datetime.combine(trading_date, datetime.min.time(), UTC)
                + timedelta(hours=7),
                close_time=datetime.combine(trading_date, datetime.min.time(), UTC)
                + timedelta(hours=18),
                open=open_value,
                high=max(open_value, close_value),
                low=min(open_value, close_value),
                close=close_value,
                volume=Decimal(1000),
            )
        )
    return tuple(bars)


def make_candles(prices, start=date(2026, 11, 2)):
    from tradelab.domain.strategy_tool import Candle

    return tuple(
        Candle(
            trading_date=bar.trading_date,
            open_time=bar.open_time,
            close_time=bar.close_time,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
        )
        for bar in make_bars(prices, start)
    )


def tool_result(signal, bars, code="test", indicators=None):
    return ToolResult(
        signal=signal,
        bar_time=bars[-1].close_time if bars else None,
        reason=SignalReason(code, "Пояснение тестового правила"),
        indicators={} if indicators is None else indicators,
    )


def spec(name="primary", params=None):
    return StrategySpec(name, "1.0.0", NormalizedParams(params or {}))


def run_engine(
    dispatcher,
    bars,
    *,
    period=None,
    strategy=None,
    baseline=None,
    capital="1000.00",
    commission="0",
    lot_size=1,
):
    if period is None:
        period = BacktestPeriod(bars[0].trading_date, bars[-1].trading_date)
    snapshot = MarketSnapshot(
        "5d6e7f80-91a2-4b3c-8d4e-5f6a7b8c9d0e",
        InstrumentMetadata("TEST", "RUB", lot_size),
        bars,
    )
    return BacktestEngine(dispatcher).run(
        snapshot=snapshot,
        period=period,
        strategy=strategy or spec(),
        baseline=baseline or spec("baseline"),
        initial_capital=Decimal(capital),
        commission_rate=Decimal(commission),
    )


def real_spec(registry, name, params):
    tool = registry.get(name, "1.0.0")
    return StrategySpec(name, tool.version, tool.validate_params(params))


def run_real_contract(case, contract_cases, contract_bars, dispatcher):
    registry = dispatcher.registry
    period_data = contract_cases["period"]
    return run_engine(
        dispatcher,
        contract_bars,
        period=BacktestPeriod(
            date.fromisoformat(period_data["start"]),
            date.fromisoformat(period_data["end"]),
        ),
        strategy=real_spec(registry, case["strategy"], case["params"]),
        baseline=real_spec(registry, "buy_and_hold", {}),
        capital=contract_cases["initial_capital"],
        commission=contract_cases["commission_rate"],
        lot_size=contract_cases["instrument"]["lot_size"],
    )


def nullable_decimal(value):
    return None if value is None else Decimal(value)


def test_i33_1_and_3_contract_backtests(contract_cases, contract_bars):
    for case in contract_cases["backtests"]:
        dispatcher = RegistryDispatcher(Registry())
        register_builtin_strategies(dispatcher.registry)

        result = run_real_contract(case, contract_cases, contract_bars, dispatcher)

        assert [item.signal.value for item in result.signals] == case["expected_signals"]
        assert [item.reason.code for item in result.signals] == case["expected_reason_codes"]
        assert len(result.trades) == len(case["expected_executions"])
        for trade, expected in zip(result.trades, case["expected_executions"]):
            assert trade.side.value == expected["side"]
            assert trade.signal_time == datetime.fromisoformat(expected["signal_time"])
            assert trade.execution_time == datetime.fromisoformat(
                expected["execution_time"]
            )
            assert trade.signal_date == trade.signal_time.date()
            assert trade.execution_date == trade.execution_time.date()
            assert trade.quantity == expected["quantity"]
            assert trade.price == Decimal(expected["price"])
            assert trade.commission == Decimal(expected["commission"])
            assert trade.cash_after == Decimal(expected["cash_after"])
            assert trade.pnl == nullable_decimal(expected["pnl"])
            assert trade.reason.code
            assert trade.reason.message

        assert result.final_quantity == case["expected_final_quantity"]
        assert result.final_cash == Decimal(case["expected_final_cash"])
        assert result.final_position is (
            Position.LONG if result.final_quantity else Position.FLAT
        )
        assert [point.equity for point in result.equity_curve] == [
            Decimal(value) for value in case["expected_equity_curve"]
        ]
        assert [point.date for point in result.equity_curve] == [
            bar.trading_date for bar in contract_bars
        ]

        metrics = result.metrics
        assert metrics.final_equity == Decimal(case["expected_final_equity"])
        assert metrics.total_return == Decimal(case["expected_total_return"])
        assert metrics.buy_and_hold_return == Decimal(
            case["expected_buy_and_hold_return"]
        )
        assert metrics.max_drawdown == Decimal(case["expected_max_drawdown"])
        assert metrics.sharpe_ratio == nullable_decimal(case["expected_sharpe_ratio"])
        assert metrics.trades_count == case["expected_trades_count"]
        assert metrics.closed_positions_count == case["expected_closed_positions_count"]
        assert metrics.win_rate == nullable_decimal(case["expected_win_rate"])
        assert metrics.avg_position_return == nullable_decimal(
            case["expected_avg_position_return"]
        )


def test_i33_1_identical_inputs_produce_identical_results(
    contract_cases, contract_bars
):
    case = contract_cases["backtests"][0]
    dispatchers = []
    results = []
    for _ in range(2):
        dispatcher = RegistryDispatcher(Registry())
        register_builtin_strategies(dispatcher.registry)
        dispatchers.append(dispatcher)
        results.append(run_real_contract(case, contract_cases, contract_bars, dispatcher))

    assert results[0] == results[1]
    assert len(dispatchers[0].calls) == len(dispatchers[1].calls)


def test_i33_1_tool_receives_only_immutable_prefixes():
    bars = make_candles([("10", "10"), ("11", "11"), ("12", "12")])

    def hold(name, closed_bars, position):
        assert isinstance(closed_bars, tuple)
        return tool_result(Signal.HOLD, closed_bars)

    dispatcher = FunctionalDispatcher(hold)
    run_engine(dispatcher, bars)

    primary_calls = [call for call in dispatcher.calls if call[0] == "primary"]
    assert [len(call[2]) for call in primary_calls] == [1, 2, 3]
    for index, call in enumerate(primary_calls):
        assert call[2] == bars[: index + 1]
        assert call[3] is Position.FLAT


def test_i33_1_tool_cannot_mutate_snapshot():
    bars = make_candles([("10", "10")])
    snapshot = MarketSnapshot(
        "snapshot", InstrumentMetadata("TEST", "RUB", 1), bars
    )

    def mutate(name, closed_bars, position):
        closed_bars[0] = closed_bars[0]

    with pytest.raises(ToolError) as raised:
        BacktestEngine(FunctionalDispatcher(mutate)).run(
            snapshot,
            BacktestPeriod(bars[0].trading_date, bars[0].trading_date),
            spec(),
            spec("baseline"),
            Decimal(1000),
            Decimal(0),
        )

    assert raised.value.code == "tool_failed"
    assert snapshot.bars == bars
    with pytest.raises(FrozenInstanceError):
        snapshot.bars = ()


def test_i33_2_warmup_is_used_without_preperiod_trading(contract_bars):
    registry = Registry()
    register_builtin_strategies(registry)
    dispatcher = RegistryDispatcher(registry)
    period = BacktestPeriod(
        contract_bars[4].trading_date, contract_bars[-1].trading_date
    )

    result = run_engine(
        dispatcher,
        contract_bars,
        period=period,
        strategy=real_spec(registry, "sma_crossover", {"fast": 2, "slow": 3}),
        baseline=real_spec(registry, "buy_and_hold", {}),
        commission="0.001",
        lot_size=10,
    )

    assert [item.signal for item in result.signals] == [
        Signal.BUY,
        Signal.SELL,
        Signal.HOLD,
    ]
    assert [trade.execution_time for trade in result.trades] == [
        contract_bars[5].open_time,
        contract_bars[6].open_time,
    ]
    assert all(trade.signal_time.date() >= period.start for trade in result.trades)
    assert [point.date for point in result.equity_curve] == [
        bar.trading_date for bar in contract_bars[4:]
    ]
    primary_calls = [call for call in dispatcher.calls if call[0] == "sma_crossover"]
    assert [len(call[2]) for call in primary_calls] == [5, 6, 7]
    assert all(
        call[2][-1].trading_date >= period.start for call in primary_calls
    )


def test_i33_2_last_signal_has_no_execution_without_next_bar():
    registry = Registry()
    register_builtin_strategies(registry)
    dispatcher = RegistryDispatcher(registry)
    bars = make_candles([("10", "10")])

    result = run_engine(
        dispatcher,
        bars,
        strategy=real_spec(registry, "buy_and_hold", {}),
        baseline=real_spec(registry, "buy_and_hold", {}),
        lot_size=10,
    )

    assert result.signals[-1].signal is Signal.BUY
    assert result.trades == ()
    assert result.final_position is Position.FLAT
    assert result.final_quantity == 0
    assert result.final_cash == Decimal("1000.00")
    assert result.metrics.total_return == 0
    assert result.metrics.max_drawdown == 0
    assert result.metrics.sharpe_ratio is None
    assert result.metrics.closed_positions_count == 0
    assert result.metrics.win_rate is None
    assert result.metrics.avg_position_return is None


def test_i33_3_insufficient_money_is_successful_and_retries_from_flat():
    registry = Registry()
    register_builtin_strategies(registry)
    dispatcher = RegistryDispatcher(registry)
    bars = make_candles([("200", "200"), ("200", "200"), ("200", "200")])

    result = run_engine(
        dispatcher,
        bars,
        strategy=real_spec(registry, "buy_and_hold", {}),
        baseline=real_spec(registry, "buy_and_hold", {}),
        lot_size=10,
    )

    assert [item.signal for item in result.signals] == [Signal.BUY] * 3
    assert result.trades == ()
    assert result.final_position is Position.FLAT
    assert result.final_cash == Decimal("1000.00")
    assert result.metrics.final_equity == Decimal("1000.00")


def test_i33_3_commission_boundaries(contract_cases):
    for case in contract_cases["commission_boundaries"]:
        purchase = calculate_purchase(
            cash=Decimal(case["cash"]),
            price=Decimal(case["price"]),
            commission_rate=Decimal(case["commission_rate"]),
            lot_size=case["lot_size"],
        )
        assert purchase.preliminary_quantity == case["expected_preliminary_quantity"]
        assert purchase.quantity == case["expected_quantity"]
        assert purchase.commission == Decimal(case["expected_commission"])
        assert purchase.cash_after == Decimal(case["expected_cash_after"])


def test_i33_5_entirely_insufficient_period_is_error(contract_bars):
    registry = Registry()
    register_builtin_strategies(registry)
    dispatcher = RegistryDispatcher(registry)

    with pytest.raises(ToolError) as raised:
        run_engine(
            dispatcher,
            contract_bars[:3],
            strategy=real_spec(registry, "sma_crossover", {"fast": 2, "slow": 3}),
            baseline=real_spec(registry, "buy_and_hold", {}),
        )

    assert raised.value.code == "insufficient_data"


def test_i33_5_empty_period_is_insufficient_data():
    with pytest.raises(ToolError) as raised:
        run_engine(
            FunctionalDispatcher(lambda name, bars, position: None),
            (),
            period=BacktestPeriod(date(2026, 11, 2), date(2026, 11, 3)),
        )
    assert raised.value.code == "insufficient_data"


def test_i33_5_sufficient_history_without_trades_is_successful(contract_bars):
    registry = Registry()
    register_builtin_strategies(registry)
    dispatcher = RegistryDispatcher(registry)

    result = run_engine(
        dispatcher,
        contract_bars[:4],
        strategy=real_spec(registry, "sma_crossover", {"fast": 2, "slow": 3}),
        baseline=real_spec(registry, "buy_and_hold", {}),
    )

    assert result.signals[-1].signal is Signal.HOLD
    assert result.trades == ()
    assert result.metrics.total_return == 0
    assert result.metrics.trades_count == 0


def test_i33_6_unknown_signal_is_tool_failed():
    bars = make_candles([("10", "10")])

    def invalid(name, closed_bars, position):
        return SimpleNamespace(
            signal="UNKNOWN",
            bar_time=closed_bars[-1].close_time,
            reason=SignalReason("bad", "Неверный сигнал"),
            indicators={},
        )

    with pytest.raises(ToolError) as raised:
        run_engine(FunctionalDispatcher(invalid), bars)
    assert raised.value.code == "tool_failed"


def test_i33_6_mutable_tool_response_is_copied_before_storage():
    bars = make_candles([("10", "10")])
    response = SimpleNamespace(
        signal=Signal.HOLD,
        bar_time=bars[-1].close_time,
        reason=SignalReason("test", "Пояснение"),
        indicators={"value": "1"},
    )

    def respond(name, closed_bars, position):
        return response

    result = run_engine(FunctionalDispatcher(respond), bars)
    response.signal = Signal.SELL
    response.reason = SignalReason("changed", "Изменённое пояснение")
    response.indicators["value"] = "NaN"

    (saved,) = result.signals
    assert type(saved) is ToolResult
    assert saved.signal is Signal.HOLD
    assert saved.reason.code == "test"
    assert saved.indicators == {"value": "1"}
    with pytest.raises(TypeError):
        saved.indicators["value"] = "2"


def test_i33_4_job_stores_the_canonical_engine_result():
    bars = make_candles([("10", "10")])

    def hold(name, closed_bars, position):
        return tool_result(Signal.HOLD, closed_bars)

    result = run_engine(FunctionalDispatcher(hold), bars)
    assert get_type_hints(Job)["result"] == type(result) | None

    now = bars[0].close_time
    repository = InMemoryJobRepository()
    job = Job("engine-result", now, snapshot_id=result.snapshot_id)
    repository.add(job)
    lifecycle = JobLifecycle(repository)
    lifecycle.enqueue(job.id, now)
    lifecycle.start(
        job.id,
        "worker-1",
        now,
        lease_expires_at=now + timedelta(seconds=30),
        deadline_at=now + timedelta(minutes=5),
    )
    lifecycle.complete(job.id, "worker-1", result, now)
    assert repository.get(job.id).result is result


def test_i33_1_engine_is_independent_of_callers_decimal_context(
    contract_cases, contract_bars
):
    registry = Registry()
    register_builtin_strategies(registry)
    case = contract_cases["backtests"][0]
    purchase_args = (Decimal(1000), Decimal(11), Decimal("0.0005"), 1)
    expected = run_real_contract(
        case, contract_cases, contract_bars, RegistryDispatcher(registry)
    )
    expected_purchase = calculate_purchase(*purchase_args)

    with localcontext() as caller:
        caller.prec = 6
        caller.rounding = ROUND_UP
        caller.traps[Inexact] = True
        caller.traps[Rounded] = True

        actual = run_real_contract(
            case, contract_cases, contract_bars, RegistryDispatcher(registry)
        )
        assert actual == expected
        assert calculate_purchase(*purchase_args) == expected_purchase
        assert caller.prec == 6
        assert caller.rounding == ROUND_UP
        assert caller.traps[Inexact] and caller.traps[Rounded]


@pytest.mark.parametrize(
    "field", ["bar_time", "bar_time_impostor", "reason_code", "reason_message"]
)
def test_i33_6_malformed_result_is_tool_failed(field):
    bars = make_candles([("10", "10")])

    class EqualBarTime:
        def __eq__(self, other):
            return True

    def invalid(name, closed_bars, position):
        result = tool_result(Signal.HOLD, closed_bars)
        values = {
            "signal": result.signal,
            "bar_time": result.bar_time,
            "reason": result.reason,
            "indicators": result.indicators,
        }
        if field == "bar_time":
            values["bar_time"] = closed_bars[-1].open_time
        elif field == "bar_time_impostor":
            values["bar_time"] = EqualBarTime()
        elif field == "reason_code":
            values["reason"] = SignalReason("", "Пустой код")
        else:
            values["reason"] = SignalReason("bad", "")
        return SimpleNamespace(**values)

    with pytest.raises(ToolError) as raised:
        run_engine(FunctionalDispatcher(invalid), bars)
    assert raised.value.code == "tool_failed"


@pytest.mark.parametrize("value", ["NaN", "Infinity", 1])
def test_i33_6_invalid_indicators_are_tool_failed(value):
    bars = make_candles([("10", "10")])

    def invalid(name, closed_bars, position):
        return tool_result(Signal.HOLD, closed_bars, indicators={"value": value})

    with pytest.raises(ToolError) as raised:
        run_engine(FunctionalDispatcher(invalid), bars)
    assert raised.value.code == "tool_failed"


def test_i33_6_sell_while_flat_is_not_executed():
    bars = make_candles([("10", "10"), ("11", "11")])

    def invalid(name, closed_bars, position):
        if name == "primary":
            return tool_result(Signal.SELL, closed_bars)
        return tool_result(Signal.HOLD, closed_bars)

    dispatcher = FunctionalDispatcher(invalid)
    with pytest.raises(ToolError) as raised:
        run_engine(dispatcher, bars)
    assert raised.value.code == "tool_failed"
    assert len(dispatcher.calls) == 1


def test_i33_6_buy_while_long_is_not_executed_again():
    bars = make_candles([("10", "10"), ("11", "11"), ("12", "12")])

    def invalid(name, closed_bars, position):
        if name == "primary":
            return tool_result(Signal.BUY, closed_bars)
        return tool_result(Signal.HOLD, closed_bars)

    dispatcher = FunctionalDispatcher(invalid)
    with pytest.raises(ToolError) as raised:
        run_engine(dispatcher, bars)
    assert raised.value.code == "tool_failed"
    assert len([call for call in dispatcher.calls if call[0] == "primary"]) == 2


def test_i33_6_tool_exception_becomes_tool_failed():
    bars = make_candles([("10", "10")])

    def fail(name, closed_bars, position):
        raise RuntimeError("boom")

    with pytest.raises(ToolError) as raised:
        run_engine(FunctionalDispatcher(fail), bars)
    assert raised.value.code == "tool_failed"


def test_i33_4_closed_position_statistics_use_full_costs():
    bars = make_candles(
        [("10", "10"), ("10", "10"), ("12", "12"), ("12", "12"), ("10", "10")]
    )
    schedule = {
        1: Signal.BUY,
        2: Signal.SELL,
        3: Signal.BUY,
        4: Signal.SELL,
        5: Signal.HOLD,
    }

    def scripted(name, closed_bars, position):
        signal = schedule[len(closed_bars)] if name == "primary" else Signal.HOLD
        return tool_result(signal, closed_bars)

    result = run_engine(FunctionalDispatcher(scripted), bars)

    assert [trade.pnl for trade in result.trades] == [
        None,
        Decimal(200),
        None,
        Decimal(-200),
    ]
    assert result.metrics.trades_count == 4
    assert result.metrics.closed_positions_count == 2
    assert result.metrics.win_rate == Decimal("0.5")
    assert result.metrics.avg_position_return == (
        Decimal("0.2") + Decimal(-200) / Decimal(1200)
    ) / 2
    assert result.final_position is Position.FLAT


def test_i33_1_baseline_uses_same_generic_protocol_and_period(
    contract_cases, contract_bars
):
    registry = Registry()
    register_builtin_strategies(registry)
    dispatcher = RegistryDispatcher(registry)
    case = contract_cases["backtests"][0]

    result = run_real_contract(case, contract_cases, contract_bars, dispatcher)

    baseline_calls = [call for call in dispatcher.calls if call[0] == "buy_and_hold"]
    assert [len(call[2]) for call in baseline_calls] == list(range(1, 8))
    assert all(call[1] == "1.0.0" for call in baseline_calls)
    assert result.metrics.buy_and_hold_return == Decimal("-0.2709")
