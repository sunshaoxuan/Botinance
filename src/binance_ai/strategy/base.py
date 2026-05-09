from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Mapping, Sequence

from binance_ai.models import Candle, TradeSignal


class Strategy(ABC):
    @abstractmethod
    def generate(
        self,
        symbol: str,
        candles_by_interval: Mapping[str, Sequence[Candle]],
        has_position: bool,
    ) -> TradeSignal:
        raise NotImplementedError
