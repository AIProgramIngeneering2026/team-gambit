import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from uuid import uuid4

from .backtest import BacktestEngine
from .backtest_types import BacktestPeriod, StrategySpec
from .instrument_catalog import InstrumentCatalog
from .job import BacktestJobConfig, Job
from .job_lifecycle import JobLifecycle
from .job_repository import JobRepository
from .registry import Registry
from .strategy_tool import ParamValue, ToolError

_DECIMAL_PATTERN = re.compile(r"-?(0|[1-9][0-9]{0,17})(\.[0-9]{1,10})?")
_TICKER_PATTERN = re.compile(r"[A-Za-z0-9]{1,12}")
_STRATEGY_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CAPITAL_CONTEXT = Context(prec=28, rounding=ROUND_HALF_UP)
_MONEY_QUANTUM = Decimal("0.01")
_DEFAULT_PARAMS = object()
_MARKET_TIMEZONE = timezone(timedelta(hours=3), name="Europe/Moscow")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_job_id() -> str:
    return str(uuid4())


def _invalid_params(
    message: str, field: str, reason: str, **details: ParamValue
) -> ToolError:
    return ToolError("invalid_params", message, field, reason, **details)


def _decimal_field(
    value: object,
    field: str,
    *,
    minimum: Decimal,
    maximum: Decimal,
) -> Decimal:
    if not isinstance(value, str):
        raise _invalid_params(
            f"Поле {field} должно быть десятичной строкой", field, "wrong_type"
        )
    if _DECIMAL_PATTERN.fullmatch(value) is None:
        raise _invalid_params(
            f"Поле {field} должно использовать обычную десятичную запись",
            field,
            "invalid_format",
        )
    parsed = Decimal(value)
    if not minimum <= parsed <= maximum:
        raise _invalid_params(
            f"Поле {field} должно быть от {minimum} до {maximum}",
            field,
            "out_of_range",
            allowed={"min": str(minimum), "max": str(maximum)},
        )
    return parsed


class JobService:
    def __init__(
        self,
        *,
        repository: JobRepository,
        registry: Registry,
        instruments: InstrumentCatalog,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_job_id,
        engine_version: str = BacktestEngine.version,
    ) -> None:
        self._registry = registry
        self._instruments = instruments
        self._clock = clock
        self._id_factory = id_factory
        self._engine_version = engine_version
        self._lifecycle = JobLifecycle(repository)

    def create_backtest(
        self,
        *,
        ticker: str,
        strategy: str,
        period: BacktestPeriod,
        params: object = _DEFAULT_PARAMS,
        initial_capital: str = "100000.00",
        commission_rate: str = "0.0005",
    ) -> Job:
        now = self._clock()
        normalized_ticker = self._validate_ticker(ticker)
        self._validate_period(period, now.astimezone(_MARKET_TIMEZONE).date())
        capital = _decimal_field(
            initial_capital,
            "initial_capital",
            minimum=Decimal("1000.00"),
            maximum=Decimal("1000000000.00"),
        )
        commission = _decimal_field(
            commission_rate,
            "commission_rate",
            minimum=Decimal("0"),
            maximum=Decimal("0.05"),
        )
        self._validate_strategy_name(strategy)

        try:
            tool = self._registry.get(strategy)
        except ToolError as error:
            if error.code != "unknown_operation":
                raise
            raise ToolError(
                "unknown_operation",
                error.message,
                "strategy",
                "not_found",
                allowed=error.details["allowed"],
            ) from error
        normalized_params = tool.validate_params(
            {} if params is _DEFAULT_PARAMS else params
        )
        baseline_tool = self._registry.get("buy_and_hold")
        baseline_params = baseline_tool.validate_params({})
        with localcontext(_CAPITAL_CONTEXT):
            capital = capital.quantize(_MONEY_QUANTUM)

        config = BacktestJobConfig(
            ticker=normalized_ticker,
            period=period,
            strategy=StrategySpec(tool.name, tool.version, normalized_params),
            baseline=StrategySpec(
                baseline_tool.name, baseline_tool.version, baseline_params
            ),
            initial_capital=capital,
            commission_rate=commission,
            engine_version=self._engine_version,
        )
        job = Job(self._id_factory(), now, config=config)
        return self._lifecycle.create_and_enqueue(job, now)

    def _validate_ticker(self, ticker: object) -> str:
        if not isinstance(ticker, str):
            raise _invalid_params("Тикер должен быть строкой", "ticker", "wrong_type")
        if _TICKER_PATTERN.fullmatch(ticker) is None:
            raise _invalid_params(
                "Тикер должен содержать от 1 до 12 латинских букв или цифр",
                "ticker",
                "invalid_format",
            )
        normalized = ticker.upper()
        if not self._instruments.contains(normalized):
            raise _invalid_params(
                f"Инструмент {normalized} не поддерживается",
                "ticker",
                "unknown_ticker",
            )
        return normalized

    @staticmethod
    def _validate_strategy_name(strategy: object) -> None:
        if not isinstance(strategy, str):
            raise _invalid_params(
                "Имя стратегии должно быть строкой", "strategy", "wrong_type"
            )
        if _STRATEGY_PATTERN.fullmatch(strategy) is None:
            raise _invalid_params(
                "Неверный формат имени стратегии", "strategy", "invalid_format"
            )

    @staticmethod
    def _validate_period(period: object, today: date) -> None:
        if not isinstance(period, BacktestPeriod):
            raise _invalid_params("Период задан неверно", "period", "wrong_type")
        for field, value in (("start", period.start), ("end", period.end)):
            if type(value) is not date:
                raise _invalid_params(
                    f"Граница {field} должна быть датой",
                    f"period.{field}",
                    "wrong_type",
                )
        if period.start >= period.end:
            raise _invalid_params(
                "Дата начала должна быть раньше даты окончания",
                "period.start",
                "must_be_before_end",
            )
        if period.end > today:
            raise _invalid_params(
                "Дата окончания не может быть в будущем", "period.end", "in_future"
            )
        if (period.end - period.start).days >= 3653:
            raise _invalid_params(
                "Период не может быть длиннее 3653 дней",
                "period",
                "too_long",
                allowed={"max": 3653},
            )
