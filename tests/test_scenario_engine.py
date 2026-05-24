import unittest

from binance_ai.config import Settings
from binance_ai.models import AiRiskAssessment, Candle
from binance_ai.scenario import ScenarioEngine
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
        kline_interval="1m",
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
    )
    if not updates:
        return base
    from dataclasses import replace

    return replace(base, **updates)


def _target(**updates):
    payload = dict(
        symbol="XRPJPY",
        regime="range",
        target_fraction=0.55,
        lower_fraction=0.47,
        upper_fraction=0.63,
        current_fraction=0.30,
        total_equity=10000.0,
        quote_balance=7000.0,
        position_value=3000.0,
        available_buy_notional=2500.0,
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


def _candles(values):
    candles = []
    for index, close in enumerate(values):
        candles.append(
            Candle(
                open_time=index * 60_000,
                open=close,
                high=close * 1.001,
                low=close * 0.999,
                close=close,
                volume=100.0 + index,
                close_time=index * 60_000 + 59_999,
            )
        )
    return candles


class ScenarioEngineTests(unittest.TestCase):
    def test_uptrend_expansion_generates_probe_entry(self):
        settings = _settings()
        values = [100 + index * 0.03 + (index * index) * 0.001 for index in range(80)]
        decision = ScenarioEngine(settings).evaluate(
            symbol="XRPJPY",
            price=values[-1],
            candles_by_interval={"1m": _candles(values), "3m": _candles(values), "5m": _candles(values), "30m": _candles(values), "1h": _candles(values)},
            target_inventory=_target(),
            ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
            has_position=False,
        )

        self.assertEqual(decision.scenario_state, "UPTREND_PROBE_ENTRY")
        self.assertIn("BUY", decision.allowed_actions)
        self.assertAlmostEqual(decision.buy_size_fraction, settings.trend_probe_entry_fraction)

    def test_low_vol_observe_keeps_existing_orders_only(self):
        settings = _settings(low_vol_atr_pct=0.01)
        values = [100.0 + (index % 2) * 0.01 for index in range(80)]
        decision = ScenarioEngine(settings).evaluate(
            symbol="XRPJPY",
            price=values[-1],
            candles_by_interval={"1m": _candles(values), "3m": _candles(values), "5m": _candles(values), "30m": _candles(values), "1h": _candles(values)},
            target_inventory=_target(),
            ai_assessment=AiRiskAssessment("XRPJPY", "READY", True, 0.1, 1.0, ""),
            has_position=True,
        )

        self.assertEqual(decision.scenario_state, "LOW_VOL_OBSERVE")
        self.assertFalse(decision.generate_new_orders)
        self.assertIn("NEW_BUY", decision.blocked_actions)

    def test_panic_risk_reduction_blocks_normal_orders(self):
        settings = _settings()
        values = [100.0 - index * 0.25 for index in range(80)]
        decision = ScenarioEngine(settings).evaluate(
            symbol="XRPJPY",
            price=values[-1],
            candles_by_interval={"1m": _candles(values), "3m": _candles(values), "5m": _candles(values), "30m": _candles(values), "1h": _candles(values)},
            target_inventory=_target(),
            ai_assessment=AiRiskAssessment("XRPJPY", "EXTREME", False, 0.95, 0.0, "极端风险"),
            has_position=True,
        )

        self.assertEqual(decision.scenario_state, "PANIC_RISK_REDUCTION")
        self.assertEqual(decision.allowed_actions, ["RISK_EXIT"])
        self.assertFalse(decision.generate_new_orders)


if __name__ == "__main__":
    unittest.main()
