import unittest

from binance_ai.config import Settings
from binance_ai.models import AccountSnapshot, PortfolioSnapshot, PositionSnapshot, SymbolFilters
from binance_ai.position_activation import PositionActivationEngine


class _Client:
    def quantize_quantity(self, quantity: float, step_size: float) -> float:
        return int(quantity / step_size) * step_size


def _settings() -> Settings:
    return Settings(
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
        min_order_notional=10.0,
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


class PositionActivationEngineTests(unittest.TestCase):
    def test_profit_grid_sell_uses_25_percent_without_breaking_core(self) -> None:
        engine = PositionActivationEngine(_settings(), _Client())
        snapshot = PortfolioSnapshot(
            quote_asset="JPY",
            quote_balance=100.0,
            initial_quote_balance=1000.0,
            positions={"XRPJPY": PositionSnapshot(quantity=100.0, average_entry_price=100.0, highest_price=100.0)},
        )
        decision = engine.evaluate(
            symbol="XRPJPY",
            price=100.31,
            account=AccountSnapshot({"JPY": 100.0, "XRP": 100.0}),
            filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
            snapshot=snapshot,
            timestamp_ms=1_778_300_000_000,
        )

        self.assertEqual(decision.action, "SELL")
        self.assertEqual(decision.trigger, "grid_profit_sell")
        self.assertAlmostEqual(decision.quantity, 25.0)

    def test_buyback_after_price_retraces_from_last_grid_sell(self) -> None:
        engine = PositionActivationEngine(_settings(), _Client())
        snapshot = PortfolioSnapshot(
            quote_asset="JPY",
            quote_balance=3000.0,
            initial_quote_balance=1000.0,
            positions={"XRPJPY": PositionSnapshot(quantity=75.0, average_entry_price=100.0, highest_price=101.0)},
            activation_state={
                "XRPJPY": {
                    "pending_buyback_quantity": 25.0,
                    "last_grid_sell_price": 101.0,
                    "daily_trade_day": "2026-05-09",
                    "daily_trade_count": 1,
                }
            },
        )
        decision = engine.evaluate(
            symbol="XRPJPY",
            price=100.70,
            account=AccountSnapshot({"JPY": 3000.0, "XRP": 75.0}),
            filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
            snapshot=snapshot,
            timestamp_ms=1_778_300_000_000,
        )

        self.assertEqual(decision.action, "BUY")
        self.assertEqual(decision.trigger, "grid_buyback")
        self.assertAlmostEqual(decision.quantity, 25.0)

    def test_loss_recovery_sell_is_allowed_with_standard_fraction(self) -> None:
        engine = PositionActivationEngine(_settings(), _Client())
        snapshot = PortfolioSnapshot(
            quote_asset="JPY",
            quote_balance=100.0,
            initial_quote_balance=1000.0,
            positions={"XRPJPY": PositionSnapshot(quantity=100.0, average_entry_price=100.0, highest_price=100.0)},
            activation_state={"XRPJPY": {"cost_basis_source": "binance_my_trades_fifo"}},
        )
        decision = engine.evaluate(
            symbol="XRPJPY",
            price=99.69,
            account=AccountSnapshot({"JPY": 100.0, "XRP": 100.0}),
            filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
            snapshot=snapshot,
            timestamp_ms=1_778_300_000_000,
        )

        self.assertEqual(decision.action, "SELL")
        self.assertEqual(decision.trigger, "grid_loss_recovery_sell")
        self.assertAlmostEqual(decision.quantity, 25.0)

    def test_loss_recovery_waits_until_threshold(self) -> None:
        engine = PositionActivationEngine(_settings(), _Client())
        snapshot = PortfolioSnapshot(
            quote_asset="JPY",
            quote_balance=100.0,
            initial_quote_balance=1000.0,
            positions={"XRPJPY": PositionSnapshot(quantity=100.0, average_entry_price=100.0, highest_price=100.0)},
            activation_state={"XRPJPY": {"cost_basis_source": "binance_my_trades_fifo"}},
        )
        decision = engine.evaluate(
            symbol="XRPJPY",
            price=99.90,
            account=AccountSnapshot({"JPY": 100.0, "XRP": 100.0}),
            filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
            snapshot=snapshot,
            timestamp_ms=1_778_300_000_000,
        )

        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.state_update["last_trigger"], "grid_loss_recovery_wait")

    def test_loss_recovery_is_blocked_when_cost_basis_is_seed_price(self) -> None:
        engine = PositionActivationEngine(_settings(), _Client())
        snapshot = PortfolioSnapshot(
            quote_asset="JPY",
            quote_balance=100.0,
            initial_quote_balance=1000.0,
            positions={"XRPJPY": PositionSnapshot(quantity=100.0, average_entry_price=100.0, highest_price=100.0)},
            activation_state={"XRPJPY": {"cost_basis_source": "sync_current_price"}},
        )
        decision = engine.evaluate(
            symbol="XRPJPY",
            price=99.0,
            account=AccountSnapshot({"JPY": 100.0, "XRP": 100.0}),
            filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
            snapshot=snapshot,
            timestamp_ms=1_778_300_000_000,
        )

        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.state_update["last_trigger"], "grid_loss_recovery_blocked")

    def test_daily_trade_limit_blocks_activation(self) -> None:
        engine = PositionActivationEngine(_settings(), _Client())
        snapshot = PortfolioSnapshot(
            quote_asset="JPY",
            quote_balance=100.0,
            initial_quote_balance=1000.0,
            positions={"XRPJPY": PositionSnapshot(quantity=100.0, average_entry_price=100.0, highest_price=100.0)},
            activation_state={"XRPJPY": {"daily_trade_day": "2026-05-09", "daily_trade_count": 8}},
        )
        decision = engine.evaluate(
            symbol="XRPJPY",
            price=101.0,
            account=AccountSnapshot({"JPY": 100.0, "XRP": 100.0}),
            filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
            snapshot=snapshot,
            timestamp_ms=1_778_300_000_000,
        )

        self.assertEqual(decision.action, "HOLD")
        self.assertEqual(decision.reason, "grid_daily_trade_limit_reached")


if __name__ == "__main__":
    unittest.main()
