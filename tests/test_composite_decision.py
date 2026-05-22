import unittest
from dataclasses import replace

from binance_ai.composite_decision import CompositeDecisionEngine
from binance_ai.models import AccountSnapshot, AiRiskAssessment, Candle, PositionSnapshot, SignalAction, TradeSignal
from binance_ai.target_inventory import TargetInventoryEngine
from tests.test_position_activation import _settings


def _candles(start: float, end: float, count: int = 40):
    candles = []
    for index in range(count):
        value = start + (end - start) * index / max(1, count - 1)
        candles.append(
            Candle(
                open_time=index * 60_000,
                open=value,
                high=value * 1.001,
                low=value * 0.999,
                close=value,
                volume=1000 + index,
                close_time=(index + 1) * 60_000 - 1,
            )
        )
    return candles


class CompositeDecisionEngineTests(unittest.TestCase):
    def test_low_inventory_with_cash_no_longer_chases_direction_directly(self) -> None:
        settings = replace(_settings(), target_inventory_enabled=True, composite_decision_enabled=True)
        target = TargetInventoryEngine(settings).evaluate(
            symbol="XRPJPY",
            price=100.0,
            account=AccountSnapshot({"JPY": 10000.0, "XRP": 1.0}),
            base_balance=1.0,
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            candles=_candles(99.0, 100.0),
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            daily_risk_state={},
        )
        decision = CompositeDecisionEngine(settings).evaluate(
            symbol="XRPJPY",
            price=100.0,
            candles=_candles(99.0, 100.0),
            signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "hold"),
            position=PositionSnapshot(quantity=1.0, average_entry_price=100.0),
            quote_balance=10000.0,
            target_inventory=target,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            open_orders=[],
            activation_state={},
            timestamp_ms=1_000_000,
        )

        self.assertEqual(decision.scenario, "低仓位重建")
        self.assertIn(decision.recommended_action, {"HOLD", "BUY"})
        self.assertGreater(decision.buy_score, decision.sell_score)

    def test_entry_protection_suppresses_sell_score(self) -> None:
        settings = replace(_settings(), target_inventory_enabled=True, composite_decision_enabled=True)
        target = TargetInventoryEngine(settings).evaluate(
            symbol="XRPJPY",
            price=100.0,
            account=AccountSnapshot({"JPY": 0.0, "XRP": 100.0}),
            base_balance=100.0,
            signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "sell"),
            candles=_candles(102.0, 100.0),
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            daily_risk_state={},
        )
        protected = CompositeDecisionEngine(settings).evaluate(
            symbol="XRPJPY",
            price=100.0,
            candles=_candles(102.0, 100.0),
            signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "sell"),
            position=PositionSnapshot(quantity=100.0, average_entry_price=100.0),
            quote_balance=0.0,
            target_inventory=target,
            ai_assessment=AiRiskAssessment("XRPJPY", "PASS", True, 0.1, 1.0, ""),
            open_orders=[],
            activation_state={
                "decision_state": "ENTRY_PROTECTION",
                "entry_protection_until_candle": 2_000_000,
                "entry_protection_interval_ms": 60_000,
            },
            timestamp_ms=1_000_000,
        )

        self.assertEqual(protected.scenario, "入场保护")
        self.assertLess(protected.sell_score, settings.sell_score_threshold)
        self.assertTrue(protected.entry_protection["active"])


if __name__ == "__main__":
    unittest.main()
