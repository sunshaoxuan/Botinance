import tempfile
import unittest
from pathlib import Path

from binance_ai.config import Settings
from binance_ai.execution.executor import OrderExecutor
from binance_ai.models import OrderRequest, SymbolFilters
from binance_ai.paper.portfolio import PaperPortfolio


class _ClientStub:
    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(symbol=symbol, step_size=0.1, min_qty=0.1, min_notional=100.0)

    def place_market_order(self, order: OrderRequest):
        return {"symbol": order.symbol}


class OrderExecutorTests(unittest.TestCase):
    def test_execute_blocks_order_below_min_notional_after_quantization(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1h",
            kline_limit=250,
            fast_window=20,
            slow_window=50,
            risk_per_trade=0.10,
            min_order_notional=100.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=Path(tmpdir) / "paper_state.json",
            )
            executor = OrderExecutor(settings, _ClientStub(), portfolio)
            result = executor.execute(
                OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=0.4),
                fill_price=222.66,
                filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
            )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "execution_notional_below_min_notional")


if __name__ == "__main__":
    unittest.main()
