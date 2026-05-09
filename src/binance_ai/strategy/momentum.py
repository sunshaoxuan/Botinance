from __future__ import annotations

from statistics import mean
from typing import Sequence

from binance_ai.models import Candle, SignalAction, TradeSignal
from binance_ai.strategy.base import Strategy


class MovingAverageMomentumStrategy(Strategy):
    def __init__(self, fast_window: int, slow_window: int) -> None:
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        self.fast_window = fast_window
        self.slow_window = slow_window

    def generate(self, symbol: str, candles: Sequence[Candle], has_position: bool) -> TradeSignal:
        needed = self.slow_window + 1
        if len(candles) < needed:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.HOLD,
                confidence=0.0,
                reason=f"insufficient_candles:{len(candles)}/{needed}",
            )

        closes = [candle.close for candle in candles]
        fast_prev = mean(closes[-self.fast_window - 1 : -1])
        fast_now = mean(closes[-self.fast_window :])
        slow_prev = mean(closes[-self.slow_window - 1 : -1])
        slow_now = mean(closes[-self.slow_window :])

        bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
        bearish_cross = fast_prev >= slow_prev and fast_now < slow_now
        distance = abs(fast_now - slow_now) / slow_now if slow_now else 0.0
        confidence = min(distance * 100, 1.0)

        if bullish_cross and not has_position:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.BUY,
                confidence=confidence,
                reason=f"bullish_cross fast={fast_now:.4f} slow={slow_now:.4f}",
            )
        if bearish_cross and has_position:
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.SELL,
                confidence=confidence,
                reason=f"bearish_cross fast={fast_now:.4f} slow={slow_now:.4f}",
            )
        return TradeSignal(
            symbol=symbol,
            action=SignalAction.HOLD,
            confidence=confidence,
            reason=f"no_cross fast={fast_now:.4f} slow={slow_now:.4f}",
        )

