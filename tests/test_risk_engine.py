import unittest

from binance_ai.config import Settings
from binance_ai.models import AccountSnapshot, SymbolFilters
from binance_ai.risk.engine import RiskEngine


class _ClientStub:
    @staticmethod
    def quantize_quantity(quantity: float, step_size: float) -> float:
        steps = int(quantity / step_size) if step_size else quantity
        return round(steps * step_size, 8) if step_size else quantity


class RiskEngineTests(unittest.TestCase):
    def test_inspect_buy_decision_reports_non_buy_signal_blocker(self) -> None:
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
        risk = RiskEngine(settings, _ClientStub())
        diagnostic = risk.inspect_buy_decision(
            symbol="XRPJPY",
            price=200.0,
            account=AccountSnapshot(balances={"JPY": 1000.0}),
            filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
            signal_action="HOLD",
            signal_reason="no_cross",
            has_position=False,
        )
        self.assertFalse(diagnostic.eligible_to_buy)
        self.assertIn("当前策略信号不是买入", diagnostic.blocker_details)

    def test_build_buy_order_blocks_when_quantized_notional_falls_below_minimum(self) -> None:
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
        risk = RiskEngine(settings, _ClientStub())
        decision = risk.build_buy_order(
            symbol="XRPJPY",
            price=222.66,
            account=AccountSnapshot(balances={"JPY": 1000.0}),
            filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
        )
        self.assertFalse(decision.approved)
        self.assertIn("final_notional_below_min_notional", decision.reason)

    def test_inspect_buy_decision_reports_final_notional_failure(self) -> None:
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
        risk = RiskEngine(settings, _ClientStub())
        diagnostic = risk.inspect_buy_decision(
            symbol="XRPJPY",
            price=222.66,
            account=AccountSnapshot(balances={"JPY": 1000.0}),
            filters=SymbolFilters(symbol="XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
            signal_action="BUY",
            signal_reason="bullish_cross",
            has_position=False,
        )
        self.assertFalse(diagnostic.min_notional_passed)
        self.assertAlmostEqual(diagnostic.final_notional, 89.064)
        self.assertIn("按步进取整后的最终成交额低于最小成交额", diagnostic.blocker_details)

    def test_determine_exit_reason_take_profit(self) -> None:
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
        risk = RiskEngine(settings, _ClientStub())
        reason = risk.determine_exit_reason(
            price=204.1,
            position=__import__("binance_ai.models", fromlist=["PositionSnapshot"]).PositionSnapshot(
                quantity=1.0,
                average_entry_price=200.0,
                opened_at_ms=1,
                entry_candle_close_time=1,
                highest_price=204.1,
            ),
            candles=[],
            current_timestamp_ms=100,
        )
        self.assertEqual(reason, "take_profit")

    def test_determine_exit_reason_stop_loss(self) -> None:
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
        risk = RiskEngine(settings, _ClientStub())
        reason = risk.determine_exit_reason(
            price=197.9,
            position=__import__("binance_ai.models", fromlist=["PositionSnapshot"]).PositionSnapshot(
                quantity=1.0,
                average_entry_price=200.0,
                opened_at_ms=1,
                entry_candle_close_time=1,
                highest_price=201.0,
            ),
            candles=[],
            current_timestamp_ms=100,
        )
        self.assertEqual(reason, "stop_loss")

    def test_determine_exit_reason_trailing_stop(self) -> None:
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
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.02,
            take_profit_pct=0.05,
            trailing_stop_pct=0.01,
            max_hold_bars=24,
        )
        risk = RiskEngine(settings, _ClientStub())
        reason = risk.determine_exit_reason(
            price=201.9,
            position=__import__("binance_ai.models", fromlist=["PositionSnapshot"]).PositionSnapshot(
                quantity=1.0,
                average_entry_price=200.0,
                opened_at_ms=1,
                entry_candle_close_time=10,
                highest_price=205.0,
            ),
            candles=[],
            current_timestamp_ms=100,
        )
        self.assertEqual(reason, "trailing_stop")

    def test_determine_exit_reason_max_hold(self) -> None:
        from binance_ai.models import Candle, PositionSnapshot

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
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.02,
            take_profit_pct=0.05,
            trailing_stop_pct=0.01,
            max_hold_bars=2,
        )
        risk = RiskEngine(settings, _ClientStub())
        candles = [
            Candle(open_time=0, open=200.0, high=201.0, low=199.0, close=200.0, volume=1.0, close_time=10),
            Candle(open_time=10, open=200.0, high=201.0, low=199.0, close=200.2, volume=1.0, close_time=20),
            Candle(open_time=20, open=200.2, high=201.0, low=199.0, close=200.4, volume=1.0, close_time=30),
        ]
        reason = risk.determine_exit_reason(
            price=200.4,
            position=PositionSnapshot(
                quantity=1.0,
                average_entry_price=200.0,
                opened_at_ms=1,
                entry_candle_close_time=10,
                highest_price=201.0,
            ),
            candles=candles,
            current_timestamp_ms=30,
        )
        self.assertEqual(reason, "max_hold_exit")

    def test_build_position_diagnostic_contains_exit_lines(self) -> None:
        from binance_ai.models import Candle, PositionSnapshot

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
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.01,
            max_hold_bars=24,
        )
        risk = RiskEngine(settings, _ClientStub())
        candles = [
            Candle(open_time=0, open=200.0, high=201.0, low=199.0, close=200.0, volume=1.0, close_time=10),
            Candle(open_time=10, open=200.0, high=202.0, low=199.0, close=201.0, volume=1.0, close_time=20),
        ]
        diagnostic = risk.build_position_diagnostic(
            symbol="XRPJPY",
            price=201.5,
            position=PositionSnapshot(
                quantity=0.4,
                average_entry_price=200.0,
                opened_at_ms=1,
                entry_candle_close_time=10,
                highest_price=202.0,
            ),
            candles=candles,
            current_timestamp_ms=20,
        )
        self.assertAlmostEqual(diagnostic.stop_loss_price, 198.0)
        self.assertAlmostEqual(diagnostic.take_profit_price, 204.0)
        self.assertAlmostEqual(diagnostic.trailing_stop_price, 199.98)
        self.assertEqual(diagnostic.bars_held, 1)


if __name__ == "__main__":
    unittest.main()
