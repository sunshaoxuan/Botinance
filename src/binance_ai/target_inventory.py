from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable

from binance_ai.config import Settings
from binance_ai.models import AccountSnapshot, AiRiskAssessment, Candle, OrderRequest, SignalAction, TradeSignal


@dataclass(frozen=True)
class TargetInventoryDecision:
    symbol: str
    regime: str
    target_fraction: float
    lower_fraction: float
    upper_fraction: float
    current_fraction: float
    total_equity: float
    quote_balance: float
    position_value: float
    available_buy_notional: float
    allowed_sell_notional: float
    allowed_sell_quantity: float
    min_cash_reserve: float
    reason: str
    daily_turnover_used: float
    daily_turnover_limit: float
    daily_realized_pnl: float
    daily_loss_limit: float
    active_trading_allowed: bool
    active_trading_blocker: str = ""

    def as_dict(self) -> Dict[str, object]:
        return {
            "symbol": self.symbol,
            "regime": self.regime,
            "target_fraction": self.target_fraction,
            "lower_fraction": self.lower_fraction,
            "upper_fraction": self.upper_fraction,
            "current_fraction": self.current_fraction,
            "total_equity": self.total_equity,
            "quote_balance": self.quote_balance,
            "position_value": self.position_value,
            "available_buy_notional": self.available_buy_notional,
            "allowed_sell_notional": self.allowed_sell_notional,
            "allowed_sell_quantity": self.allowed_sell_quantity,
            "min_cash_reserve": self.min_cash_reserve,
            "reason": self.reason,
            "daily_turnover_used": self.daily_turnover_used,
            "daily_turnover_limit": self.daily_turnover_limit,
            "daily_realized_pnl": self.daily_realized_pnl,
            "daily_loss_limit": self.daily_loss_limit,
            "active_trading_allowed": self.active_trading_allowed,
            "active_trading_blocker": self.active_trading_blocker,
        }


