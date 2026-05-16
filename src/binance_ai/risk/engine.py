from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import AccountSnapshot, AiRiskAssessment, BuyDecisionDiagnostic, Candle, OrderRequest, PositionDiagnostic, PositionSnapshot, SellDecisionDiagnostic, SymbolFilters, TradeSignal
from binance_ai.position_activation import PositionActivationDecision


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    order: OrderRequest | None = None


@dataclass(frozen=True)
class DynamicExitProfile:
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    stop_loss_multiplier: float
    take_profit_multiplier: float
    trailing_stop_multiplier: float
    trend_score: float
    volatility_ratio: float
    volume_ratio: float
    strong_trend: bool
    explanation: str


class RiskEngine:
    def __init__(self, settings: Settings, client: BinanceSpotClient) -> None:
        self.settings = settings
        self.client = client

    def ensure_symbol_limit(self) -> None:
        limit = self.settings.active_symbol_limit
        if limit is not None and len(self.settings.trading_symbols) > limit:
            raise ValueError(
                f"Configured {len(self.settings.trading_symbols)} symbols, limit is {limit}."
            )

    def build_buy_order(
        self,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        filters: SymbolFilters,
        position_multiplier: float = 1.0,
    ) -> RiskDecision:
        quote_balance = account.balance_of(self.settings.quote_asset)
        normalized_multiplier = min(1.0, max(0.0, position_multiplier))
        quote_budget = quote_balance * self.settings.risk_per_trade * normalized_multiplier
        notional = min(quote_budget, quote_balance)
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        raw_quantity = notional / price if price > 0 else 0.0
        quantity = self.client.quantize_quantity(raw_quantity, filters.step_size)
        final_notional = quantity * price

        if normalized_multiplier <= 0:
            return RiskDecision(False, "ai_position_multiplier_zero")
        if quantity < filters.min_qty or quantity <= 0:
            return RiskDecision(False, f"quantity_below_min_qty:{quantity}")
        if final_notional < min_notional:
            return RiskDecision(False, f"final_notional_below_min_notional:{final_notional:.2f}")

        return RiskDecision(
            approved=True,
            reason="buy_order_approved",
            order=OrderRequest(symbol=symbol, side="BUY", order_type="MARKET", quantity=quantity),
        )

    def inspect_buy_decision(
        self,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        filters: SymbolFilters,
        signal_action: str,
        signal_reason: str,
        has_position: bool,
        position_value: float = 0.0,
        ai_assessment: AiRiskAssessment | None = None,
    ) -> BuyDecisionDiagnostic:
        quote_balance = account.balance_of(self.settings.quote_asset)
        ai_allow_entry = ai_assessment.allow_entry if ai_assessment is not None else True
        ai_risk_score = ai_assessment.risk_score if ai_assessment is not None else 0.0
        ai_position_multiplier = ai_assessment.position_multiplier if ai_assessment is not None else 1.0
        ai_veto_reason = ai_assessment.veto_reason if ai_assessment is not None else ""

        quote_budget = quote_balance * self.settings.risk_per_trade * min(1.0, max(0.0, ai_position_multiplier))
        notional = min(quote_budget, quote_balance)
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        raw_quantity = notional / price if price > 0 else 0.0
        quantity = self.client.quantize_quantity(raw_quantity, filters.step_size)
        final_notional = quantity * price

        blocker_details = []
        total_equity = quote_balance + max(0.0, position_value)
        current_position_fraction = position_value / total_equity if total_equity > 0 else 0.0
        cash_fraction = quote_balance / total_equity if total_equity > 0 else 0.0
        rebuild_allowed = (
            self.settings.cash_rebuild_enabled
            and has_position
            and signal_action == "BUY"
            and cash_fraction >= self.settings.cash_rebuild_min_cash_fraction
            and current_position_fraction < self.settings.cash_rebuild_max_position_fraction
        )
        eligible_signal = signal_action == "BUY" and (not has_position or rebuild_allowed)
        if signal_action != "BUY":
            blocker_details.append("当前策略信号不是买入")
        if has_position and not rebuild_allowed:
            blocker_details.append(
                f"当前已有持仓且仓位占比 {current_position_fraction:.2%}，"
                f"未满足现金补仓条件"
            )
        elif rebuild_allowed:
            blocker_details.append(
                f"允许现金补仓：现金占比 {cash_fraction:.2%}，仓位占比 {current_position_fraction:.2%}"
            )
        if not ai_allow_entry:
            blocker_details.append(f"AI 风险闸门否决入场：{ai_veto_reason or '未提供否决原因'}")
        elif ai_position_multiplier < 1.0:
            blocker_details.append(f"AI 风险闸门将仓位系数收缩到 {ai_position_multiplier:.2f}")

        eligible_risk = True
        min_notional_passed = final_notional >= min_notional
        if notional < min_notional:
            eligible_risk = False
            blocker_details.append("单次预算低于最小成交额")
        if quantity < filters.min_qty or quantity <= 0:
            eligible_risk = False
            blocker_details.append("按步进取整后的下单数量低于最小数量")
        if quantity > 0 and final_notional < min_notional:
            eligible_risk = False
            blocker_details.append("按步进取整后的最终成交额低于最小成交额")
        if not ai_allow_entry:
            eligible_risk = False

        eligible_to_buy = eligible_signal and eligible_risk
        blocker = "可以买入" if eligible_to_buy else (blocker_details[0] if blocker_details else "条件未满足")

        return BuyDecisionDiagnostic(
            symbol=symbol,
            signal_action=signal_action,
            signal_reason=signal_reason,
            has_position=has_position,
            quote_balance=quote_balance,
            quote_budget=quote_budget,
            effective_notional=notional,
            min_notional_required=min_notional,
            price=price,
            raw_quantity=raw_quantity,
            adjusted_quantity=quantity,
            final_notional=final_notional,
            min_notional_passed=min_notional_passed,
            min_qty=filters.min_qty,
            eligible_signal=eligible_signal,
            eligible_risk=eligible_risk,
            ai_allow_entry=ai_allow_entry,
            ai_risk_score=ai_risk_score,
            ai_position_multiplier=ai_position_multiplier,
            ai_veto_reason=ai_veto_reason,
            eligible_to_buy=eligible_to_buy,
            blocker=blocker,
            blocker_details=blocker_details,
        )

    def build_sell_order(
        self,
        symbol: str,
        price: float,
        base_asset_balance: float,
        filters: SymbolFilters,
        sell_fraction: float | None = None,
    ) -> RiskDecision:
        fraction = 1.0 if sell_fraction is None else self._normalized_fraction(sell_fraction)
        quantity = self.client.quantize_quantity(base_asset_balance * fraction, filters.step_size)
        final_notional = quantity * price
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        full_quantity = self.client.quantize_quantity(base_asset_balance, filters.step_size)
        full_notional = full_quantity * price
        if fraction <= 0:
            return RiskDecision(False, "sell_fraction_zero")
        if quantity < filters.min_qty or quantity <= 0:
            return RiskDecision(False, f"position_too_small_to_sell:{quantity}")
        if final_notional < min_notional:
            if full_quantity >= filters.min_qty and full_notional >= min_notional:
                quantity = full_quantity
                final_notional = full_notional
            else:
                return RiskDecision(False, f"sell_notional_below_min_notional:{final_notional:.2f}")
        if final_notional < min_notional:
            return RiskDecision(False, f"sell_notional_below_min_notional:{final_notional:.2f}")
        return RiskDecision(
            approved=True,
            reason="sell_order_approved",
            order=OrderRequest(symbol=symbol, side="SELL", order_type="MARKET", quantity=quantity),
        )

    def exit_sell_fraction(self, exit_reason: str | None, *, strategy_sell: bool = False) -> float:
        if exit_reason == "stop_loss":
            return self._normalized_fraction(self.settings.exit_stop_loss_fraction)
        if exit_reason == "emergency_stop":
            return self._normalized_fraction(self.settings.exit_emergency_stop_fraction)
        if exit_reason == "trailing_stop":
            return self._normalized_fraction(self.settings.exit_trailing_stop_fraction)
        if exit_reason == "take_profit":
            return self._normalized_fraction(self.settings.exit_take_profit_fraction)
        if exit_reason == "max_hold_exit":
            return self._normalized_fraction(self.settings.exit_max_hold_fraction)
        if strategy_sell:
            return self._normalized_fraction(self.settings.strategy_sell_fraction)
        return 1.0

    @staticmethod
    def _normalized_fraction(value: float) -> float:
        return min(1.0, max(0.0, float(value)))

    def inspect_sell_decision(
        self,
        *,
        symbol: str,
        price: float,
        position: PositionSnapshot | None,
        candles: Sequence[Candle],
        current_timestamp_ms: int,
        signal: TradeSignal,
        exit_reason: str | None,
        activation_decision: PositionActivationDecision | None = None,
    ) -> SellDecisionDiagnostic:
        if position is None or position.quantity <= 0:
            return SellDecisionDiagnostic(
                symbol=symbol,
                has_position=False,
                quantity=0.0,
                average_entry_price=0.0,
                mark_price=price,
                unrealized_pnl=0.0,
                unrealized_pnl_pct=0.0,
                strategy_signal=signal.action.value,
                strategy_reason=signal.reason,
                exit_reason="",
                stop_loss_price=0.0,
                take_profit_price=0.0,
                trailing_stop_price=0.0,
                max_hold_bars=self.settings.max_hold_bars,
                bars_held=0,
                activation_trigger=activation_decision.trigger if activation_decision else "",
                eligible_to_sell=False,
                recommended_sell_quantity=0.0,
                blocker="无持仓，不需要卖出",
                blocker_details=["无持仓，不需要卖出"],
            )

        position_diagnostic = self.build_position_diagnostic(
            symbol=symbol,
            price=price,
            position=position,
            candles=candles,
            current_timestamp_ms=current_timestamp_ms,
        )
        unrealized_pnl_pct = (
            (price - position.average_entry_price) / position.average_entry_price
            if position.average_entry_price > 0
            else 0.0
        )
        activation_trigger = activation_decision.trigger if activation_decision else ""
        activation_qty = activation_decision.quantity if activation_decision and activation_decision.action == "SELL" else 0.0
        strategy_sell = signal.action.value == "SELL"
        eligible_to_sell = bool(exit_reason or strategy_sell or activation_qty > 0)
        if exit_reason or strategy_sell:
            sell_fraction = self.exit_sell_fraction(exit_reason, strategy_sell=strategy_sell)
            recommended_sell_quantity = position.quantity * sell_fraction
        else:
            sell_fraction = 0.0
            recommended_sell_quantity = activation_qty
        blocker_details = []
        if exit_reason:
            blocker = f"规则退出触发：{exit_reason}"
            blocker_details.append(blocker)
            blocker_details.append(f"退出比例 {sell_fraction:.0%}")
        elif strategy_sell:
            blocker = "策略 SELL 触发"
            blocker_details.append(signal.reason)
            blocker_details.append(f"退出比例 {sell_fraction:.0%}")
        elif activation_qty > 0:
            blocker = f"仓位激活触发：{activation_trigger}"
            blocker_details.append(activation_decision.reason if activation_decision else blocker)
        else:
            blocker = "继续持有"
            blocker_details.append(position_diagnostic.exit_watch_reason)
            if activation_decision and activation_decision.reason:
                blocker_details.append(activation_decision.reason)
        dynamic_profile = self.dynamic_exit_profile(candles)
        blocker_details.append(dynamic_profile.explanation)

        return SellDecisionDiagnostic(
            symbol=symbol,
            has_position=True,
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            mark_price=price,
            unrealized_pnl=position.quantity * (price - position.average_entry_price),
            unrealized_pnl_pct=unrealized_pnl_pct,
            strategy_signal=signal.action.value,
            strategy_reason=signal.reason,
            exit_reason=exit_reason or "",
            stop_loss_price=position_diagnostic.stop_loss_price,
            take_profit_price=position_diagnostic.take_profit_price,
            trailing_stop_price=position_diagnostic.trailing_stop_price,
            max_hold_bars=self.settings.max_hold_bars,
            bars_held=position_diagnostic.bars_held,
            activation_trigger=activation_trigger,
            eligible_to_sell=eligible_to_sell,
            recommended_sell_quantity=recommended_sell_quantity,
            blocker=blocker,
            blocker_details=blocker_details,
        )

    def determine_exit_reason(
        self,
        price: float,
        position: PositionSnapshot,
        candles: Sequence[Candle],
        current_timestamp_ms: int,
    ) -> str | None:
        entry_price = position.average_entry_price
        highest_price = max(position.highest_price or entry_price, price)
        stop_loss_price, take_profit_price, trailing_stop_price = self._fee_adjusted_exit_prices(
            entry_price,
            highest_price,
            candles=candles,
        )

        bars_held = 0
        if position.entry_candle_close_time > 0:
            bars_held = sum(
                1
                for candle in candles
                if position.entry_candle_close_time < candle.close_time <= current_timestamp_ms
            )

        if price <= stop_loss_price:
            return "stop_loss"
        if price >= take_profit_price:
            return "take_profit"
        if highest_price > entry_price and price <= trailing_stop_price:
            return "trailing_stop"
        if self.settings.max_hold_bars > 0 and bars_held >= self.settings.max_hold_bars:
            return "max_hold_exit"
        return None

    def is_emergency_stop(
        self,
        price: float,
        position: PositionSnapshot,
        candles: Sequence[Candle],
    ) -> bool:
        if not self.settings.emergency_stop_confirmation_enabled:
            return False
        entry_price = position.average_entry_price
        if entry_price <= 0:
            return False
        highest_price = max(position.highest_price or entry_price, price)
        stop_loss_price, _, _ = self._fee_adjusted_exit_prices(entry_price, highest_price, candles=candles)
        profile = self.dynamic_exit_profile(candles)
        emergency_price = entry_price * (1.0 - max(profile.stop_loss_pct * 1.65, profile.stop_loss_pct + 0.006))
        severe_price_break = price <= emergency_price
        confirmed_deterioration = (
            price <= stop_loss_price
            and profile.trend_score <= -0.55
            and profile.volatility_ratio >= 1.05
        )
        return severe_price_break or confirmed_deterioration

    def _fee_adjusted_exit_prices(
        self,
        entry_price: float,
        highest_price: float,
        candles: Sequence[Candle] | None = None,
    ) -> tuple[float, float, float]:
        profile = self.dynamic_exit_profile(candles or [])
        trading_fee_rate = getattr(self.settings, "trading_fee_rate", 0.0)
        sell_net_multiplier = max(0.000001, 1.0 - max(0.0, trading_fee_rate))
        stop_loss_price = entry_price * (1.0 - profile.stop_loss_pct) / sell_net_multiplier
        take_profit_price = entry_price * (1.0 + profile.take_profit_pct) / sell_net_multiplier
        trailing_stop_price = highest_price * (1.0 - profile.trailing_stop_pct) / sell_net_multiplier
        return stop_loss_price, take_profit_price, trailing_stop_price

    @staticmethod
    def _clamp(value: float, lower: float, upper: float) -> float:
        return min(upper, max(lower, value))

    @staticmethod
    def _true_range(current: Candle, previous_close: float | None) -> float:
        if previous_close is None:
            return max(0.0, current.high - current.low)
        return max(
            max(0.0, current.high - current.low),
            abs(current.high - previous_close),
            abs(current.low - previous_close),
        )

    def dynamic_exit_profile(self, candles: Sequence[Candle]) -> DynamicExitProfile:
        base_stop = self.settings.stop_loss_pct
        base_take = self.settings.take_profit_pct
        base_trailing = self.settings.trailing_stop_pct
        dynamic_enabled = getattr(self.settings, "dynamic_exit_enabled", True)
        if not dynamic_enabled or len(candles) < 6:
            return DynamicExitProfile(
                stop_loss_pct=base_stop,
                take_profit_pct=base_take,
                trailing_stop_pct=base_trailing,
                stop_loss_multiplier=1.0,
                take_profit_multiplier=1.0,
                trailing_stop_multiplier=1.0,
                trend_score=0.0,
                volatility_ratio=1.0,
                volume_ratio=1.0,
                strong_trend=False,
                explanation="动态退出未启用或样本不足，使用基础止损止盈线",
            )

        closes = [candle.close for candle in candles if candle.close > 0]
        if len(closes) < 6:
            return DynamicExitProfile(
                stop_loss_pct=base_stop,
                take_profit_pct=base_take,
                trailing_stop_pct=base_trailing,
                stop_loss_multiplier=1.0,
                take_profit_multiplier=1.0,
                trailing_stop_multiplier=1.0,
                trend_score=0.0,
                volatility_ratio=1.0,
                volume_ratio=1.0,
                strong_trend=False,
                explanation="动态退出样本不足，使用基础止损止盈线",
            )

        price = closes[-1]
        fast_window = min(6, len(closes))
        slow_window = min(18, len(closes))
        fast_ma = sum(closes[-fast_window:]) / fast_window
        slow_ma = sum(closes[-slow_window:]) / slow_window
        ma_gap_pct = (fast_ma - slow_ma) / price if price > 0 else 0.0
        recent_change_pct = (closes[-1] - closes[-fast_window]) / closes[-fast_window] if closes[-fast_window] > 0 else 0.0
        trend_unit = max(0.000001, getattr(self.settings, "dynamic_exit_strong_trend_threshold", 0.004))
        trend_score = self._clamp((ma_gap_pct + recent_change_pct * 0.5) / trend_unit, -1.0, 1.0)

        ranges = []
        previous_close = None
        for candle in candles:
            ranges.append(self._true_range(candle, previous_close) / candle.close if candle.close > 0 else 0.0)
            previous_close = candle.close
        recent_ranges = [value for value in ranges[-fast_window:] if value > 0]
        baseline_ranges = [value for value in ranges[-slow_window:] if value > 0]
        recent_atr = sum(recent_ranges) / len(recent_ranges) if recent_ranges else 0.0
        baseline_atr = sum(baseline_ranges) / len(baseline_ranges) if baseline_ranges else recent_atr
        volatility_ratio = self._clamp(recent_atr / baseline_atr if baseline_atr > 0 else 1.0, 0.7, 1.4)

        volume_lookback = max(3, min(getattr(self.settings, "dynamic_exit_volume_lookback", 20), len(candles)))
        recent_volume = sum(candle.volume for candle in candles[-fast_window:]) / fast_window
        baseline_volume = sum(candle.volume for candle in candles[-volume_lookback:]) / volume_lookback
        volume_ratio = self._clamp(recent_volume / baseline_volume if baseline_volume > 0 else 1.0, 0.7, 1.4)

        min_multiplier = getattr(self.settings, "dynamic_exit_min_multiplier", 0.75)
        max_multiplier = getattr(self.settings, "dynamic_exit_max_multiplier", 1.35)
        stop_max_multiplier = min(getattr(self.settings, "dynamic_stop_max_multiplier", 1.25), max_multiplier)
        strong_trend = trend_score >= 0.55 and volume_ratio >= 0.95
        take_profit_multiplier = self._clamp(
            1.0
            + 0.28 * trend_score
            + 0.14 * (volatility_ratio - 1.0)
            + 0.10 * (volume_ratio - 1.0),
            min_multiplier,
            max_multiplier,
        )
        stop_loss_multiplier = self._clamp(
            1.0
            + 0.14 * (volatility_ratio - 1.0)
            - 0.12 * max(-trend_score, 0.0),
            min_multiplier,
            stop_max_multiplier,
        )
        trailing_stop_multiplier = self._clamp(
            1.0
            + 0.22 * max(trend_score, 0.0)
            + 0.16 * (volatility_ratio - 1.0)
            + 0.08 * (volume_ratio - 1.0),
            min_multiplier,
            max_multiplier,
        )
        if strong_trend:
            take_profit_multiplier = self._clamp(take_profit_multiplier * 1.08, min_multiplier, max_multiplier)
            trailing_stop_multiplier = self._clamp(trailing_stop_multiplier * 1.10, min_multiplier, max_multiplier)

        explanation = (
            f"动态退出：趋势 {trend_score:.2f}，波动 {volatility_ratio:.2f}，量能 {volume_ratio:.2f}；"
            f"止损系数 {stop_loss_multiplier:.2f}，止盈系数 {take_profit_multiplier:.2f}，"
            f"跟踪系数 {trailing_stop_multiplier:.2f}"
        )
        if strong_trend:
            explanation += "；强势/阶梯上涨，放宽止盈与跟踪区间"

        return DynamicExitProfile(
            stop_loss_pct=base_stop * stop_loss_multiplier,
            take_profit_pct=base_take * take_profit_multiplier,
            trailing_stop_pct=base_trailing * trailing_stop_multiplier,
            stop_loss_multiplier=stop_loss_multiplier,
            take_profit_multiplier=take_profit_multiplier,
            trailing_stop_multiplier=trailing_stop_multiplier,
            trend_score=trend_score,
            volatility_ratio=volatility_ratio,
            volume_ratio=volume_ratio,
            strong_trend=strong_trend,
            explanation=explanation,
        )

    def build_position_diagnostic(
        self,
        symbol: str,
        price: float,
        position: PositionSnapshot,
        candles: Sequence[Candle],
        current_timestamp_ms: int,
    ) -> PositionDiagnostic:
        entry_price = position.average_entry_price
        highest_price = max(position.highest_price or entry_price, price)
        stop_loss_price, take_profit_price, trailing_stop_price = self._fee_adjusted_exit_prices(
            entry_price,
            highest_price,
            candles=candles,
        )
        bars_held = 0
        if position.entry_candle_close_time > 0:
            bars_held = sum(
                1
                for candle in candles
                if position.entry_candle_close_time < candle.close_time <= current_timestamp_ms
            )
        exit_watch_reason = (
            self.determine_exit_reason(
                price=price,
                position=position,
                candles=candles,
                current_timestamp_ms=current_timestamp_ms,
            )
            or "持仓观察中"
        )
        return PositionDiagnostic(
            symbol=symbol,
            quantity=position.quantity,
            average_entry_price=entry_price,
            mark_price=price,
            highest_price=highest_price,
            unrealized_pnl=position.quantity * (price - entry_price),
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            trailing_stop_price=trailing_stop_price,
            bars_held=bars_held,
            opened_at_ms=position.opened_at_ms,
            entry_candle_close_time=position.entry_candle_close_time,
            exit_watch_reason=exit_watch_reason,
        )

    def base_asset_for_symbol(self, symbol: str) -> Optional[str]:
        if symbol.endswith(self.settings.quote_asset):
            return symbol[: -len(self.settings.quote_asset)]
        return None
