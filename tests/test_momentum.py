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
    def build_strategy(self) -> MovingAverageMomentumStrategy:
        return MovingAverageMomentumStrategy(
            main_interval="1h",
            fast_window=3,
            slow_window=5,
            entry_interval="15m",
            entry_fast_window=3,
            entry_slow_window=5,
            trend_interval="4h",
            trend_fast_window=3,
            trend_slow_window=5,
        )

    def test_buy_signal_when_all_timeframes_align(self) -> None:
        strategy = self.build_strategy()
        candles_by_interval = {
            "1h": [make_candle(value, idx) for idx, value in enumerate([10, 10, 10, 10, 8, 14])],
            "15m": [make_candle(value, idx) for idx, value in enumerate([8, 8, 8, 8, 7, 12])],
            "4h": [make_candle(value, idx) for idx, value in enumerate([8, 9, 10, 11, 12])],
        }
        signal = strategy.generate("BTCUSDT", candles_by_interval, has_position=False)
        self.assertEqual(signal.action, SignalAction.BUY)

    def test_buy_filtered_when_trend_is_down(self) -> None:
        strategy = self.build_strategy()
        candles_by_interval = {
            "1h": [make_candle(value, idx) for idx, value in enumerate([10, 9, 8, 7, 6, 15])],
            "15m": [make_candle(value, idx) for idx, value in enumerate([6, 7, 8, 9, 10])],
            "4h": [make_candle(value, idx) for idx, value in enumerate([10, 9, 8, 7, 6])],
        }
        signal = strategy.generate("BTCUSDT", candles_by_interval, has_position=False)
        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("buy_filtered", signal.reason)

    def test_sell_signal_when_position_and_entry_momentum_weakens(self) -> None:
        strategy = self.build_strategy()
        candles_by_interval = {
            "1h": [make_candle(value, idx) for idx, value in enumerate([15, 15, 15, 15, 17, 9])],
            "15m": [make_candle(value, idx) for idx, value in enumerate([10, 9, 8, 7, 6])],
            "4h": [make_candle(value, idx) for idx, value in enumerate([10, 9, 8, 7, 6])],
        }
        signal = strategy.generate("BTCUSDT", candles_by_interval, has_position=True)
        self.assertEqual(signal.action, SignalAction.SELL)

    def test_main_interval_uses_configured_timeframe_instead_of_fixed_1h(self) -> None:
        strategy = MovingAverageMomentumStrategy(
            main_interval="30m",
            fast_window=3,
            slow_window=5,
            entry_interval="15m",
            entry_fast_window=3,
            entry_slow_window=5,
            trend_interval="4h",
            trend_fast_window=3,
            trend_slow_window=5,
        )
        candles_by_interval = {
            "30m": [make_candle(value, idx) for idx, value in enumerate([10, 10, 10, 10, 8, 14])],
            "1h": [make_candle(value, idx) for idx, value in enumerate([14, 14, 14, 14, 16, 9])],
            "15m": [make_candle(value, idx) for idx, value in enumerate([8, 8, 8, 8, 7, 12])],
            "4h": [make_candle(value, idx) for idx, value in enumerate([8, 9, 10, 11, 12])],
        }
        signal = strategy.generate("BTCUSDT", candles_by_interval, has_position=False)
        self.assertEqual(signal.action, SignalAction.BUY)
        self.assertIn("30m=bullish_cross", signal.reason)


if __name__ == "__main__":
    unittest.main()
