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

    def place_market_order(self, order: OrderRequest):
        return {"symbol": order.symbol}


class _LiveClientStub:
    def __init__(self) -> None:
        self.open_payload = {
            "symbol": "XRPJPY",
            "orderId": 123,
            "clientOrderId": "live-buy",
            "status": "NEW",
            "side": "BUY",
            "origQty": "1.0",
            "executedQty": "0.0",
            "price": "200.0",
            "timeInForce": "GTC",
        }
        self.query_payload = dict(self.open_payload)
        self.cancel_payload = {
            **self.open_payload,
            "status": "CANCELED",
        }

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(symbol=symbol, step_size=0.1, min_qty=0.1, min_notional=100.0)

    def place_limit_order(self, order: OrderRequest):
        return {
            **self.open_payload,
            "symbol": order.symbol,
            "side": order.side,
            "origQty": str(order.quantity),
            "price": str(order.limit_price),
            "clientOrderId": order.client_order_id,
        }

    def query_order(self, symbol: str, order_id=None, client_order_id=None):
        return {
            **self.query_payload,
            "symbol": symbol,
            "clientOrderId": client_order_id or self.query_payload["clientOrderId"],
        }

    def cancel_order(self, symbol: str, order_id=None, client_order_id=None):
        return {
            **self.cancel_payload,
            "symbol": symbol,
            "clientOrderId": client_order_id or self.cancel_payload["clientOrderId"],
        }

    def get_open_orders(self, symbol: str | None = None):
        return []


