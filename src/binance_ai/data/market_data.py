from __future__ import annotations

from typing import Dict, List, Sequence

from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import Candle


class MarketDataService:
    def __init__(self, client: BinanceSpotClient) -> None:
        self.client = client

    def recent_candles(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        return self.client.get_klines(symbol=symbol, interval=interval, limit=limit)

    def recent_candles_by_interval(
        self,
        symbol: str,
        intervals: Sequence[str],
        limit: int,
    ) -> Dict[str, List[Candle]]:
        unique_intervals = []
        seen = set()
        for interval in intervals:
            normalized = interval.strip()
            if not normalized or normalized in seen:
                continue
            unique_intervals.append(normalized)
            seen.add(normalized)
        return {
            interval: self.client.get_klines(symbol=symbol, interval=interval, limit=limit)
            for interval in unique_intervals
        }
