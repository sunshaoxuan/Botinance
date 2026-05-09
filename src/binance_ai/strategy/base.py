from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Sequence

from binance_ai.models import Candle, TradeSignal


class Strategy(ABC):
    @abstractmethod
    def generate(self, symbol: str, candles: Sequence[Candle], has_position: bool) -> TradeSignal:
        raise NotImplementedError

