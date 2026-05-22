from __future__ import annotations

from dataclasses import asdict
from statistics import median
from typing import Sequence

from binance_ai.config import Settings
from binance_ai.models import (
    AiRiskAssessment,
    Candle,
    DirectionDecision,
    FairValueSummary,
    ManagedOrder,
    SignalAction,
    TradeSignal,
)
from binance_ai.target_inventory import TargetInventoryDecision


class FairValueEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def evaluate(self, *, symbol: str, price: float, candles: Sequence[Candle]) -> FairValueSummary:
        window_size = max(5, min(len(candles), self.settings.fair_value_lookback_bars))
        window = list(candles[-window_size:])
        if not window:
            return self._fallback(symbol=symbol, price=price)

        closes = [c.close for c in window if c.close > 0]
        typical_prices = [(c.high + c.low + c.close) / 3.0 for c in window if c.close > 0]
        volumes = [max(0.0, c.volume) for c in window if c.close > 0]
        ema_value = self._ema(closes) if closes else price
        vwap_value = self._vwap(typical_prices, volumes, fallback=ema_value)
        range_midpoint = (max(c.high for c in window) + min(c.low for c in window)) / 2.0
        method = self.settings.fair_value_method
        if method == "ema":
            fair_value = ema_value
        elif method == "vwap":
            fair_value = vwap_value
        elif method == "range_midpoint":
            fair_value = range_midpoint
        else:
            fair_value = (ema_value * 0.45) + (vwap_value * 0.35) + (range_midpoint * 0.20)

        atr = self._atr(window)
        atr_pct = atr / fair_value if fair_value > 0 else 0.0
        volatility_buffer_pct = max(0.0, atr_pct * self.settings.volatility_buffer_atr_multiplier)
        buy_discount_pct = max(self.settings.buy_zone_min_discount_pct, self.settings.min_pair_net_edge_pct * 0.5) + volatility_buffer_pct
        sell_premium_pct = max(self.settings.sell_zone_min_premium_pct, self.settings.min_pair_net_edge_pct * 0.5) + volatility_buffer_pct
        return FairValueSummary(
            symbol=symbol,
            method=method,
            current_price=round(price, 8),
            fair_value=round(fair_value, 8),
            ema_value=round(ema_value, 8),
            vwap_value=round(vwap_value, 8),
            range_midpoint=round(range_midpoint, 8),
            atr=round(atr, 8),
            atr_pct=round(atr_pct, 8),
            buy_zone_price=round(fair_value * (1.0 - buy_discount_pct), 8),
            sell_zone_price=round(fair_value * (1.0 + sell_premium_pct), 8),
            buy_discount_pct=round(buy_discount_pct, 8),
            sell_premium_pct=round(sell_premium_pct, 8),
            volatility_buffer_pct=round(volatility_buffer_pct, 8),
            lookback_bars=window_size,
        )

    @staticmethod
    def _fallback(*, symbol: str, price: float) -> FairValueSummary:
        return FairValueSummary(
            symbol=symbol,
            method="fallback",
            current_price=price,
            fair_value=price,
            ema_value=price,
            vwap_value=price,
            range_midpoint=price,
            atr=0.0,
            atr_pct=0.0,
            buy_zone_price=price,
            sell_zone_price=price,
            buy_discount_pct=0.0,
            sell_premium_pct=0.0,
            volatility_buffer_pct=0.0,
            lookback_bars=0,
        )

    @staticmethod
    def _ema(values: Sequence[float]) -> float:
        if not values:
            return 0.0
        alpha = 2.0 / (len(values) + 1.0)
        ema = values[0]
        for value in values[1:]:
            ema = value * alpha + ema * (1.0 - alpha)
        return ema

    @staticmethod
    def _vwap(prices: Sequence[float], volumes: Sequence[float], *, fallback: float) -> float:
        notional = sum(price * volume for price, volume in zip(prices, volumes))
        volume_sum = sum(volumes)
        if volume_sum <= 0:
            return fallback
        return notional / volume_sum

    @staticmethod
    def _atr(candles: Sequence[Candle]) -> float:
        if not candles:
            return 0.0
        ranges = []
        previous_close = candles[0].close
        for candle in candles:
            ranges.append(max(candle.high - candle.low, abs(candle.high - previous_close), abs(candle.low - previous_close)))
            previous_close = candle.close
        return sum(ranges) / max(1, len(ranges))


