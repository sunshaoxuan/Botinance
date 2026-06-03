import unittest

from binance_ai.config import Settings
from binance_ai.models import (
    AiRiskAssessment,
    Candle,
    CompositeDecision,
    DirectionDecision,
    ManagedOrder,
    ScenarioDecision,
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


def _scenario(**updates):
    payload = dict(
        symbol="XRPJPY",
        scenario_state="LOW_VOL_OBSERVE",
        reason_cn="低波动低仓位深折价建仓",
        allowed_actions=["DEEP_DISCOUNT_BUY", "KEEP_OPEN_ORDERS"],
        blocked_actions=["MARKET_BUY", "CHASE_BUY", "NEW_SELL"],
        buy_size_fraction=0.35,
        sell_size_fraction=1.0,
        buy_discount_multiplier=1.8,
        generate_new_orders=True,
    )
    payload.update(updates)
    return ScenarioDecision(**payload)


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

    def test_low_vol_low_inventory_generates_deep_discount_buy_proposal(self):
        decision = PolicyEngine(_settings(order_target_notional=8000.0, min_effective_order_notional=5000.0)).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=100.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "test"),
                exit_reason=None,
                has_position=False,
                base_balance=0.0,
                quote_balance=12000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.0, available_buy_notional=10000.0),
                composite_decision=_composite(recommended_action="HOLD"),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
                direction_decision=_direction(buy_zone_price=99.5),
                scenario_decision=_scenario(),
            )
        )

        self.assertEqual(decision.policy_state, "INVENTORY_REBALANCE")
        self.assertEqual(len(decision.order_proposals), 1)
        self.assertEqual(decision.order_proposals[0].trigger, "low_vol_deep_discount_buy")
        self.assertGreaterEqual(decision.order_proposals[0].notional, 5000.0)

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

    def test_range_market_adaptive_spread_tightens_first_level(self) -> None:
        settings = _settings(
            pair_spread_levels="0.0035,0.0055",
            min_range_spread_pct=0.0012,
            range_spread_atr_multiplier=1.0,
            min_pair_net_edge_pct=0.0015,
            min_effective_order_notional=500.0,
            order_target_notional=2000.0,
        )
        decision = PolicyEngine(settings).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=100.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=50.0,
                quote_balance=5000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.55, available_buy_notional=4000.0, allowed_sell_quantity=25.0),
                composite_decision=_composite(recommended_action="HOLD"),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
                direction_decision=_direction(buy_zone_price=99.0, sell_zone_price=100.3),
                scenario_decision=_scenario(
                    scenario_state="RANGE_MARKET_MAKING",
                    allowed_actions=["BUY", "SELL"],
                    blocked_actions=[],
                    buy_size_fraction=1.0,
                    sell_size_fraction=1.0,
                    buy_discount_multiplier=1.0,
                    indicators={"atr_pct": 0.002},
                ),
            )
        )

        spreads = [item.target_spread_pct for item in decision.merged_order_proposals]
        self.assertTrue(spreads)
        self.assertLess(min(spreads), 0.0035)
        self.assertGreaterEqual(min(spreads), 0.0012)

    def test_dust_pending_buyback_does_not_block_policy(self) -> None:
        settings = _settings(
            min_effective_order_notional=500.0,
            order_target_notional=2000.0,
            grid_min_order_notional=3000.0,
        )
        decision = PolicyEngine(settings).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=228.0,
                candles=_candles(228.0),
                signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=17.0,
                quote_balance=5000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=100.0),
                target_inventory=_target(current_fraction=0.55, available_buy_notional=4000.0, allowed_sell_quantity=10.0),
                composite_decision=_composite(recommended_action="HOLD"),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={"pending_buyback_quantity": 0.1, "last_grid_sell_price": 228.86},
                timestamp_ms=10_000_000,
                direction_decision=_direction(current_price=228.0, fair_value=228.0, buy_zone_price=227.5, sell_zone_price=228.5),
                scenario_decision=_scenario(
                    scenario_state="RANGE_MARKET_MAKING",
                    allowed_actions=["BUY", "SELL"],
                    blocked_actions=[],
                    indicators={"atr_pct": 0.002},
                ),
            )
        )

        self.assertGreater(len(decision.merged_order_proposals), 0)

    def test_below_cost_target_rebalance_sell_is_blocked(self) -> None:
        settings = _settings(
            min_effective_order_notional=500.0,
            order_target_notional=2000.0,
            pair_spread_levels="0.0035,0.0055",
        )
        decision = PolicyEngine(settings).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=207.0,
                candles=_candles(207.0),
                signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.8, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=370.0,
                position_average_entry_price=213.22,
                quote_balance=20000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(
                    current_fraction=0.75,
                    available_buy_notional=0.0,
                    allowed_sell_quantity=80.0,
                    allowed_sell_notional=16000.0,
                ),
                composite_decision=_composite(recommended_action="SELL", sell_score=0.9),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={"real_average_entry_price": 213.22},
                timestamp_ms=10_000_000,
                direction_decision=_direction(current_price=207.0, fair_value=207.0, sell_zone_price=200.0),
            )
        )

        self.assertEqual(decision.order_proposals, [])
        self.assertTrue(any(result.reason == "below_cost_sell_blocked" for result in decision.proposal_filter_results))

    def test_risk_exit_sell_can_bypass_cost_protection(self) -> None:
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=207.0,
                candles=_candles(207.0),
                signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.8, "test"),
                exit_reason="stop_loss",
                has_position=True,
                base_balance=100.0,
                position_average_entry_price=213.22,
                quote_balance=20000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.75, allowed_sell_quantity=80.0),
                composite_decision=_composite(recommended_action="RISK_EXIT", risk_score=0.9),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={"real_average_entry_price": 213.22},
                timestamp_ms=10_000_000,
                direction_decision=_direction(current_price=207.0, fair_value=207.0, sell_zone_price=230.0),
            )
        )

        self.assertEqual(decision.policy_state, "RISK_REDUCTION")
        self.assertEqual(len(decision.order_proposals), 1)
        self.assertEqual(decision.order_proposals[0].trigger, "stop_loss")

    def test_emergency_stop_first_stage_sells_only_configured_fraction(self) -> None:
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=198.0,
                candles=_candles(198.0),
                signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "test"),
                exit_reason="emergency_stop",
                has_position=True,
                base_balance=100.0,
                position_average_entry_price=213.22,
                quote_balance=10000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.75, allowed_sell_quantity=100.0),
                composite_decision=_composite(recommended_action="RISK_EXIT", risk_score=0.95),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.2, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
            )
        )

        self.assertEqual(decision.order_proposals[0].trigger, "emergency_stop")
        self.assertAlmostEqual(decision.order_proposals[0].quantity, 35.0)

    def test_emergency_stop_ai_extreme_allows_full_exit(self) -> None:
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=198.0,
                candles=_candles(198.0),
                signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "test"),
                exit_reason="emergency_stop",
                has_position=True,
                base_balance=100.0,
                position_average_entry_price=213.22,
                quote_balance=10000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.75, allowed_sell_quantity=100.0),
                composite_decision=_composite(recommended_action="RISK_EXIT", risk_score=0.95),
                ai_assessment=AiRiskAssessment("XRPJPY", "EXTREME_RISK", False, 0.95, 1.0, "extreme"),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
            )
        )

        self.assertAlmostEqual(decision.order_proposals[0].quantity, 100.0)

    def test_emergency_stop_waits_for_second_stage_confirmation(self) -> None:
        decision = PolicyEngine(_settings()).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=198.0,
                candles=_candles(198.0),
                signal=TradeSignal("XRPJPY", SignalAction.SELL, 0.9, "test"),
                exit_reason="emergency_stop",
                has_position=True,
                base_balance=100.0,
                position_average_entry_price=213.22,
                quote_balance=10000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.75, allowed_sell_quantity=100.0),
                composite_decision=_composite(recommended_action="RISK_EXIT", risk_score=0.95),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.2, 1.0, ""),
                open_orders=[],
                activation_state={"risk_exit_stage": 1, "emergency_stop_confirmation_bars": 1},
                timestamp_ms=10_000_000,
            )
        )

        self.assertEqual(decision.policy_state, "RISK_REDUCTION")
        self.assertEqual(decision.order_proposals, [])
        self.assertEqual(decision.proposal_filter_results[0].reason, "proposal_size_zero")

    def test_drawdown_guard_allows_recovery_probe_entry(self) -> None:
        decision = PolicyEngine(_settings(min_effective_order_notional=100.0, order_target_notional=2000.0)).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=99.0,
                candles=_candles(99.0),
                signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=1.0,
                quote_balance=9000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.01, daily_realized_pnl=-200.0, available_buy_notional=4000.0),
                composite_decision=_composite(recommended_action="HOLD", buy_score=0.55),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.2, 1.0, ""),
                open_orders=[],
                activation_state={},
                timestamp_ms=10_000_000,
                direction_decision=_direction(current_price=99.0, fair_value=100.0, buy_zone_price=99.5, expected_net_edge_pct=0.006),
            )
        )

        self.assertEqual(decision.policy_state, "RECOVERY_PROBE_ENTRY")
        self.assertEqual(len(decision.order_proposals), 1)
        self.assertEqual(decision.order_proposals[0].trigger, "recovery_probe_entry")

    def test_uptrend_probe_adds_near_price_confirmation_buy(self) -> None:
        open_orders = [
            ManagedOrder(
                client_order_id=f"open-buy-{index}",
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=10.0,
                limit_price=98.0 - index,
                time_in_force="GTC",
                status="OPEN",
                created_at_ms=1_000,
                updated_at_ms=1_000,
                expires_at_ms=0,
                trigger="trend_probe_entry",
                ladder_group="pair_market_making",
                remaining_quantity=10.0,
                reserved_quote=1000.0,
                tier_index=index,
            )
            for index in range(2)
        ]
        decision = PolicyEngine(
            _settings(
                min_effective_order_notional=500.0,
                order_target_notional=2000.0,
                uptrend_confirmation_passive_offset_pct=0.0003,
            )
        ).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=100.0,
                candles=_candles(100.0),
                signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.8, "test"),
                exit_reason=None,
                has_position=False,
                base_balance=0.0,
                quote_balance=9000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.01, available_buy_notional=4000.0),
                composite_decision=_composite(recommended_action="HOLD", buy_score=0.58),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.2, 1.0, ""),
                open_orders=open_orders,
                activation_state={},
                timestamp_ms=10_000_000,
                direction_decision=_direction(
                    current_price=100.0,
                    fair_value=99.0,
                    buy_zone_price=98.5,
                    sell_zone_price=99.5,
                    expected_net_edge_pct=0.006,
                ),
                scenario_decision=_scenario(
                    scenario_state="UPTREND_PROBE_ENTRY",
                    reason_cn="短周期向上扩散",
                    allowed_actions=["BUY"],
                    blocked_actions=["FULL_SIZE_BUY"],
                    buy_size_fraction=0.25,
                    buy_discount_multiplier=1.0,
                    buy_anchor_price=99.0,
                ),
            )
        )

        confirmation = [item for item in decision.order_proposals if item.trigger == "trend_confirmation_entry"]
        self.assertEqual(len(confirmation), 1)
        self.assertAlmostEqual(confirmation[0].target_spread_pct, 0.0003)
        self.assertGreaterEqual(confirmation[0].expected_pair_net_edge_pct, 0.0045)
        self.assertTrue(any(item.reason == "duplicate_open_ladder_order" for item in decision.proposal_filter_results))

    def test_pending_buyback_generates_counter_buyback_proposal(self) -> None:
        decision = PolicyEngine(_settings(min_effective_order_notional=500.0, order_target_notional=2000.0)).evaluate(
            PolicyContext(
                symbol="XRPJPY",
                price=211.0,
                candles=_candles(211.0),
                signal=TradeSignal("XRPJPY", SignalAction.HOLD, 0.5, "test"),
                exit_reason=None,
                has_position=True,
                base_balance=300.0,
                quote_balance=10000.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=50.0),
                target_inventory=_target(current_fraction=0.55, available_buy_notional=4000.0, allowed_sell_quantity=20.0),
                composite_decision=_composite(recommended_action="HOLD"),
                ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
                open_orders=[],
                activation_state={
                    "pending_buyback_quantity": 30.0,
                    "last_release_price": 213.22,
                    "target_buyback_price": 211.0,
                    "last_release_pair_id": "pair-1",
                },
                timestamp_ms=10_000_000,
                direction_decision=_direction(current_price=211.0, fair_value=211.0, buy_zone_price=200.0),
            )
        )

        self.assertEqual(len(decision.order_proposals), 1)
        self.assertEqual(decision.order_proposals[0].trigger, "pair_counter_buyback")
        self.assertEqual(decision.order_proposals[0].pair_id, "pair-1")


if __name__ == "__main__":
    unittest.main()
