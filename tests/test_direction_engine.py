import unittest

from binance_ai.config import Settings
from binance_ai.direction import DirectionDecisionEngine
from binance_ai.models import AccountSnapshot, AiRiskAssessment, Candle, SignalAction, TradeSignal
from binance_ai.target_inventory import TargetInventoryEngine


def _settings(**updates):
    base = Settings(
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
        min_order_notional=50.0,
        trading_fee_rate=0.001,
        paper_quote_balance=10000.0,
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
        min_effective_order_notional=500.0,
        order_target_notional=2000.0,
        direction_engine_enabled=True,
        legacy_direct_order_fallback=False,
    )
    if not updates:
        return base
    from dataclasses import replace

    return replace(base, **updates)


def _flat_candles(price=100.0, count=80):
    return [
        Candle(
            open_time=index * 60_000,
            open=price,
            high=price * 1.001,
            low=price * 0.999,
            close=price,
            volume=1000.0,
            close_time=(index + 1) * 60_000 - 1,
        )
        for index in range(count)
    ]


class DirectionDecisionEngineTests(unittest.TestCase):
    def test_price_above_buy_zone_rejects_chase_buy(self):
        settings = _settings()
        candles = _flat_candles(100.0)
        target = TargetInventoryEngine(settings).evaluate(
            symbol="XRPJPY",
            price=101.0,
            account=AccountSnapshot({"JPY": 10000.0, "XRP": 1.0}),
            base_balance=1.0,
            signal=TradeSignal("XRPJPY", SignalAction.BUY, 0.9, "up"),
            candles=candles,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            daily_risk_state={},
        )
        decision = DirectionDecisionEngine(settings).evaluate(
            symbol="XRPJPY",
            price=101.0,
            candles=candles,
            signal=TradeSignal("XRPJPY", SignalAction.BUY, 0.9, "up"),
            target_inventory=target,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            open_orders=[],
            exit_reason=None,
        )

        self.assertFalse(decision.allow_buy)
        self.assertIn("拒绝追涨买入", decision.reason_cn)

    def test_price_below_sell_zone_rejects_panic_sell(self):
        settings = _settings()
        candles = _flat_candles(100.0)
        target = TargetInventoryEngine(settings).evaluate(
            symbol="XRPJPY",
            price=99.0,
            account=AccountSnapshot({"JPY": 1000.0, "XRP": 100.0}),
            base_balance=100.0,
            signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "down"),
            candles=candles,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            daily_risk_state={},
        )
        decision = DirectionDecisionEngine(settings).evaluate(
            symbol="XRPJPY",
            price=99.0,
            candles=candles,
            signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "down"),
            target_inventory=target,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            open_orders=[],
            exit_reason=None,
        )

        self.assertFalse(decision.allow_sell)
        self.assertIn("拒绝杀跌卖出", decision.reason_cn)

    def test_discount_price_allows_buy_when_inventory_low(self):
        settings = _settings()
        candles = _flat_candles(100.0)
        target = TargetInventoryEngine(settings).evaluate(
            symbol="XRPJPY",
            price=99.0,
            account=AccountSnapshot({"JPY": 10000.0, "XRP": 1.0}),
            base_balance=1.0,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            candles=candles,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            daily_risk_state={},
        )
        decision = DirectionDecisionEngine(settings).evaluate(
            symbol="XRPJPY",
            price=99.0,
            candles=candles,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            target_inventory=target,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            open_orders=[],
            exit_reason=None,
        )

        self.assertEqual(decision.price_zone, "BUY_ZONE")
        self.assertTrue(decision.allow_buy)

    def test_risk_exit_can_sell_below_sell_zone(self):
        settings = _settings()
        candles = _flat_candles(100.0)
        target = TargetInventoryEngine(settings).evaluate(
            symbol="XRPJPY",
            price=99.0,
            account=AccountSnapshot({"JPY": 1000.0, "XRP": 100.0}),
            base_balance=100.0,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            candles=candles,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            daily_risk_state={},
        )
        decision = DirectionDecisionEngine(settings).evaluate(
            symbol="XRPJPY",
            price=99.0,
            candles=candles,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            target_inventory=target,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            open_orders=[],
            exit_reason="stop_loss",
        )

        self.assertEqual(decision.recommended_action, "RISK_EXIT")
        self.assertTrue(decision.allow_risk_exit)


if __name__ == "__main__":
    unittest.main()