class MarketRegimeClassifier:
    def classify(self, candles: Sequence[Candle], ai_assessment: AiRiskAssessment) -> str:
        if ai_assessment.risk_score >= 0.9 or "极端" in ai_assessment.veto_reason or "extreme" in ai_assessment.veto_reason.lower():
            return "PANIC"
        if len(candles) < 8:
            return "RANGE"
        trend = self._change(candles, 30)
        momentum = self._change(candles, 6)
        ranges = [(c.high - c.low) / c.close for c in candles[-20:] if c.close > 0]
        volatility = median(ranges) if ranges else 0.0
        if trend > 0.008 and momentum > 0.002:
            return "TREND_UP"
        if trend < -0.008 and momentum < -0.002:
            return "TREND_DOWN"
        if abs(momentum) > max(0.006, volatility * 2.5):
            return "BREAKOUT"
        return "RANGE"

    @staticmethod
    def _change(candles: Sequence[Candle], bars: int) -> float:
        if len(candles) < 2:
            return 0.0
        start = candles[-min(len(candles), max(2, bars))]
        end = candles[-1]
        if start.close <= 0:
            return 0.0
        return end.close / start.close - 1.0


class PairedTradePlanner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def buy_exit_price(self, buy_price: float) -> float:
        return buy_price * (1.0 + self.required_roundtrip_edge_pct())

    def sell_buyback_price(self, sell_price: float) -> float:
        return sell_price * (1.0 - self.required_roundtrip_edge_pct())

    def required_roundtrip_edge_pct(self) -> float:
        return max(0.0, self.settings.trading_fee_rate * 2.0 + self.settings.min_pair_net_edge_pct)

    def expected_net_edge_pct(self, *, side: str, price: float, fair_value: FairValueSummary) -> float:
        required_fee = max(0.0, self.settings.trading_fee_rate * 2.0)
        if side.upper() == "BUY" and price > 0:
            gross = max(0.0, fair_value.sell_zone_price / price - 1.0)
            return gross - required_fee
        if side.upper() == "SELL" and fair_value.buy_zone_price > 0:
            gross = max(0.0, price / fair_value.buy_zone_price - 1.0)
            return gross - required_fee
        return 0.0


class DirectionDecisionEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.fair_value = FairValueEngine(settings)
        self.regime = MarketRegimeClassifier()
        self.pairs = PairedTradePlanner(settings)

    def evaluate(
        self,
        *,
        symbol: str,
        price: float,
        candles: Sequence[Candle],
        signal: TradeSignal,
        target_inventory: TargetInventoryDecision,
        ai_assessment: AiRiskAssessment,
        open_orders: Sequence[ManagedOrder],
        exit_reason: str | None,
    ) -> DirectionDecision:
        fair = self.fair_value.evaluate(symbol=symbol, price=price, candles=candles)
        regime = self.regime.classify(candles, ai_assessment)
        allow_risk_exit = bool(exit_reason and self.settings.allow_risk_sell_below_sell_zone)
        price_zone = self._price_zone(price, fair)
        buy_edge = self.pairs.expected_net_edge_pct(side="BUY", price=price, fair_value=fair)
        sell_edge = self.pairs.expected_net_edge_pct(side="SELL", price=price, fair_value=fair)
        buy_limit_price, buy_limit_edge = self._resting_limit_edge(side="BUY", price=price, fair=fair)
        sell_limit_price, sell_limit_edge = self._resting_limit_edge(side="SELL", price=price, fair=fair)
        required = max(0.0, self.settings.min_pair_net_edge_pct)
        has_open_buy = any(order.side.upper() == "BUY" for order in open_orders)
        has_open_sell = any(order.side.upper() == "SELL" for order in open_orders)
        buy_limit_in_zone = buy_limit_price > 0 and buy_limit_price <= fair.buy_zone_price
        sell_limit_in_zone = sell_limit_price > 0 and sell_limit_price >= fair.sell_zone_price
        allow_buy = (
            (price_zone == "BUY_ZONE" or buy_limit_in_zone)
            and target_inventory.available_buy_notional > 0
            and max(buy_edge, buy_limit_edge) >= required
            and not has_open_buy
            and ai_assessment.allow_entry
        )
        allow_sell = (
            (price_zone == "SELL_ZONE" or sell_limit_in_zone)
            and target_inventory.allowed_sell_quantity > 0
            and max(sell_edge, sell_limit_edge) >= required
            and not has_open_sell
        )
        blockers = []
        if target_inventory.available_buy_notional > 0 and price_zone != "BUY_ZONE" and not buy_limit_in_zone:
            blockers.append("当前价和首档买入挂单价都不在折价建仓区，拒绝追涨买入")
        if target_inventory.allowed_sell_quantity > 0 and price_zone != "SELL_ZONE" and not sell_limit_in_zone and not allow_risk_exit:
            blockers.append("当前价和首档卖出挂单价都不在溢价卖出区，拒绝杀跌卖出")
        if max(buy_edge, sell_edge, buy_limit_edge, sell_limit_edge) < required:
            blockers.append("预期净边际不足，扣除手续费后不值得交易")
        if has_open_buy:
            blockers.append("已有买入挂单，等待触价或重定价")
        if has_open_sell:
            blockers.append("已有卖出挂单，等待触价或重定价")
        if not ai_assessment.allow_entry and target_inventory.available_buy_notional > 0:
            blockers.append("AI 风险闸门不允许新增买入")

        action = "HOLD"
        if allow_risk_exit:
            action = "RISK_EXIT"
        elif allow_buy:
            action = "BUY"
        elif allow_sell:
            action = "SELL"

        paired_state = {
            "required_roundtrip_edge_pct": round(self.pairs.required_roundtrip_edge_pct(), 8),
            "buy_exit_price": round(self.pairs.buy_exit_price(price), 8),
            "sell_buyback_price": round(self.pairs.sell_buyback_price(price), 8),
            "buy_expected_net_edge_pct": round(buy_edge, 8),
            "sell_expected_net_edge_pct": round(sell_edge, 8),
            "resting_buy_limit_price": round(buy_limit_price, 8),
            "resting_sell_limit_price": round(sell_limit_price, 8),
            "resting_buy_expected_net_edge_pct": round(buy_limit_edge, 8),
            "resting_sell_expected_net_edge_pct": round(sell_limit_edge, 8),
            "resting_buy_limit_in_zone": buy_limit_in_zone,
            "resting_sell_limit_in_zone": sell_limit_in_zone,
        }
        reason = self._reason(
            action=action,
            mode=regime,
            price_zone=price_zone,
            price=price,
            fair=fair,
            buy_edge=buy_edge,
            sell_edge=sell_edge,
            blockers=blockers,
        )
        return DirectionDecision(
            symbol=symbol,
            mode=regime,
            recommended_action=action,
            price_zone=price_zone,
            current_price=round(price, 8),
            fair_value=fair.fair_value,
            buy_zone_price=fair.buy_zone_price,
            sell_zone_price=fair.sell_zone_price,
            expected_net_edge_pct=round(max(buy_edge, sell_edge, buy_limit_edge, sell_limit_edge), 8),
            allow_buy=allow_buy,
            allow_sell=allow_sell,
            allow_risk_exit=allow_risk_exit,
            reason_cn=reason,
            blockers=blockers,
            fair_value_summary=asdict(fair),
            paired_order_state=paired_state,
        )

    @staticmethod
    def _price_zone(price: float, fair: FairValueSummary) -> str:
        if price <= fair.buy_zone_price:
            return "BUY_ZONE"
        if price >= fair.sell_zone_price:
            return "SELL_ZONE"
        return "NEUTRAL_ZONE"

    def _resting_limit_edge(self, *, side: str, price: float, fair: FairValueSummary) -> tuple[float, float]:
        side = side.upper()
        tiers = self._tiers(self.settings.entry_ladder_tiers if side == "BUY" else self.settings.exit_ladder_tiers)
        offset = tiers[0][0] if tiers else self.settings.order_passive_offset_pct
        limit_price = price * (1.0 - offset) if side == "BUY" else price * (1.0 + offset)
        fee = max(0.0, self.settings.trading_fee_rate * 2.0)
        if side == "BUY" and limit_price > 0:
            return limit_price, max(0.0, fair.sell_zone_price / limit_price - 1.0) - fee
        if side == "SELL" and fair.buy_zone_price > 0:
            return limit_price, max(0.0, limit_price / fair.buy_zone_price - 1.0) - fee
        return limit_price, 0.0

    @staticmethod
    def _tiers(raw: str) -> list[tuple[float, float]]:
        tiers: list[tuple[float, float]] = []
        for item in str(raw or "").split(","):
            if ":" not in item:
                continue
            offset_raw, fraction_raw = item.split(":", 1)
            try:
                offset = max(0.0, float(offset_raw.strip()))
                fraction = max(0.0, float(fraction_raw.strip()))
            except ValueError:
                continue
            if fraction > 0:
                tiers.append((offset, fraction))
        return tiers

    @staticmethod
    def _reason(
        *,
        action: str,
        mode: str,
        price_zone: str,
        price: float,
        fair: FairValueSummary,
        buy_edge: float,
        sell_edge: float,
        blockers: Sequence[str],
    ) -> str:
        label = {
            "BUY": "允许折价建仓/补仓",
            "SELL": "允许溢价减仓/释放",
            "RISK_EXIT": "硬风险退出优先",
            "HOLD": "观望",
        }.get(action, action)
        detail = (
            f"{label}；模式 {mode}，当前价 {price:.4f}，公平价 {fair.fair_value:.4f}，"
            f"买入区 <= {fair.buy_zone_price:.4f}，卖出区 >= {fair.sell_zone_price:.4f}，"
            f"价格区间 {price_zone}，买入净边际 {buy_edge:.2%}，卖出净边际 {sell_edge:.2%}"
        )
        if blockers:
            detail = f"{detail}；阻塞：{'；'.join(blockers[:3])}"
        return detail