class TargetInventoryEngine:
    """Portfolio-level inventory target, separated from stop/take-profit exits."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(
        self,
        *,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        base_balance: float,
        signal: TradeSignal,
        candles: Iterable[Candle],
        ai_assessment: AiRiskAssessment,
        daily_risk_state: Dict[str, object],
    ) -> TargetInventoryDecision:
        quote_balance = account.balance_of(self.settings.quote_asset)
        position_value = max(0.0, base_balance * price)
        total_equity = max(0.0, quote_balance + position_value)
        current_fraction = position_value / total_equity if total_equity > 0 else 0.0
        regime = self._classify_regime(signal=signal, candles=list(candles), ai_assessment=ai_assessment)
        target_fraction = self._target_fraction_for_regime(regime)
        band = min(0.5, max(0.0, self.settings.target_position_band_pct))
        lower = max(0.0, target_fraction - band)
        upper = min(1.0, target_fraction + band)
        min_cash_reserve = total_equity * min(1.0, max(0.0, self.settings.min_cash_reserve_fraction))
        max_buy_notional = max(0.0, quote_balance - min_cash_reserve)
        buy_gap = max(0.0, (target_fraction * total_equity) - position_value)
        sell_gap = max(0.0, position_value - (target_fraction * total_equity))
        available_buy_notional = min(max_buy_notional, buy_gap) if current_fraction < lower else 0.0
        allowed_sell_notional = sell_gap if current_fraction > upper else 0.0
        allowed_sell_quantity = allowed_sell_notional / price if price > 0 else 0.0

        daily_turnover_used = float(daily_risk_state.get("turnover_notional", 0.0) or 0.0)
        daily_realized_pnl = float(daily_risk_state.get("realized_pnl", 0.0) or 0.0)
        daily_turnover_limit = total_equity * max(0.0, self.settings.max_daily_turnover_fraction)
        daily_loss_limit = total_equity * max(0.0, self.settings.max_daily_realized_loss_pct)
        recovery_turnover_limit = daily_turnover_limit
        if current_fraction < lower and daily_realized_pnl > -daily_loss_limit:
            recovery_turnover_limit += total_equity * max(0.0, self.settings.recovery_turnover_fraction)
        active_allowed = True
        blocker = ""
        if recovery_turnover_limit > 0 and daily_turnover_used >= recovery_turnover_limit:
            active_allowed = False
            blocker = "daily_turnover_budget_exhausted"
        if daily_loss_limit > 0 and daily_realized_pnl <= -daily_loss_limit:
            active_allowed = False
            blocker = "daily_realized_loss_limit_reached"
        if regime == "emergency":
            active_allowed = False
            blocker = "target_inventory_emergency_risk"

        reason = (
            f"{regime}: 当前仓位 {current_fraction:.2%}，目标 {target_fraction:.2%}，"
            f"可买 {available_buy_notional:.2f} {self.settings.quote_asset}，"
            f"可卖 {allowed_sell_quantity:.8f}"
        )
        if blocker:
            reason = f"{reason}；主动交易暂停：{blocker}"

        return TargetInventoryDecision(
            symbol=symbol,
            regime=regime,
            target_fraction=target_fraction,
            lower_fraction=lower,
            upper_fraction=upper,
            current_fraction=current_fraction,
            total_equity=total_equity,
            quote_balance=quote_balance,
            position_value=position_value,
            available_buy_notional=available_buy_notional,
            allowed_sell_notional=allowed_sell_notional,
            allowed_sell_quantity=allowed_sell_quantity,
            min_cash_reserve=min_cash_reserve,
            reason=reason,
            daily_turnover_used=daily_turnover_used,
            daily_turnover_limit=daily_turnover_limit,
            daily_realized_pnl=daily_realized_pnl,
            daily_loss_limit=daily_loss_limit,
            active_trading_allowed=active_allowed,
            active_trading_blocker=blocker,
        )

    def build_buy_order(self, *, decision: TargetInventoryDecision, price: float, quantize_quantity, step_size: float, min_qty: float) -> tuple[OrderRequest | None, str]:
        if not self.settings.target_inventory_enabled:
            return None, "target_inventory_disabled"
        if not decision.active_trading_allowed:
            return None, decision.active_trading_blocker
        if decision.available_buy_notional <= 0:
            return None, "target_position_buy_gap_not_available"
        desired_total = max(0.0, self.settings.order_target_notional) * max(1, self.settings.order_max_open_per_side)
        spend = min(decision.available_buy_notional, desired_total)
        quantity = quantize_quantity(spend / price, step_size) if price > 0 else 0.0
        if quantity <= 0 or quantity < min_qty:
            return None, f"target_inventory_buy_quantity_below_min_qty:{quantity}"
        return OrderRequest(symbol=decision.symbol, side="BUY", order_type="MARKET", quantity=quantity), ""

    def build_sell_order(self, *, decision: TargetInventoryDecision, price: float, quantize_quantity, step_size: float, min_qty: float) -> tuple[OrderRequest | None, str]:
        if not self.settings.target_inventory_enabled:
            return None, "target_inventory_disabled"
        if not decision.active_trading_allowed:
            return None, decision.active_trading_blocker
        if decision.allowed_sell_quantity <= 0:
            return None, "target_position_sell_gap_not_available"
        desired_total = max(0.0, self.settings.order_target_notional) * max(1, self.settings.order_max_open_per_side)
        target_quantity = min(decision.allowed_sell_quantity, desired_total / price if price > 0 else 0.0)
        quantity = quantize_quantity(target_quantity, step_size)
        if quantity <= 0 or quantity < min_qty:
            return None, f"target_inventory_sell_quantity_below_min_qty:{quantity}"
        return OrderRequest(symbol=decision.symbol, side="SELL", order_type="MARKET", quantity=quantity), ""

    def current_day_key(self, timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def normalized_daily_state(self, raw: Dict[str, object] | None, *, timestamp_ms: int) -> Dict[str, object]:
        day = self.current_day_key(timestamp_ms)
        state = dict(raw or {})
        if state.get("day") != day:
            return {"day": day, "turnover_notional": 0.0, "realized_pnl": 0.0}
        state.setdefault("turnover_notional", 0.0)
        state.setdefault("realized_pnl", 0.0)
        return state

    def _classify_regime(self, *, signal: TradeSignal, candles: list[Candle], ai_assessment: AiRiskAssessment) -> str:
        if ai_assessment.risk_score >= 0.9 or "极端" in ai_assessment.veto_reason or "extreme" in ai_assessment.veto_reason.lower():
            return "emergency"
        trend_pct = 0.0
        short_pct = 0.0
        if len(candles) >= 2:
            long_window = candles[-min(len(candles), 30)]
            short_window = candles[-min(len(candles), 6)]
            if long_window.close > 0:
                trend_pct = (candles[-1].close - long_window.close) / long_window.close
            if short_window.close > 0:
                short_pct = (candles[-1].close - short_window.close) / short_window.close
        action = signal.action
        if trend_pct <= -0.006 or (action == SignalAction.SELL and short_pct <= -0.003):
            return "strong_down"
        if trend_pct <= -0.002 or action == SignalAction.SELL:
            return "weak_down"
        if trend_pct >= 0.006 or (action == SignalAction.BUY and short_pct >= 0.003):
            return "strong_up"
        return "range"

    def _target_fraction_for_regime(self, regime: str) -> float:
        values = {
            "strong_down": self.settings.target_position_strong_down,
            "weak_down": self.settings.target_position_weak_down,
            "range": self.settings.target_position_range,
            "strong_up": self.settings.target_position_strong_up,
            "emergency": self.settings.target_position_emergency,
        }
        return min(1.0, max(0.0, values.get(regime, self.settings.target_position_range)))
