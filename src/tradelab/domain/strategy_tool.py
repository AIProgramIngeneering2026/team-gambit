from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol, TypeAlias

ParamValue: TypeAlias = (
    str
    | int
    | float
    | bool
    | None
    | Mapping[str, "ParamValue"]
    | list["ParamValue"]
    | tuple["ParamValue", ...]
)


def _freeze(value: ParamValue) -> ParamValue:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True, init=False, eq=False)
class NormalizedParams(Mapping[str, ParamValue]):
    """Проверенные параметры Tool с неизменяемыми вложенными объектами и массивами."""

    _values: Mapping[str, ParamValue]

    def __init__(self, values: Mapping[str, ParamValue]) -> None:
        frozen = MappingProxyType(
            {key: _freeze(value) for key, value in values.items()}
        )
        object.__setattr__(self, "_values", frozen)

    def __getitem__(self, key: str) -> ParamValue:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class Position(StrEnum):
    FLAT = "flat"
    LONG = "long"


class Signal(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True, slots=True)
class Candle:
    trading_date: date
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class SignalReason:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ToolResult:
    signal: Signal
    bar_time: datetime | None
    reason: SignalReason
    indicators: Mapping[str, str | None]

    def __post_init__(self) -> None:
        object.__setattr__(self, "indicators", MappingProxyType(dict(self.indicators)))


class ToolError(Exception):
    def __init__(
        self, code: str, message: str, field: str, reason: str, **details: ParamValue
    ) -> None:
        self.code = code
        self.message = message
        self.details = NormalizedParams({"field": field, "reason": reason, **details})
        super().__init__(message)


class StrategyTool(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def title(self) -> str: ...

    @property
    def description(self) -> str: ...

    def params_schema(self) -> Mapping[str, ParamValue]: ...

    def validate_params(self, params: object) -> NormalizedParams: ...

    def required_bars(self, normalized_params: NormalizedParams) -> int: ...

    def execute(
        self,
        closed_bars: Sequence[Candle],
        position: Position,
        normalized_params: NormalizedParams,
    ) -> ToolResult: ...
