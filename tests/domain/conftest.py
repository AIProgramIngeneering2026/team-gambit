import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tradelab.domain.strategy_tool import Candle


@pytest.fixture
def contract_cases():
    path = Path(__file__).parents[2] / "docs" / "examples" / "strategy-tool-cases.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def contract_bars(contract_cases):
    return tuple(
        Candle(
            trading_date=date.fromisoformat(item["trading_date"]),
            open_time=datetime.fromisoformat(item["open_time"]),
            close_time=datetime.fromisoformat(item["close_time"]),
            open=Decimal(item["open"]),
            high=Decimal(item["high"]),
            low=Decimal(item["low"]),
            close=Decimal(item["close"]),
            volume=Decimal(item["volume"]),
        )
        for item in contract_cases["bars"]
    )
