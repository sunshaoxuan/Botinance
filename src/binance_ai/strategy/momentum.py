from __future__ import annotations

from statistics import mean
from typing import Mapping, Sequence

from binance_ai.models import Candle, SignalAction, TradeSignal
from binance_ai.strategy.base import Strategy


class MovingAverageMomentumStrategy(Strategy):
    def __init__(
        self,
        main_interval: str,
        fast_window: int,
        slow_window: int,
        entry_interval: str,
        entry_fast_window: int,
        entry_slow_window: int,
        trend_interval: str,
        trend_fast_window: int,
        trend_slow_window: int,
    ) -> None:
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")
        if entry_fast_window >= entry_slow_window:
            raise ValueError("entry_fast_window must be smaller than entry_slow_window")
        if trend_fast_window >= trend_slow_window:
            raise ValueError("trend_fast_window must be smaller than trend_slow_window")

        self.main_interval = main_interval
        self.main_fast_window = fast_window
        self.main_slow_window = slow_window
        self.entry_interval = entry_interval
        self.entry_fast_window = entry_fast_window
        self.entry_slow_window = entry_slow_window
        self.trend_interval = trend_interval
        self.trend_fast_window = trend_fast_window
        self.trend_slow_window = trend_slow_window

    def generate(
        self,
        symbol: str,
        candles_by_interval: Mapping[str, Sequence[Candle]],
        has_position: bool,
    ) -> TradeSignal:
        main_interval, main_candles = self._pick_main_interval(candles_by_interval, self.main_interval)
        entry_candles = candles_by_interval.get(self.entry_interval, ())
        trend_candles = candles_by_interval.get(self.trend_interval, ())

        main_signal = self._compute_cross_signal(
            symbol=symbol,
            candles=main_candles,
            fast_window=self.main_fast_window,
            slow_window=self.main_slow_window,
            label=main_interval,
        )
        if main_signal is None:
            needed = self.main_slow_window + 1
            return TradeSignal(
                symbol=symbol,
                action=SignalAction.HOLD,
                confidence=0.0,
                reason=f"insufficient_{main_interval}_candles:{len(main_candles)}/{needed}",
                regime="数据不足",
            )

        entry_signal = self._compute_cross_signal(
            symbol=symbol,
            candles=entry_candles,
            fast_window=self.entry_fast_window,
            slow_window=self.entry_slow_window,
            label=self.entry_interval,
        )
        trend_state = self._compute_trend_state(
            candles=trend_candles,
            fast_window=self.trend_fast_window,
            slow_window=self.trend_slow_window,
        )

        action = SignalAction.HOLD
        regime = trend_state["regime"]
        confidence = min(
            1.0,
            max(
                main_signal["confidence"],
                entry_signal["confidence"] if entry_signal is not None else 0.0,
                trend_state["confidence"],
            ),
        )

        if main_signal["action"] == SignalAction.BUY and not has_position:
            if trend_state["bullish"] and entry_signal is not None and entry_signal["fast_now"] >= entry_signal["slow_now"]:
                action = SignalAction.BUY
                reason = (
                    f"mtf_buy {main_interval}=bullish_cross "
                    f"{self.trend_interval}=uptrend {self.entry_interval}=momentum_ok "
                    f"main_fast={main_signal['fast_now']:.4f} main_slow={main_signal['slow_now']:.4f}"
                )
                return TradeSignal(symbol=symbol, action=action, confidence=confidence, reason=reason, regime=regime)
            reason = (
                f"buy_filtered trend={trend_state['regime']} "
                f"entry={self._entry_state_text(entry_signal)} "
                f"main_fast={main_signal['fast_now']:.4f} main_slow={main_signal['slow_now']:.4f}"
            )
            return TradeSignal(symbol=symbol, action=SignalAction.HOLD, confidence=confidence, reason=reason, regime=regime)

        if has_position:
            if main_signal["action"] == SignalAction.SELL:
                reason = (
                    f"mtf_sell {main_interval}=bearish_cross "
                    f"main_fast={main_signal['fast_now']:.4f} main_slow={main_signal['slow_now']:.4f}"
                )
                return TradeSignal(symbol=symbol, action=SignalAction.SELL, confidence=confidence, reason=reason, regime=regime)
            if not trend_state["bullish"] and entry_signal is not None and entry_signal["fast_now"] < entry_signal["slow_now"]:
                reason = (
                    f"mtf_sell_filter {self.trend_interval}=weak "
                    f"{self.entry_interval}=down_momentum "
                    f"entry_fast={entry_signal['fast_now']:.4f} entry_slow={entry_signal['slow_now']:.4f}"
                )
                return TradeSignal(symbol=symbol, action=SignalAction.SELL, confidence=confidence, reason=reason, regime=regime)

        return TradeSignal(
            symbol=symbol,
            action=SignalAction.HOLD,
            confidence=confidence,
            reason=(
                f"mtf_hold {main_interval}={main_signal['state']} "
                f"{self.trend_interval}={trend_state['regime']} "
                f"{self.entry_interval}={self._entry_state_text(entry_signal)}"
            ),
            regime=regime,
        )

    @staticmethod
    def _pick_main_interval(
        candles_by_interval: Mapping[str, Sequence[Candle]],
        main_interval: str,
    ) -> tuple[str, Sequence[Candle]]:
        if not candles_by_interval:
            return main_interval, ()
        if main_interval in candles_by_interval:
            return main_interval, candles_by_interval[main_interval]
        interval = next(iter(candles_by_interval))
        return interval, candles_by_interval[interval]

    @staticmethod
    def _compute_cross_signal(
        *,
        symbol: str,
        candles: Sequence[Candle],
        fast_window: int,
        slow_window: int,
        label: str,
    ) -> dict[str, object] | None:
        needed = slow_window + 1
        if len(candles) < needed:
            return None

        closes = [candle.close for candle in candles]
        fast_prev = mean(closes[-fast_window - 1 : -1])
        fast_now = mean(closes[-fast_window:])
        slow_prev = mean(closes[-slow_window - 1 : -1])
        slow_now = mean(closes[-slow_window:])
        bullish_cross = fast_prev <= slow_prev and fast_now > slow_now
        bearish_cross = fast_prev >= slow_prev and fast_now < slow_now
        distance = abs(fast_now - slow_now) / slow_now if slow_now else 0.0
        confidence = min(distance * 100, 1.0)
        state = "above" if fast_now > slow_now else "below" if fast_now < slow_now else "flat"

        action = SignalAction.HOLD
        if bullish_cross:
            action = SignalAction.BUY
        elif bearish_cross:
            action = SignalAction.SELL

        return {
            "symbol": symbol,
            "interval": label,
            "action": action,
            "confidence": confidence,
            "fast_now": fast_now,
            "slow_now": slow_now,
            "fast_prev": fast_prev,
            "slow_prev": slow_prev,
            "state": state,
        }

    @staticmethod
    def _compute_trend_state(
        *,
        candles: Sequence[Candle],
        fast_window: int,
        slow_window: int,
    ) -> dict[str, object]:
        needed = slow_window
        if len(candles) < needed:
            return {"bullish": False, "regime": "trend_unknown", "confidence": 0.0}

        closes = [candle.close for candle in candles]
        fast_now = mean(closes[-fast_window:])
        slow_now = mean(closes[-slow_window:])
        bullish = fast_now >= slow_now
        distance = abs(fast_now - slow_now) / slow_now if slow_now else 0.0
        confidence = min(distance * 100, 1.0)
        regime = "uptrend" if bullish else "downtrend"
        return {
            "bullish": bullish,
            "regime": regime,
            "confidence": confidence,
            "fast_now": fast_now,
            "slow_now": slow_now,
        }

    @staticmethod
    def _entry_state_text(entry_signal: dict[str, object] | None) -> str:
        if entry_signal is None:
            return "entry_unknown"
        fast_now = float(entry_signal["fast_now"])
        slow_now = float(entry_signal["slow_now"])
        if fast_now > slow_now:
            return "momentum_up"
        if fast_now < slow_now:
            return "momentum_down"
        return "momentum_flat"
