from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from binance_ai.backtest.data_loader import HistoricalDatasetLoader
from binance_ai.backtest.models import BacktestConfig, BacktestDataset, BacktestManifest
from binance_ai.backtest.reporter import BacktestReporter
from binance_ai.backtest.runner import BacktestRunner
from binance_ai.models import Candle, SignalAction, SymbolFilters, TradeSignal
from binance_ai.risk.engine import RiskEngine
from binance_ai.strategy.base import Strategy


def make_candle(close: float, index: int, step_ms: int = 3_600_000) -> Candle:
    open_time = index * step_ms
    return Candle(
        open_time=open_time,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=1.0,
        close_time=open_time + step_ms - 1,
    )


class _ClientStub:
    def __init__(self, klines=None) -> None:
        self.klines = klines or {}
        self.range_calls = 0

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(symbol=symbol, step_size=0.1, min_qty=0.1, min_notional=10.0)

    def quantize_quantity(self, quantity: float, step_size: float) -> float:
        return max(0.0, round((int(quantity / step_size)) * step_size, 4))

    def get_klines_range(self, symbol: str, interval: str, start_time_ms: int, end_time_ms: int | None = None, limit: int = 1000):
        self.range_calls += 1
        candles = list(self.klines.get(interval, []))
        rows = []
        for candle in candles:
            if candle.open_time < start_time_ms:
                continue
            if end_time_ms is not None and candle.open_time >= end_time_ms:
                continue
            rows.append(candle)
        return rows[:limit]

    def close(self) -> None:
        return None


class _BuyThenHoldStrategy(Strategy):
    def generate(self, symbol: str, candles_by_interval, has_position: bool) -> TradeSignal:
        if has_position:
            return TradeSignal(symbol=symbol, action=SignalAction.HOLD, confidence=0.4, reason="hold_position")
        return TradeSignal(symbol=symbol, action=SignalAction.BUY, confidence=0.8, reason="enter_now")


class _AlwaysHoldStrategy(Strategy):
    def generate(self, symbol: str, candles_by_interval, has_position: bool) -> TradeSignal:
        return TradeSignal(symbol=symbol, action=SignalAction.HOLD, confidence=0.1, reason="wait")


class _LoaderStub:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.requests = []

    def load(self, symbol: str, intervals, date_from: str, date_to: str) -> BacktestDataset:
        self.requests.append((symbol, tuple(intervals), date_from, date_to))
        day_count = (int(date_to[-2:]) - int(date_from[-2:]) + 1) if date_from[:8] == date_to[:8] else 150
        candles = [make_candle(100.0 + (idx % 5), idx) for idx in range(max(160, day_count * 24))]
        return BacktestDataset(
            candles_by_interval={interval: list(candles) for interval in intervals},
            infos=[],
        )


