from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from binance_ai.config import Settings
from binance_ai.execution.executor import OrderExecutor
from binance_ai.models import Candle, OrderRequest, SymbolFilters
from binance_ai.paper.portfolio import PaperPortfolio


class _ClientStub:
    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(symbol=symbol, step_size=0.1, min_qty=0.1, min_notional=100.0)


class RangeMarketFillTests(unittest.TestCase):
    def test_tight_limit_order_fills_when_candle_touches_spread(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=180,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=100.0,
            trading_fee_rate=0.001,
            paper_quote_balance=10000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.003,
            take_profit_pct=0.004,
            trailing_stop_pct=0.0025,
            max_hold_bars=30,
            order_passive_offset_pct=0.0001,
        )
        with tempfile.TemporaryDirectory() as tmp:
            portfolio = PaperPortfolio("JPY", 10000.0, Path(tmp) / "paper_state.json")
            executor = OrderExecutor(settings, _ClientStub(), portfolio)
            current_price = 228.0
            spread = 0.0012
            buy_limit = current_price * (1.0 - spread)
            order = OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=10.0,
                limit_price=buy_limit,
                trigger="pair_market_making",
            )
            result, _ = executor.submit_limit_order(
                order,
                current_price=current_price,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                timestamp_ms=1_000_000,
                entry_candle_close_time_ms=1_000_000,
            )
            self.assertEqual(result["status"], "ORDER_OPEN")

            candles = [
                Candle(
                    open_time=1_000_000,
                    open=228.0,
                    high=228.1,
                    low=buy_limit - 0.05,
                    close=228.0,
                    volume=100.0,
                    close_time=1_059_999,
                )
            ]
            fills, events = executor.process_open_orders(
                symbol="XRPJPY",
                candles=candles,
                current_price=228.0,
                timestamp_ms=1_060_000,
            )

            self.assertEqual(len(fills), 1)
            self.assertEqual(fills[0]["status"], "PAPER_FILLED")
            self.assertAlmostEqual(float(fills[0]["fill_price"]), buy_limit)
            self.assertTrue(events)
            self.assertIn(events[0].event_type, {"PAPER_FILLED", "FILLED"})


if __name__ == "__main__":
    unittest.main()
