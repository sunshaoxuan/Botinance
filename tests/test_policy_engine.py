import unittest

from binance_ai.config import Settings
from binance_ai.models import (
    AiRiskAssessment,
    Candle,
    CompositeDecision,
    DirectionDecision,
    SignalAction,
    SymbolFilters,
    TradeSignal,
)
from binance_ai.policy.engine import PolicyContext, PolicyEngine
from binance_ai.target_inventory import TargetInventoryDecision


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
    )
    if not updates:
        return base
    from dataclasses import replace

    return replace(base, **updates)


def _candles(last_price: float = 100.0):
    return [
        Candle(
            open_time=index * 3_600_000,
            open=last_price - 1.0 + index * 0.01,
            high=last_price + 0.5 + index * 0.01,
            low=last_price - 1.5 + index * 0.01,
            close=last_price - 1.0 + index * 0.01,
            volume=100.0,
            close_time=index * 3_600_000 + 3_599_999,
        )
        for index in range(60)
    ]


def _target(**updates):
    payload = dict(
        symbol="XRPJPY",
        regime="range",
        target_fraction=0.55,
        lower_fraction=0.47,
        upper_fraction=0.63,
        current_fraction=0.10,
        total_equity=10000.0,
        quote_balance=9000.0,
        position_value=1000.0,
        available_buy_notional=4000.0,
        allowed_sell_notional=0.0,
        allowed_sell_quantity=0.0,
        min_cash_reserve=1000.0,
        reason="test",
        daily_turnover_used=0.0,
        daily_turnover_limit=10000.0,
        daily_realized_pnl=0.0,
        daily_loss_limit=100.0,
        active_trading_allowed=True,
        active_trading_blocker="",
    )
    payload.update(updates)
    return TargetInventoryDecision(**payload)


def _composite(**updates):
    payload = dict(
        symbol="XRPJPY",
        scenario="低仓位重建",
        recommended_action="BUY",
        buy_score=0.9,
        sell_score=0.1,
        hold_score=0.1,
        risk_score=0.1,
        target_position_fraction=0.55,
        recommended_notional=4000.0,
        blockers=[],
        explanation_cn="测试复合决策",
        score_breakdown={},
        target_position_summary={},
        entry_protection={},
    )
    payload.update(updates)
    return CompositeDecision(**payload)


def _direction(**updates):
    payload = dict(
        symbol="XRPJPY",
        mode="RANGE",
        recommended_action="HOLD",
        price_zone="NEUTRAL_ZONE",
        current_price=100.0,
        fair_value=100.0,
        buy_zone_price=99.5,
        sell_zone_price=100.5,
        expected_net_edge_pct=0.0,
        allow_buy=False,
        allow_sell=False,
        allow_risk_exit=False,
        reason_cn="当前价不在折价建仓区，拒绝追涨买入",
        blockers=["当前价不在折价建仓区，拒绝追涨买入"],
        fair_value_summary={},
        paired_order_state={},
    )
    payload.update(updates)
    return DirectionDecision(**payload)


class PolicyEngineTests(unittest.TestCase):
    def test_pair_lock_after_risk_exit_filters_buy_proposal(self):
        settings = _settings()
        decision = PolicyEngine(settings).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=102.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.BUY, 0.9, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=10.0,
                quote_balance=9000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(),
                composite_decision=_composite(),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={
                    "last_risk_exit_price": 101.0,
                    "risk_exit_reentry_price": 100.5,
                    "last_risk_exit_timestamp_ms": 1_000_000,
                },
                timestamp_ms=1_000_000 + 2 * 3_600_000,
            )
        )

        self.assertEqual(decision.policy_state, "PAIR_LOCKED_AFTER_STOP")
        self.assertEqual(decision.order_proposals, [])
        self.assertTrue(any(lock.lock_type == "PAIR_LOCK_AFTER_STOP" for lock in decision.protection_locks))

    def test_low_inventory_generates_buy_proposal(self):
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=100.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.BUY, 0.9, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=10.0,
                quote_balance=9000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(),
                composite_decision=_composite(),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
            )
        )

        self.assertIn(decision.policy_state, {"INVENTORY_REBALANCE", "MARKET_MAKING"})
        self.assertEqual(len(decision.merged_order_proposals), 2)
        self.assertEqual(len(decision.order_proposals), 2)
        self.assertTrue(all(item.side == "BUY" for item in decision.order_proposals))
        self.assertTrue(all(item.pair_id for item in decision.order_proposals))
        self.assertGreaterEqual(decision.order_proposals[0].notional, 5000.0)
        self.assertGreater(decision.inventory_skew_summary.buy_weight, 1.0)

    def test_direction_decision_blocks_low_inventory_chase_buy(self):
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=101.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.BUY, 0.9, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=10.0,
                quote_balance=9000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(),
                composite_decision=_composite(),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
                direction_decision=_direction(),
            )
        )

        self.assertEqual(len(decision.order_proposals), 1)
        self.assertEqual(decision.order_proposals[0].side, "BUY")
        self.assertEqual(decision.order_proposals[0].tier_index, 4)
        self.assertEqual(decision.direction_decision.price_zone, "NEUTRAL_ZONE")

    def test_drawdown_guard_blocks_active_proposals(self):
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=100.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.BUY, 0.9, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=10.0,
                quote_balance=9000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(daily_realized_pnl=-200.0),
                composite_decision=_composite(),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
            )
        )

        self.assertEqual(decision.policy_state, "OBSERVE_ONLY")
        self.assertTrue(any(lock.lock_type == "DRAWDOWN_GUARD" for lock in decision.protection_locks))
        self.assertEqual(decision.order_proposals, [])


if __name__ == "__main__":
    unittest.main()