class BacktestTests(unittest.TestCase):
    def build_config(self) -> BacktestConfig:
        return BacktestConfig(
            symbol="XRPJPY",
            quote_asset="JPY",
            date_from="2026-01-01",
            date_to="2026-05-01",
            output_dir="runtime_backtest",
            initial_quote_balance=1000.0,
            main_interval="1h",
            entry_interval="15m",
            trend_interval="4h",
            fast_window=1,
            slow_window=2,
            entry_fast_window=1,
            entry_slow_window=2,
            trend_fast_window=1,
            trend_slow_window=1,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            stop_loss_pct=0.05,
            take_profit_pct=0.02,
            trailing_stop_pct=0.50,
            max_hold_bars=50,
            walk_forward=False,
        )

    def build_settings_like(self):
        class _Settings:
            quote_asset = "JPY"
            risk_per_trade = 0.10
            min_order_notional = 10.0
            stop_loss_pct = 0.05
            take_profit_pct = 0.02
            trailing_stop_pct = 0.50
            max_hold_bars = 50
            trading_symbols = ["XRPJPY"]
            max_active_symbols = 3

            @property
            def active_symbol_limit(self):
                return 3

        return _Settings()

    def test_loader_uses_cache_on_second_read(self) -> None:
        start_ms = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        candles = [
            Candle(
                open_time=start_ms + idx * 3_600_000,
                open=100.0 + idx,
                high=100.0 + idx,
                low=100.0 + idx,
                close=100.0 + idx,
                volume=1.0,
                close_time=start_ms + (idx + 1) * 3_600_000 - 1,
            )
            for idx in range(5)
        ]
        client = _ClientStub(klines={"1h": candles})
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = HistoricalDatasetLoader(client, Path(tmpdir))
            first = loader.load("XRPJPY", ["1h"], "2026-01-01", "2026-01-02")
            second = loader.load("XRPJPY", ["1h"], "2026-01-01", "2026-01-02")
        self.assertEqual(client.range_calls, 1)
        self.assertFalse(first.infos[0].cache_hit)
        self.assertTrue(second.infos[0].cache_hit)

    def test_runner_generates_trade_and_metrics(self) -> None:
        candles = [make_candle(value, idx) for idx, value in enumerate([100.0, 101.0, 103.0], start=1)]
        dataset = BacktestDataset(
            candles_by_interval={
                "1h": candles,
                "15m": list(candles),
                "4h": list(candles),
            },
            infos=[],
        )
        config = self.build_config()
        runner = BacktestRunner(
            config=config,
            client=_ClientStub(),
            strategy=_BuyThenHoldStrategy(),
            risk=RiskEngine(self.build_settings_like(), _ClientStub()),
        )
        result = runner.run(dataset)
        self.assertEqual(result.summary.trade_count, 1)
        self.assertEqual(result.summary.completed_trade_count, 1)
        self.assertAlmostEqual(result.trades[0].realized_pnl, 3.0)
        self.assertAlmostEqual(result.trades[0].mfe_pct, 3.0)
        self.assertAlmostEqual(result.trades[0].mae_pct, 0.0)
        self.assertGreater(result.summary.total_return_pct, 0.0)

    def test_runner_returns_zero_metrics_when_no_trades(self) -> None:
        candles = [make_candle(100.0 + idx, idx) for idx in range(1, 6)]
        dataset = BacktestDataset(
            candles_by_interval={"1h": candles, "15m": list(candles), "4h": list(candles)},
            infos=[],
        )
        runner = BacktestRunner(
            config=self.build_config(),
            client=_ClientStub(),
            strategy=_AlwaysHoldStrategy(),
            risk=RiskEngine(self.build_settings_like(), _ClientStub()),
        )
        result = runner.run(dataset)
        self.assertEqual(result.summary.trade_count, 0)
        self.assertEqual(result.summary.completed_trade_count, 0)
        self.assertEqual(result.summary.win_rate, 0.0)
        self.assertEqual(result.summary.avg_win_loss_ratio, 0.0)

    def test_runner_raises_on_insufficient_data(self) -> None:
        dataset = BacktestDataset(
            candles_by_interval={"1h": [make_candle(100.0, 1)], "15m": [make_candle(100.0, 1)], "4h": [make_candle(100.0, 1)]},
            infos=[],
        )
        runner = BacktestRunner(
            config=self.build_config(),
            client=_ClientStub(),
            strategy=_AlwaysHoldStrategy(),
            risk=RiskEngine(self.build_settings_like(), _ClientStub()),
        )
        with self.assertRaises(ValueError):
            runner.run(dataset)

    def test_walk_forward_segment_boundaries_and_report_output(self) -> None:
        config = self.build_config()
        config = BacktestConfig(**{**config.__dict__, "date_to": "2026-06-30", "walk_forward": True})
        runner = BacktestRunner(
            config=config,
            client=_ClientStub(),
            strategy=_AlwaysHoldStrategy(),
            risk=RiskEngine(self.build_settings_like(), _ClientStub()),
        )
        loader = _LoaderStub(config)
        baseline, segment_runs, segments = runner.run_walk_forward(loader)
        self.assertEqual(len(segments), 3)
        self.assertEqual(segments[0].train_from, "2026-01-01")
        self.assertEqual(segments[0].test_from, "2026-04-01")
        self.assertEqual(segments[0].test_to, "2026-04-30")
        self.assertEqual(segments[1].train_from, "2026-01-31")
        self.assertEqual(segments[1].test_from, "2026-05-01")
        self.assertEqual(segments[2].test_to, "2026-06-29")
        with tempfile.TemporaryDirectory() as tmpdir:
            reporter = BacktestReporter(Path(tmpdir))
            manifest = BacktestManifest(
                config=config,
                dataset_infos=[],
                walk_forward=True,
                segment_count=len(segments),
                baseline_summary=baseline.summary,
                notes=[],
            )
            reporter.write_walk_forward(baseline, segment_runs, segments, manifest)
            self.assertTrue((Path(tmpdir) / "summary.json").exists())
            self.assertTrue((Path(tmpdir) / "trades.csv").exists())
            self.assertTrue((Path(tmpdir) / "equity_curve.csv").exists())
            self.assertTrue((Path(tmpdir) / "segments.json").exists())
            self.assertTrue((Path(tmpdir) / "run_manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
