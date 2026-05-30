from __future__ import annotations

from dataclasses import asdict
from typing import Mapping, Sequence

from binance_ai.config import Settings
from binance_ai.direction.engine import FairValueEngine
from binance_ai.models import AiRiskAssessment, Candle, ScenarioDecision
from binance_ai.target_inventory import TargetInventoryDecision


class ScenarioEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fair_value = FairValueEngine(settings)

    def evaluate(
        self,
        *,
        symbol: str,
        price: float,
        candles_by_interval: Mapping[str, Sequence[Candle]],
        target_inventory: TargetInventoryDecision,
        ai_assessment: AiRiskAssessment,
        has_position: bool,
    ) -> ScenarioDecision:
        main = list(candles_by_interval.get(self.settings.kline_interval) or next(iter(candles_by_interval.values()), []))
        fair = self.fair_value.evaluate(symbol=symbol, price=price, candles=main)
        metrics = {interval: self._metrics(candles) for interval, candles in candles_by_interval.items()}
        short_intervals = ["1m", "3m", "5m"]
        expanding = sum(1 for interval in short_intervals if metrics.get(interval, {}).get("ma6_above_ma18") and metrics.get(interval, {}).get("spread_slope", 0.0) > 0)
        contracting = sum(1 for interval in short_intervals if metrics.get(interval, {}).get("ma6_above_ma18") and metrics.get(interval, {}).get("spread_slope", 0.0) < 0)
        falling = sum(1 for interval in short_intervals if metrics.get(interval, {}).get("ma6_slope", 0.0) < 0 and metrics.get(interval, {}).get("ma6_above_ma18") is False)
        main_metrics = metrics.get(self.settings.kline_interval) or metrics.get("1m") or {}
        atr_pct = float(main_metrics.get("atr_pct", 0.0))
        volume_ratio = float(main_metrics.get("volume_ratio", 1.0))
        fair_deviation = price / fair.fair_value - 1.0 if fair.fair_value > 0 else 0.0
        inventory = self._inventory_state(target_inventory)
        extreme_risk = ai_assessment.risk_score >= 0.9 or "极端" in ai_assessment.veto_reason or "extreme" in ai_assessment.veto_reason.lower()

        state = "RANGE_MARKET_MAKING"
        reason = "行情处于可做市区间，按公平价和库存偏移生成双边提案"
        allowed = ["BUY", "SELL"]
        blocked: list[str] = []
        buy_anchor = min(fair.fair_value, fair.vwap_value, fair.range_midpoint)
        sell_anchor = max(fair.fair_value, fair.vwap_value, fair.range_midpoint)
        buy_fraction = 1.0
        sell_fraction = 1.0
        buy_discount_multiplier = 1.0
        generate = True
        templates = [{"name": "pair_market_making", "levels": self.settings.order_levels_per_side, "source": "range"}]

        if extreme_risk or self._panic(main_metrics):
            state = "PANIC_RISK_REDUCTION"
            reason = "AI 或价格波动触发急跌风险，只允许风险退出和保护锁"
            allowed = ["RISK_EXIT"]
            blocked = ["BUY", "NORMAL_SELL"]
            generate = False
            templates = [{"name": "risk_reduction_only", "levels": 0, "source": "panic"}]
        elif atr_pct > 0 and atr_pct <= self.settings.low_vol_atr_pct:
            state = "LOW_VOL_OBSERVE"
            reason = "ATR 处于低波动区间，本轮不新增订单，只维护已有 GTC 挂单"
            allowed = ["KEEP_OPEN_ORDERS"]
            blocked = ["NEW_BUY", "NEW_SELL"]
            generate = False
            templates = [{"name": "observe_only", "levels": 0, "source": "low_vol"}]
            if inventory == "HIGH_INVENTORY" and target_inventory.available_sell_quantity * price >= self.settings.min_effective_order_notional:
                reason = "ATR 处于低波动区间，但当前高库存偏离目标，允许溢价释放库存挂单"
                allowed = ["SELL_IF_OVER_INVENTORY", "KEEP_OPEN_ORDERS"]
                blocked = ["NEW_BUY", "CHASE_BUY"]
                buy_fraction = 0.0
                sell_fraction = 0.5
                generate = True
                templates = [{"name": "low_vol_inventory_release", "levels": 1, "source": "low_vol_high_inventory"}]
            elif inventory == "LOW_INVENTORY" and target_inventory.available_buy_notional >= self.settings.min_effective_order_notional:
                reason = "ATR 处于低波动区间，但当前低仓位且现金充足，允许深折价 GTC 建仓挂单"
                allowed = ["DEEP_DISCOUNT_BUY", "KEEP_OPEN_ORDERS"]
                blocked = ["MARKET_BUY", "CHASE_BUY", "NEW_SELL"]
                buy_fraction = 0.35
                buy_discount_multiplier = max(1.0, self.settings.downtrend_buy_discount_multiplier)
                generate = True
                templates = [{"name": "low_vol_deep_discount_entry", "levels": 1, "source": "low_vol_low_inventory"}]
        elif expanding >= max(1, self.settings.uptrend_expansion_min_periods):
            gap = float(main_metrics.get("ma_gap_pct", 0.0))
            if contracting >= 2 and gap <= self.settings.uptrend_exhaustion_gap_pct:
                state = "UPTREND_EXHAUSTION_TAKE_PROFIT"
                reason = "MA6 走平且 MA18 追近，暂停追买，允许按库存和卖出区分批减仓"
                allowed = ["SELL", "HOLD"]
                blocked = ["CHASE_BUY"]
                buy_fraction = 0.0
                templates = [{"name": "take_profit_ladder", "levels": 2, "source": "uptrend_exhaustion"}]
            elif fair_deviation <= self.settings.buy_zone_min_discount_pct:
                state = "UPTREND_PULLBACK_ENTRY"
                reason = "上行结构保持，价格回到 MA18、VWAP 或公平价附近，允许受控回调买入"
                allowed = ["BUY", "KEEP_SELL"]
                blocked = ["CHASE_BUY_ABOVE_ANCHOR"]
                buy_fraction = self.settings.trend_probe_entry_fraction
                anchor_values = [buy_anchor]
                for interval in short_intervals:
                    value = float(metrics.get(interval, {}).get("ma18", 0.0))
                    if value > 0:
                        anchor_values.append(value)
                buy_anchor = min(anchor_values)
                templates = [{"name": "pullback_entry", "levels": 2, "source": "uptrend_pullback"}]
            elif has_position:
                state = "UPTREND_HOLD_EXPANSION"
                reason = "MA6 与 MA18 向上扩散，已有仓位优先持有，卖单只在卖出区或库存超标时生成"
                allowed = ["HOLD", "SELL_IF_OVER_INVENTORY"]
                blocked = ["EARLY_TAKE_PROFIT", "CHASE_BUY"]
                buy_fraction = 0.0
                templates = [{"name": "hold_expansion", "levels": 0, "source": "uptrend_hold"}]
            else:
                state = "UPTREND_PROBE_ENTRY"
                reason = "短周期至少两个级别向上扩散，允许小仓位确认买入，仍采用限价挂单"
                allowed = ["BUY"]
                blocked = ["FULL_SIZE_BUY"]
                buy_fraction = self.settings.trend_probe_entry_fraction
                templates = [{"name": "probe_entry", "levels": 2, "source": "uptrend_probe"}]
        elif falling >= 2:
            state = "DOWNTREND_DEFENSIVE"
            reason = "短周期 MA6 位于 MA18 下方且继续走弱，降低目标仓位，只允许深折价小仓位试单"
            allowed = ["DEEP_DISCOUNT_BUY", "RISK_EXIT"]
            blocked = ["NORMAL_BUY", "CHASE_BUY"]
            buy_fraction = 0.25
            buy_discount_multiplier = max(1.0, self.settings.downtrend_buy_discount_multiplier)
            templates = [{"name": "defensive_deep_discount", "levels": 1, "source": "downtrend"}]
        elif self._recovery(metrics):
            state = "RECOVERY_AFTER_DROP"
            reason = "下跌后 MA6 重新上穿 MA18 且成交量恢复，允许恢复建仓首单"
            allowed = ["BUY"]
            blocked = ["FULL_SIZE_BUY"]
            buy_fraction = self.settings.recovery_entry_fraction
            templates = [{"name": "recovery_entry", "levels": 2, "source": "recovery"}]

        indicators = {
            "ma_expanding_periods": expanding,
            "ma_contracting_periods": contracting,
            "ma_falling_periods": falling,
            "atr_pct": round(atr_pct, 8),
            "volume_ratio": round(volume_ratio, 6),
            "fair_deviation_pct": round(fair_deviation, 8),
            "inventory_state": inventory,
            "fair_value": asdict(fair),
            "intervals": metrics,
        }
        return ScenarioDecision(
            symbol=symbol,
            scenario_state=state,
            reason_cn=reason,
            indicators=indicators,
            order_templates=templates,
            allowed_actions=allowed,
            blocked_actions=blocked,
            buy_anchor_price=round(buy_anchor, 8),
            sell_anchor_price=round(sell_anchor, 8),
            buy_size_fraction=max(0.0, min(1.0, buy_fraction)),
            sell_size_fraction=max(0.0, min(1.0, sell_fraction)),
            buy_discount_multiplier=max(0.0, buy_discount_multiplier),
            generate_new_orders=generate,
        )

    def _metrics(self, candles: Sequence[Candle]) -> dict[str, object]:
        values = list(candles)
        closes = [c.close for c in values if c.close > 0]
        if len(closes) < 6:
            return {"bars": len(values)}
        ma6 = sum(closes[-6:]) / 6.0
        ma18_window = closes[-18:] if len(closes) >= 18 else closes
        ma18 = sum(ma18_window) / len(ma18_window)
        previous_closes = closes[:-1]
        previous_ma6 = sum(previous_closes[-6:]) / 6.0 if len(previous_closes) >= 6 else ma6
        previous_ma18_window = previous_closes[-18:] if len(previous_closes) >= 18 else previous_closes
        previous_ma18 = sum(previous_ma18_window) / len(previous_ma18_window) if previous_ma18_window else ma18
        atr = self._atr(values[-20:])
        close = closes[-1]
        volume_window = [max(0.0, c.volume) for c in values[-20:]]
        recent_volume = sum(volume_window[-3:]) / max(1, min(3, len(volume_window))) if volume_window else 0.0
        base_volume = sum(volume_window) / len(volume_window) if volume_window else 0.0
        return {
            "bars": len(values),
            "ma6": round(ma6, 8),
            "ma18": round(ma18, 8),
            "ma6_above_ma18": ma6 > ma18,
            "ma6_slope": round(ma6 / previous_ma6 - 1.0, 8) if previous_ma6 > 0 else 0.0,
            "spread_slope": round((ma6 - ma18) - (previous_ma6 - previous_ma18), 8),
            "ma_gap_pct": round(abs(ma6 - ma18) / close, 8) if close > 0 else 0.0,
            "atr_pct": round(atr / close, 8) if close > 0 else 0.0,
            "volume_ratio": round(recent_volume / base_volume, 6) if base_volume > 0 else 1.0,
        }

    @staticmethod
    def _atr(candles: Sequence[Candle]) -> float:
        if not candles:
            return 0.0
        previous_close = candles[0].close
        ranges: list[float] = []
        for candle in candles:
            ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
            previous_close = candle.close
        return sum(ranges) / max(1, len(ranges))

    @staticmethod
    def _panic(metrics: dict[str, object]) -> bool:
        ma6_slope = float(metrics.get("ma6_slope", 0.0))
        atr_pct = float(metrics.get("atr_pct", 0.0))
        return ma6_slope < -0.006 and atr_pct > 0.004

    @staticmethod
    def _recovery(metrics: Mapping[str, dict[str, object]]) -> bool:
        recovered = 0
        for interval in ("1m", "3m", "5m"):
            item = metrics.get(interval, {})
            if item.get("ma6_above_ma18") and float(item.get("ma6_slope", 0.0)) > 0 and float(item.get("volume_ratio", 1.0)) >= 1.0:
                recovered += 1
        return recovered >= 2

    @staticmethod
    def _inventory_state(target_inventory: TargetInventoryDecision) -> str:
        if target_inventory.current_fraction < target_inventory.lower_fraction:
            return "LOW_INVENTORY"
        if target_inventory.current_fraction > target_inventory.upper_fraction:
            return "HIGH_INVENTORY"
        return "BALANCED"
