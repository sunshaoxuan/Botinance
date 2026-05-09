import unittest

from binance_ai.models import Candle, SignalAction
from binance_ai.strategy.momentum import MovingAverageMomentumStrategy


def make_candle(close: float, index: int) -> Candle:
    return Candle(
        open_time=index,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        close_time=index + 1,
    )


class MovingAverageMomentumStrategyTests(unittest.TestCase):
    def test_buy_signal_on_bullish_cross_without_position(self) -> None:
        strategy = MovingAverageMomentumStrategy(fast_window=3, slow_window=5)
        candles = [make_candle(value, idx) for idx, value in enumerate([10, 9, 8, 7, 6, 15])]
        signal = strategy.generate("BTCUSDT", candles, has_position=False)
        self.assertEqual(signal.action, SignalAction.BUY)

    def test_sell_signal_on_bearish_cross_with_position(self) -> None:
        strategy = MovingAverageMomentumStrategy(fast_window=3, slow_window=5)
        candles = [make_candle(value, idx) for idx, value in enumerate([6, 7, 8, 9, 10, 1])]
        signal = strategy.generate("BTCUSDT", candles, has_position=True)
        self.assertEqual(signal.action, SignalAction.SELL)


if __name__ == "__main__":
    unittest.main()