class _UnknownSubmitClientStub(_LiveClientStub):
    def place_limit_order(self, order: OrderRequest):
        raise TimeoutError("gateway timeout")


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

    def test_live_market_order_requires_live_execution_switch(self) -> None:
        settings = Settings(
            api_key="key",
            api_secret="secret",
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
            dry_run=False,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            live_order_execution_enabled=False,
        )
        executor = OrderExecutor(settings, _ClientStub())

        result = executor.execute(
            OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=1.0),
            fill_price=200.0,
            filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
        )

        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(result["reason"], "simulated_execution_requires_paper_portfolio")

    def test_live_disabled_limit_order_uses_paper_lifecycle_when_available(self) -> None:
        settings = Settings(
            api_key="key",
            api_secret="secret",
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
            dry_run=False,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            live_order_execution_enabled=False,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio("JPY", 1000.0, Path(tmpdir) / "paper_state.json")
            executor = OrderExecutor(settings, _LiveClientStub(), portfolio)

            result, event = executor.submit_limit_order(
                OrderRequest(
                    symbol="XRPJPY",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=1.0,
                    limit_price=200.0,
                    client_order_id="sim-buy",
                ),
                current_price=201.0,
                filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                timestamp_ms=1_000,
            )

            self.assertEqual(result["status"], "ORDER_OPEN")
            self.assertEqual(event.reason, "paper_limit_order_open")
            self.assertEqual(len(portfolio.open_orders("XRPJPY")), 1)

    def test_process_open_limit_buy_fills_when_low_touches_limit(self) -> None:
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
            portfolio = PaperPortfolio("JPY", 1000.0, Path(tmpdir) / "paper_state.json")
            executor = OrderExecutor(settings, _ClientStub(), portfolio)
            order = OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="buy-touch",
                expires_at_ms=10_000,
            )
            result, event = executor.submit_limit_order(
                order,
                current_price=201.0,
                filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                timestamp_ms=1_000,
                entry_candle_close_time_ms=1_000,
            )
            self.assertEqual(result["status"], "ORDER_OPEN")
            self.assertEqual(event.status, "OPEN")

            results, events = executor.process_open_orders(
                symbol="XRPJPY",
                candles=[
                    Candle(open_time=1_001, open=201.0, high=202.0, low=199.0, close=200.5, volume=1.0, close_time=2_000)
                ],
                current_price=200.5,
                timestamp_ms=2_000,
            )

            self.assertEqual(results[0]["status"], "PAPER_FILLED")
            self.assertEqual(events[0].status, "FILLED")
            self.assertEqual(portfolio.open_orders(), {})

    def test_process_open_limit_order_expires_and_releases_cash(self) -> None:
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
            portfolio = PaperPortfolio("JPY", 1000.0, Path(tmpdir) / "paper_state.json")
            executor = OrderExecutor(settings, _ClientStub(), portfolio)
            order = OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="buy-expire",
                expires_at_ms=1_500,
            )
            executor.submit_limit_order(
                order,
                current_price=200.0,
                filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                timestamp_ms=1_000,
                entry_candle_close_time_ms=1_000,
            )

            _, events = executor.process_open_orders(
                symbol="XRPJPY",
                candles=[],
                current_price=200.0,
                timestamp_ms=1_600,
            )

            self.assertEqual(events[0].status, "EXPIRED")
            self.assertEqual(portfolio.open_orders(), {})
            self.assertAlmostEqual(portfolio.account_snapshot().balance_of("JPY"), 1000.0)

    def test_existing_open_order_blocks_duplicate_symbol_order(self) -> None:
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
            order_max_open_per_symbol=1,
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio("JPY", 1000.0, Path(tmpdir) / "paper_state.json")
            executor = OrderExecutor(settings, _ClientStub(), portfolio)
            first, _ = executor.submit_limit_order(
                OrderRequest(
                    symbol="XRPJPY",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=1.0,
                    limit_price=200.0,
                    client_order_id="buy-1",
                ),
                current_price=200.0,
                filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                timestamp_ms=1_000,
            )
            second, event = executor.submit_limit_order(
                OrderRequest(
                    symbol="XRPJPY",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=1.0,
                    limit_price=199.0,
                    client_order_id="buy-2",
                ),
                current_price=200.0,
                filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                timestamp_ms=1_100,
            )

            self.assertEqual(first["status"], "ORDER_OPEN")
            self.assertEqual(second["status"], "REJECTED")
            self.assertEqual(event.reason, "max_open_orders_per_symbol_reached")
            self.assertEqual(len(portfolio.open_orders("XRPJPY")), 1)

    def test_live_limit_order_is_tracked_and_synced_by_query(self) -> None:
        settings = Settings(
            api_key="key",
            api_secret="secret",
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
            dry_run=False,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            live_order_execution_enabled=True,
        )
        client = _LiveClientStub()
        executor = OrderExecutor(settings, client)

        result, event = executor.submit_limit_order(
            OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="live-buy",
            ),
            current_price=201.0,
            timestamp_ms=1_000,
        )

        self.assertEqual(result["status"], "ORDER_OPEN")
        self.assertEqual(event.status, "OPEN")
        self.assertEqual(len(executor.open_orders_for_symbol("XRPJPY")), 1)

        _, events = executor.process_open_orders(
            symbol="XRPJPY",
            candles=[],
            current_price=200.2,
            timestamp_ms=2_000,
        )

        self.assertEqual(events[0].event_type, "SYNC")
        self.assertEqual(events[0].status, "OPEN")
        self.assertEqual(len(executor.all_open_orders()), 1)

    def test_live_cancel_removes_tracked_order(self) -> None:
        settings = Settings(
            api_key="key",
            api_secret="secret",
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
            dry_run=False,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            live_order_execution_enabled=True,
        )
        executor = OrderExecutor(settings, _LiveClientStub())
        executor.submit_limit_order(
            OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="live-buy",
            ),
            current_price=201.0,
            timestamp_ms=1_000,
        )

        events = executor.cancel_open_orders_for_symbol(
            symbol="XRPJPY",
            reason="risk_worsened",
            timestamp_ms=2_000,
        )

        self.assertEqual(events[0].status, "CANCELED")
        self.assertEqual(executor.all_open_orders(), [])

    def test_live_open_order_cancels_when_price_deviates(self) -> None:
        settings = Settings(
            api_key="key",
            api_secret="secret",
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
            dry_run=False,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            live_order_execution_enabled=True,
            order_cancel_deviation_pct=0.003,
        )
        executor = OrderExecutor(settings, _LiveClientStub())
        executor.submit_limit_order(
            OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="live-buy",
            ),
            current_price=200.0,
            timestamp_ms=1_000,
        )

        _, events = executor.process_open_orders(
            symbol="XRPJPY",
            candles=[],
            current_price=201.0,
            timestamp_ms=2_000,
        )

        self.assertEqual(events[-1].status, "CANCELED")
        self.assertEqual(events[-1].reason, "order_price_deviation_exceeded")
        self.assertEqual(executor.all_open_orders(), [])

    def test_live_submit_timeout_is_unknown_then_queried_next_cycle(self) -> None:
        settings = Settings(
            api_key="key",
            api_secret="secret",
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
            dry_run=False,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            live_order_execution_enabled=True,
        )
        executor = OrderExecutor(settings, _UnknownSubmitClientStub())

        result, event = executor.submit_limit_order(
            OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="live-buy",
            ),
            current_price=200.0,
            timestamp_ms=1_000,
        )
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(event.status, "UNKNOWN")
        self.assertEqual(executor.all_open_orders()[0].status, "UNKNOWN")

        _, events = executor.process_open_orders(
            symbol="XRPJPY",
            candles=[],
            current_price=200.2,
            timestamp_ms=2_000,
        )

        self.assertEqual(events[0].event_type, "SYNC")
        self.assertEqual(events[0].status, "OPEN")
        self.assertEqual(executor.all_open_orders()[0].status, "OPEN")


if __name__ == "__main__":
    unittest.main()
