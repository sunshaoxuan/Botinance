from __future__ import annotations

from typing import List

from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import Candle


class MarketDataService:
    def __init__(self, client: BinanceSpotClient) -> None:
        self.client = client

    def recent_candles(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        return self.client.get_klines(symbol=symbol, interval=interval, limit=limit)

