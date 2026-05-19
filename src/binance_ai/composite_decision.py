from __future__ import annotations

from dataclasses import asdict
from typing import Dict, Iterable, Sequence

from binance_ai.config import Settings
from binance_ai.models import AiRiskAssessment, Candle, CompositeDecision, ManagedOrder, PositionSnapshot, SignalAction, TradeSignal
from binance_ai.target_inventory import TargetInventoryDecision


class CompositeDecisionEngine:
    """Scenario-first weighted decision layer.

    This does not replace the order executor or risk engine. It produces the
    intended action quality gate that the trading loop uses before creating
    orders.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        *,
        symbol: str,
        price: float,
        candles: Sequence[Candle] | Iterable[Candle],
        signal: TradeSignal,
        position: PositionSnapshot | None,
        quote_balance: float,
        target_inventory: TargetInventoryDecision,
        ai_assessment: AiRiskAssessment,
        open_orders: Sequence[ManagedOrder],
        activation_state: Dict[str, object],
        timestamp_ms: int,
    ) -> CompositeDecision:
        candle_list = list(candles)
        indicators = self._indicators(candle_list)
        total_equity = max(0.0, target_inventory.total_equity)
        position_value = max(0.0, target_inventory.position_value)
        position_fraction = target_inventory.current_fraction
        cash_fraction = quote_balance / total_equity if total_equity > 0 else 0.0
        entry_protection = self._entry_protection(activation_state, timestamp_ms=timestamp_ms)
        scenario = self._scenario(
            indicators=indicators,
            signal=signal,
            position_fraction=position_fraction,
            target=target_inventory,
            ai=ai_assessment,
            entry_protection=entry_protection,
        )
        target_fraction = self._scenario_target_fraction(scenario, target_inventory.target_fraction)
        target_notional = target_fraction * total_equity
        buy_gap = max(0.0, target_notional - position_value)
        sell_gap = max(0.0, position_value - target_notional)
        expected_edge = max(0.0, self.settings.min_expected_net_edge_pct)
        fee_drag = max(0.0, self.settings.trading_fee_rate * 2.0)

        trend_score = self._normalize_signed(indicators["trend_pct"], 0.012)
        momentum_score = self._normalize_signed(indicators["momentum_pct"], 0.006)
        volume_score = self._normalize(indicators["volume_ratio"] - 1.0, 1.5)
        volatility_score = self._normalize(indicators["volatility_pct"], 0.02)
        underweight_score = self._normalize(max(0.0, target_fraction - position_fraction), 0.6)
        overweight_score = self._normalize(max(0.0, position_fraction - target_fraction), 0.6)
        cash_score = self._normalize(cash_fraction, 0.5)
        cost_distance = 0.0
        if position is not None and position.average_entry_price > 0:
            cost_distance = (price - position.average_entry_price) / position.average_entry_price
        loss_pressure = self._normalize(max(0.0, -cost_distance), 0.02)
        profit_pressure = self._normalize(max(0.0, cost_distance), 0.02)
        open_buy_count = sum(1 for order in open_orders if str(order.side).upper() == "BUY")
        open_sell_count = sum(1 for order in open_orders if str(order.side).upper() == "SELL")
        ai_risk = self._normalize(ai_assessment.risk_score, 1.0)

        buy_score = (
            underweight_score * 0.32
            + cash_score * 0.18
            + max(0.0, trend_score) * 0.16
            + max(0.0, momentum_score) * 0.14
            + volume_score * 0.08
            + (1.0 - ai_risk) * 0.08
            + (0.04 if signal.action == SignalAction.BUY else 0.0)
        )
        sell_score = (
            overweight_score * 0.30
            + max(0.0, -trend_score) * 0.16
            + max(0.0, -momentum_score) * 0.14
            + profit_pressure * 0.12
            + loss_pressure * 0.08
            + ai_risk * 0.10
            + (0.06 if signal.action == SignalAction.SELL else 0.0)
        )
        risk_score = (
            max(0.0, -trend_score) * 0.25
            + max(0.0, -momentum_score) * 0.20
            + volatility_score * 0.15
            + loss_pressure * 0.20
            + ai_risk * 0.20
        )
        if entry_protection["active"]:
            sell_score *= 0.35
            risk_score *= 0.85
        if open_buy_count >= self.settings.order_max_open_per_side:
            buy_score *= 0.45
        if open_sell_count >= self.settings.order_max_open_per_side:
            sell_score *= 0.45
        buy_score = self._clamp(buy_score)
        sell_score = self._clamp(sell_score)
        risk_score = self._clamp(risk_score)
        hold_score = self._clamp(1.0 - max(buy_score, sell_score, risk_score * 0.9))

        blockers = []
        if not ai_assessment.allow_entry and ai_assessment.risk_score < self.settings.risk_exit_score_threshold:
            blockers.append("AI 风险较高，买入评分降权")
            buy_score *= 0.5
        if fee_drag + expected_edge <= 0:
            blockers.append("手续费/净边际配置无效")
        if target_inventory.active_trading_blocker:
            blockers.append(self._daily_blocker_cn(target_inventory.active_trading_blocker))

        action = "HOLD"
        recommended_notional = 0.0
        if risk_score >= self.settings.risk_exit_score_threshold and position_value > 0:
            action = "RISK_EXIT"
            recommended_notional = min(position_value, max(sell_gap, position_value * self.settings.exit_emergency_stop_fraction))
        elif buy_score >= self.settings.buy_score_threshold and buy_gap > 0:
            action = "BUY"
            recommended_notional = buy_gap
        elif sell_score >= self.settings.sell_score_threshold and sell_gap > 0:
            action = "SELL"
            recommended_notional = sell_gap

        breakdown = {
            "trend_score": round(trend_score, 6),
            "momentum_score": round(momentum_score, 6),
            "volume_score": round(volume_score, 6),
            "volatility_score": round(volatility_score, 6),
            "underweight_score": round(underweight_score, 6),
            "overweight_score": round(overweight_score, 6),
            "cash_score": round(cash_score, 6),
            "profit_pressure": round(profit_pressure, 6),
            "loss_pressure": round(loss_pressure, 6),
            "ai_risk": round(ai_risk, 6),
            "fee_drag_pct": round(fee_drag, 6),
            "expected_net_edge_pct": round(expected_edge, 6),
        }
        target_summary = {
            **target_inventory.as_dict(),
            "composite_target_fraction": target_fraction,
            "buy_gap_notional": buy_gap,
            "sell_gap_notional": sell_gap,
            "position_fraction": position_fraction,
            "cash_fraction": cash_fraction,
        }
        explanation = (
            f"{scenario}：买入评分 {buy_score:.2f}，卖出评分 {sell_score:.2f}，"
            f"风险评分 {risk_score:.2f}；目标仓位 {target_fraction:.0%}，"
            f"当前仓位 {position_fraction:.0%}，建议 {self._action_cn(action)}。"
        )
        return CompositeDecision(
            symbol=symbol,
            scenario=scenario,
            recommended_action=action,
            buy_score=round(buy_score, 6),
            sell_score=round(sell_score, 6),
            hold_score=round(hold_score, 6),
            risk_score=round(risk_score, 6),
            target_position_fraction=round(target_fraction, 6),
            recommended_notional=round(recommended_notional, 8),
            blockers=blockers,
            explanation_cn=explanation,
            score_breakdown=breakdown,
            target_position_summary=target_summary,
            entry_protection=entry_protection,
        )

    def fill_metadata(self, decision: CompositeDecision | None) -> Dict[str, object]:
        if decision is None:
            return {}
        return {
            "scenario": decision.scenario,
            "composite_decision": asdict(decision),
            "score_breakdown": decision.score_breakdown,
            "target_position_summary": decision.target_position_summary,
        }

    def _scenario(
        self,
        *,
        indicators: Dict[str, float],
        signal: TradeSignal,
        position_fraction: float,
        target: TargetInventoryDecision,
        ai: AiRiskAssessment,
        entry_protection: Dict[str, object],
    ) -> str:
        if entry_protection["active"]:
            return "入场保护"
        if ai.risk_score >= 0.9 or target.regime == "emergency":
            return "急跌风险"
        if position_fraction < max(0.05, target.lower_fraction * 0.5) and target.available_buy_notional > 0:
            return "低仓位重建"
        if position_fraction > target.upper_fraction:
            return "高仓位减仓"
        if target.regime == "strong_up" or indicators["trend_pct"] >= 0.006 or signal.action == SignalAction.BUY:
            return "强涨跟随"
        if target.regime in {"weak_down", "strong_down"} or indicators["trend_pct"] <= -0.004:
            return "弱跌防守"
        pending = float(target.target_position_fraction if hasattr(target, "target_position_fraction") else 0.0)
        if pending > 0:
            return "回补等待"
        return "震荡网格"

    def _scenario_target_fraction(self, scenario: str, fallback: float) -> float:
        mapping = {
            "强涨跟随": self.settings.target_position_strong_up,
            "震荡网格": self.settings.target_position_range,
            "弱跌防守": self.settings.target_position_weak_down,
            "急跌风险": self.settings.target_position_emergency,
            "低仓位重建": self.settings.target_position_range,
            "高仓位减仓": fallback,
            "回补等待": fallback,
            "入场保护": fallback,
        }
        return min(1.0, max(0.0, mapping.get(scenario, fallback)))

    @staticmethod
    def _entry_protection(state: Dict[str, object], *, timestamp_ms: int) -> Dict[str, object]:
        until = int(float(state.get("entry_protection_until_candle", 0) or 0))
        interval_ms = int(float(state.get("entry_protection_interval_ms", 0) or 0))
        remaining = int(float(state.get("entry_protection_remaining_bars", 0) or 0))
        if until > timestamp_ms and interval_ms > 0:
            remaining = max(remaining, int((until - timestamp_ms + interval_ms - 1) // interval_ms))
        return {
            "active": remaining > 0 or str(state.get("decision_state", "")) == "ENTRY_PROTECTION",
            "remaining_bars": max(0, remaining),
            "until_candle": until,
            "last_entry_trigger": str(state.get("last_entry_trigger", "")),
            "last_entry_price": float(state.get("last_entry_price", 0.0) or 0.0),
        }

    @staticmethod
    def _indicators(candles: Sequence[Candle]) -> Dict[str, float]:
        if len(candles) < 2:
            return {"trend_pct": 0.0, "momentum_pct": 0.0, "volatility_pct": 0.0, "volume_ratio": 1.0}
        closes = [c.close for c in candles if c.close > 0]
        recent = closes[-1]
        trend_base = closes[-min(len(closes), 30)]
        momentum_base = closes[-min(len(closes), 6)]
        ranges = [(c.high - c.low) / c.close for c in candles[-20:] if c.close > 0]
        volumes = [c.volume for c in candles[-20:] if c.volume >= 0]
        avg_volume = sum(volumes[:-1]) / max(1, len(volumes) - 1) if len(volumes) > 1 else (volumes[-1] if volumes else 1.0)
        volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 and volumes else 1.0
        return {
            "trend_pct": (recent / trend_base - 1.0) if trend_base > 0 else 0.0,
            "momentum_pct": (recent / momentum_base - 1.0) if momentum_base > 0 else 0.0,
            "volatility_pct": sum(ranges) / max(1, len(ranges)),
            "volume_ratio": volume_ratio,
        }

    @staticmethod
    def _normalize(value: float, scale: float) -> float:
        if scale <= 0:
            return 0.0
        return max(0.0, min(1.0, value / scale))

    @staticmethod
    def _normalize_signed(value: float, scale: float) -> float:
        if scale <= 0:
            return 0.0
        return max(-1.0, min(1.0, value / scale))

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))

    @staticmethod
    def _action_cn(action: str) -> str:
        return {
            "BUY": "建仓/补仓",
            "SELL": "减仓",
            "RISK_EXIT": "风险退出",
            "HOLD": "观望",
        }.get(action, action)

    @staticmethod
    def _daily_blocker_cn(blocker: str) -> str:
        return {
            "daily_turnover_budget_exhausted": "基础日内成交预算已用尽，需使用恢复预算或等待次日",
            "daily_realized_loss_limit_reached": "日内已实现亏损达到限制",
            "target_inventory_emergency_risk": "目标仓位处于极端风险模式",
        }.get(blocker, blocker)
