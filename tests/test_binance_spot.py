import unittest

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import OrderRequest


class _Client(BinanceSpotClient):
    def __init__(self) -> None:
        super().__init__(
            Settings(
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
                trading_fee_rate=0.001,
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
        )
        self.calls = []

    def _signed_request(self, method, path, params=None):
        self.calls.append((method, path, params or {}))
        return {"orderId": 123, "clientOrderId": "boti-test", "status": "NEW"}


class BinanceSpotClientTests(unittest.TestCase):
    def test_place_limit_order_uses_limit_gtc_parameters(self) -> None:
        client = _Client()
        client.place_limit_order(
            OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.2,
                limit_price=222.5,
                time_in_force="GTC",
                client_order_id="boti-test",
            )
        )

        method, path, params = client.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "/api/v3/order")
        self.assertEqual(params["type"], "LIMIT")
        self.assertEqual(params["timeInForce"], "GTC")
        self.assertEqual(params["newClientOrderId"], "boti-test")
        self.assertEqual(params["price"], "222.5")

    def test_query_and_cancel_order_use_client_order_id(self) -> None:
        client = _Client()
        client.query_order("XRPJPY", client_order_id="boti-test")
        client.cancel_order("XRPJPY", client_order_id="boti-test")

        self.assertEqual(client.calls[0][0:2], ("GET", "/api/v3/order"))
        self.assertEqual(client.calls[0][2]["origClientOrderId"], "boti-test")
        self.assertEqual(client.calls[1][0:2], ("DELETE", "/api/v3/order"))
        self.assertEqual(client.calls[1][2]["origClientOrderId"], "boti-test")


if __name__ == "__main__":
    unittest.main()
