from collections.abc import Iterable
from typing import Protocol


class InstrumentCatalog(Protocol):
    def contains(self, ticker: str) -> bool: ...


class InMemoryInstrumentCatalog:
    def __init__(self, tickers: Iterable[str]) -> None:
        self._tickers = frozenset(ticker.upper() for ticker in tickers)

    def contains(self, ticker: str) -> bool:
        return ticker.upper() in self._tickers
