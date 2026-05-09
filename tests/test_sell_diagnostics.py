import unittest

from binance_ai.config import Settings
from binance_ai.models import Candle, PositionSnapshot, SignalAction, SymbolFilters, TradeSignal
from binance_ai.position_activation import PositionActivationDecision
from binance_ai.risk.engine import RiskEngine


class _Client:
    def quantize_quantity(self, quantity: float, step_size: float) -> float:
        return quantity


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


class SellDecisionDiagnosticTests(unittest.TestCase):
    def test_hold_position_without_exit_has_explicit_no_sell_reason(self) -> None:
        risk = RiskEngine(_settings(), _Client())
        candles = [Candle(i, 100, 101, 99, 100, 1, i + 1) for i in range(20)]
        diagnostic = risk.inspect_sell_decision(
            symbol="XRPJPY",
            price=100.5,
            position=PositionSnapshot(quantity=10.0, average_entry_price=100.0, entry_candle_close_time=10),
            candles=candles,
            current_timestamp_ms=20,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            exit_reason=None,
            activation_decision=PositionActivationDecision("HOLD", "", "grid_hold"),
        )

        self.assertFalse(diagnostic.eligible_to_sell)
        self.assertEqual(diagnostic.blocker, "继续持有")
        self.assertIn("持仓观察中", diagnostic.blocker_details)

    def test_exit_reason_marks_sell_eligible(self) -> None:
        risk = RiskEngine(_settings(), _Client())
        candles = [Candle(i, 100, 101, 99, 100, 1, i + 1) for i in range(60)]
        diagnostic = risk.inspect_sell_decision(
            symbol="XRPJPY",
            price=98.5,
            position=PositionSnapshot(quantity=10.0, average_entry_price=100.0, entry_candle_close_time=10),
            candles=candles,
            current_timestamp_ms=70,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            exit_reason="stop_loss",
        )

        self.assertTrue(diagnostic.eligible_to_sell)
        self.assertEqual(diagnostic.exit_reason, "stop_loss")
        self.assertAlmostEqual(diagnostic.recommended_sell_quantity, 10.0)

    def test_strategy_sell_marks_sell_eligible(self) -> None:
        risk = RiskEngine(_settings(), _Client())
        candles = [Candle(i, 100, 101, 99, 100, 1, i + 1) for i in range(60)]
        diagnostic = risk.inspect_sell_decision(
            symbol="XRPJPY",
            price=100.5,
            position=PositionSnapshot(quantity=10.0, average_entry_price=100.0, entry_candle_close_time=10),
            candles=candles,
            current_timestamp_ms=70,
            signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.8, "bearish_cross"),
            exit_reason=None,
        )

        self.assertTrue(diagnostic.eligible_to_sell)
        self.assertEqual(diagnostic.blocker, "策略 SELL 触发")


if __name__ == "__main__":
    unittest.main()
