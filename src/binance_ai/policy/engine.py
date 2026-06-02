from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Iterable, List, Sequence

from binance_ai.config import Settings
from binance_ai.models import (
    AiRiskAssessment,
    Candle,
    CompositeDecision,
    DirectionDecision,
    InventorySkewSummary,
    ManagedOrder,
    OrderProposal,
    PolicyDecision,
    ProposalFilterResult,
    ProtectionLock,
    ScenarioDecision,
    SignalAction,
    SymbolFilters,
    TradeSignal,
)
from binance_ai.target_inventory import TargetInventoryDecision
from binance_ai.trade_guard import TradeProfitabilityGuard


@dataclass(frozen=True)
class PolicyContext:
    symbol: str
    price: float
    candles: Sequence[Candle]
    signal: TradeSignal
    exit_reason: str | None
    has_position: bool
    base_balance: float
    quote_balance: float
    filters: SymbolFilters
    target_inventory: TargetInventoryDecision
    composite_decision: CompositeDecision
    ai_assessment: AiRiskAssessment
    open_orders: Sequence[ManagedOrder]
    activation_state: Dict[str, object]
    timestamp_ms: int
    direction_decision: DirectionDecision | None = None
    scenario_decision: ScenarioDecision | None = None
    pair_profitability_stats: Dict[str, object] | None = None
    position_average_entry_price: float = 0.0


class ProtectionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.guard = TradeProfitabilityGuard(settings)

    def evaluate(self, context: PolicyContext) -> List[ProtectionLock]:
        locks: List[ProtectionLock] = []
        pair_lock = self._pair_lock_after_risk_exit(context)
        if pair_lock is not None:
            locks.append(pair_lock)
        stoploss_guard = self._stoploss_guard(context)
        if stoploss_guard is not None:
            locks.append(stoploss_guard)
        drawdown_guard = self._drawdown_guard(context)
        if drawdown_guard is not None:
            locks.append(drawdown_guard)
        low_profit_pair_guard = self._low_profit_pair_guard(context)
        if low_profit_pair_guard is not None:
            locks.append(low_profit_pair_guard)
        if self._ai_extreme(context.ai_assessment):
            locks.append(
                ProtectionLock(
                    symbol=context.symbol,
                    lock_type="AI_EXTREME_RISK",
                    active=True,
                    reason_cn="AI 极端风险闸门触发，停止主动建仓和回补",
                    unlock_conditions=["AI 风险分数回落到极端阈值以下"],
                )
            )
        return locks

    def _low_profit_pair_guard(self, context: PolicyContext) -> ProtectionLock | None:
        stats = dict(context.pair_profitability_stats or {})
        edges = list(stats.get("completed_edges", []))
        lookback = max(1, self.settings.low_profit_pair_lookback)
        recent = edges[-lookback:]
        if len(recent) < lookback:
            return None
        average_edge = sum(float(value) for value in recent) / len(recent)
        if average_edge >= self.settings.low_profit_pair_min_avg_net_edge_pct:
            return None
        return ProtectionLock(
            symbol=context.symbol,
            lock_type="LOW_PROFIT_PAIR_LOCK",
            active=True,
            reason_cn=(
                f"最近 {lookback} 组完成 pair 平均净边际 {average_edge:.2%} 低于 "
                f"{self.settings.low_profit_pair_min_avg_net_edge_pct:.2%}"
            ),
            unlock_conditions=[f"等待 {self.settings.low_profit_pair_lock_candles} 根K线后重评估"],
            remaining_bars=max(0, self.settings.low_profit_pair_lock_candles),
        )

    def _pair_lock_after_risk_exit(self, context: PolicyContext) -> ProtectionLock | None:
        state = context.activation_state
        last_exit_price = self._float(state.get("last_risk_exit_price"))
        last_exit_ts = int(self._float(state.get("last_risk_exit_timestamp_ms")))
        unlock_price = self._float(state.get("risk_exit_reentry_price"))
        if last_exit_price <= 0 or last_exit_ts <= 0:
            return None

        interval_ms = self._interval_ms()
        lock_ms = interval_ms * max(0, self.settings.pair_lock_after_risk_exit_candles)
        remaining_bars = 0
        if lock_ms > 0 and context.timestamp_ms < last_exit_ts + lock_ms:
            remaining_bars = int((last_exit_ts + lock_ms - context.timestamp_ms + interval_ms - 1) // interval_ms)

        conditions: List[str] = []
        locked = False
        if remaining_bars > 0:
            locked = True
            conditions.append(f"等待风险退出冷却结束，还剩 {remaining_bars} 根K线")
        if self.settings.pair_lock_require_net_edge and unlock_price > 0 and context.price > unlock_price:
            locked = True
            conditions.append(f"当前价 {context.price:.4f} 高于净边际回补线 {unlock_price:.4f}")
        if self.settings.pair_lock_require_trend_stable and self._trend_pct(context.candles, 6) < -0.0015:
            locked = True
            conditions.append("短周期趋势仍在恶化")
        if not locked:
            return ProtectionLock(
                symbol=context.symbol,
                lock_type="PAIR_LOCK_AFTER_STOP",
                active=False,
                reason_cn="风险退出后的冷却、趋势和净边际条件已满足，可进入恢复入场",
                unlock_conditions=["已解锁"],
                reference_price=last_exit_price,
                unlock_price=unlock_price,
            )
        return ProtectionLock(
            symbol=context.symbol,
            lock_type="PAIR_LOCK_AFTER_STOP",
            active=True,
            reason_cn="风险退出后交易对锁定，禁止普通买入和高价重建",
            unlock_conditions=conditions,
            remaining_bars=remaining_bars,
            reference_price=last_exit_price,
            unlock_price=unlock_price,
        )

    def _stoploss_guard(self, context: PolicyContext) -> ProtectionLock | None:
        count = int(self._float(context.activation_state.get("partial_stop_count")))
        if count < max(1, self.settings.stoploss_guard_trade_limit):
            return None
        return ProtectionLock(
            symbol=context.symbol,
            lock_type="STOPLOSS_GUARD",
            active=True,
            reason_cn=f"连续风险退出次数 {count} 已达到保护阈值，停止主动交易",
            unlock_conditions=[f"等待 {self.settings.stoploss_guard_lock_candles} 根K线后重新评估"],
            remaining_bars=max(0, self.settings.stoploss_guard_lock_candles),
        )

    def _drawdown_guard(self, context: PolicyContext) -> ProtectionLock | None:
        daily_pnl = context.target_inventory.daily_realized_pnl
        equity = context.target_inventory.total_equity
        max_loss = equity * max(0.0, self.settings.max_drawdown_guard_pct)
        if max_loss <= 0 or daily_pnl > -max_loss:
            return None
        return ProtectionLock(
            symbol=context.symbol,
            lock_type="DRAWDOWN_GUARD",
            active=True,
            reason_cn=f"日内已实现亏损 {daily_pnl:.2f} 已超过保护线 {-max_loss:.2f}",
            unlock_conditions=["下一个交易日或人工复核后解除"],
        )

    def _ai_extreme(self, assessment: AiRiskAssessment) -> bool:
        text = f"{assessment.status} {assessment.veto_reason}".lower()
        return assessment.risk_score >= 0.9 or "extreme" in text or "极端" in text

    def _trend_pct(self, candles: Sequence[Candle], bars: int) -> float:
        if len(candles) < 2:
            return 0.0
        start = candles[-min(len(candles), max(2, bars))]
        end = candles[-1]
        if start.close <= 0:
            return 0.0
        return end.close / start.close - 1.0

    def _interval_ms(self) -> int:
        raw = str(self.settings.kline_interval).strip().lower()
        try:
            value = int(raw[:-1])
        except (TypeError, ValueError):
            return 60 * 60 * 1000
        if raw.endswith("m"):
            return value * 60 * 1000
        if raw.endswith("h"):
            return value * 60 * 60 * 1000
        if raw.endswith("d"):
            return value * 24 * 60 * 60 * 1000
        return 60 * 60 * 1000

    @staticmethod
    def _float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0


class InventorySkewOrderProposalEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def summarize(self, context: PolicyContext) -> InventorySkewSummary:
        target = context.target_inventory
        band = max(0.01, target.upper_fraction - target.lower_fraction)
        skew = (target.current_fraction - target.target_fraction) / band
        if not self.settings.inventory_skew_enabled:
            return InventorySkewSummary(
                symbol=context.symbol,
                current_fraction=round(target.current_fraction, 6),
                target_fraction=round(target.target_fraction, 6),
                lower_fraction=round(target.lower_fraction, 6),
                upper_fraction=round(target.upper_fraction, 6),
                skew=round(skew, 6),
                buy_weight=1.0,
                sell_weight=1.0,
                reason_cn="库存偏移关闭，买卖权重保持 1.00",
            )
        multiplier = max(0.0, self.settings.inventory_range_multiplier)
        buy_weight = self._clamp(1.0 - max(0.0, skew) * multiplier)
        sell_weight = self._clamp(1.0 + max(0.0, skew) * min(multiplier, 1.0))
        if skew < 0:
            buy_weight = self._clamp(1.0 + abs(skew) * min(multiplier, 1.0))
            sell_weight = self._clamp(1.0 - abs(skew) * multiplier)
        return InventorySkewSummary(
            symbol=context.symbol,
            current_fraction=round(target.current_fraction, 6),
            target_fraction=round(target.target_fraction, 6),
            lower_fraction=round(target.lower_fraction, 6),
            upper_fraction=round(target.upper_fraction, 6),
            skew=round(skew, 6),
            buy_weight=round(buy_weight, 6),
            sell_weight=round(sell_weight, 6),
            reason_cn=(
                f"当前仓位 {target.current_fraction:.1%}，目标 {target.target_fraction:.1%}，"
                f"买入权重 {buy_weight:.2f}，卖出权重 {sell_weight:.2f}"
            ),
        )

    def generate(self, context: PolicyContext, policy_state: str, skew: InventorySkewSummary) -> List[OrderProposal]:
        initial_release = self._initial_inventory_release(context)
        if initial_release > 0 and context.has_position and context.target_inventory.allowed_sell_quantity > 0:
            cash_deficit = max(0.0, context.target_inventory.min_cash_reserve - context.quote_balance)
            target_notional = max(self.settings.order_target_notional, cash_deficit)
            quantity = min(
                context.base_balance,
                context.target_inventory.allowed_sell_quantity,
                initial_release,
                target_notional / context.price if context.price > 0 else 0.0,
            )
            if quantity > 0:
                return [
                    OrderProposal(
                        symbol=context.symbol,
                        side="SELL",
                        trigger="initial_inventory_release_sell",
                        ladder_group="initial_release",
                        quantity=quantity,
                        notional=quantity * context.price,
                        urgent=True,
                        tier_index=0,
                        target_spread_pct=0.0,
                        target_fraction=1.0,
                        score=max(context.composite_decision.sell_score, 1.0),
                        reason_cn="初始真实库存释放，首笔成交盈亏不计入 Boti 操作盈亏",
                        source="initial_inventory_release",
                    )
                ]

        if policy_state == "RISK_REDUCTION" and context.has_position and context.exit_reason:
            if context.exit_reason == "emergency_stop":
                sell_fraction = self._emergency_stop_fraction(context)
            elif context.exit_reason == "stop_loss":
                sell_fraction = self.settings.exit_stop_loss_fraction
            elif context.exit_reason == "take_profit":
                sell_fraction = self.settings.exit_take_profit_fraction
            elif context.exit_reason == "trailing_stop":
                sell_fraction = self.settings.exit_trailing_stop_fraction
            elif context.exit_reason == "max_hold_exit":
                sell_fraction = self.settings.exit_max_hold_fraction
            else:
                sell_fraction = 1.0
            quantity = context.base_balance * min(1.0, max(0.0, sell_fraction))
            return [
                OrderProposal(
                    symbol=context.symbol,
                    side="SELL",
                    trigger=context.exit_reason,
                    ladder_group="risk_exit",
                    quantity=quantity,
                    notional=quantity * context.price,
                    urgent=True,
                    score=context.composite_decision.risk_score,
                    reason_cn=f"{context.exit_reason} 进入风险降低模式，生成保护性卖出提案",
                    source="risk",
                    pair_role="ask",
                )
            ]

        if policy_state == "RECOVERY_PROBE_ENTRY":
            probe = self._recovery_probe_proposal(context)
            return [probe] if probe is not None else []

        if not self.settings.pair_market_making_enabled:
            return []

        scenario = context.scenario_decision
        if scenario is not None and not scenario.generate_new_orders:
            return []
        pending_buyback = self._effective_pending_buyback(context)
        cooldown_until = int(self._float(context.activation_state.get("buyback_cooldown_until_candle")))
        cooldown_active = cooldown_until > context.timestamp_ms
        risk_exit_reentry_price = self._float(context.activation_state.get("risk_exit_reentry_price"))
        risk_reentry_blocked = risk_exit_reentry_price > 0 and context.price > risk_exit_reentry_price
        if pending_buyback > 0:
            counter = self._counter_buyback_proposal(context, pending_buyback)
            return [counter] if counter is not None else []
        if cooldown_active:
            return []

        price_reference = (
            float(context.direction_decision.fair_value)
            if context.direction_decision is not None and context.direction_decision.fair_value > 0
            else context.price
        )
        spreads = self._spread_levels(context)
        proposals: List[OrderProposal] = []
        allow_buy_pairs = policy_state in {"PAIR_LOCKED_AFTER_STOP", "RECOVERY_ENTRY", "INVENTORY_REBALANCE", "MARKET_MAKING"}
        allow_sell_pairs = policy_state in {"INVENTORY_REBALANCE", "MARKET_MAKING"}
        if context.target_inventory.current_fraction < context.target_inventory.lower_fraction:
            allow_sell_pairs = False
        elif context.target_inventory.current_fraction > context.target_inventory.upper_fraction:
            allow_buy_pairs = False
        if risk_reentry_blocked:
            allow_buy_pairs = False

        buy_size_fraction = 1.0
        sell_size_fraction = 1.0
        buy_discount_multiplier = 1.0
        buy_trigger = "recovery_entry" if policy_state == "RECOVERY_ENTRY" else "target_rebuild_buy"
        sell_trigger = "target_rebalance_sell"
        buy_reason = "价格进入折价区，按库存偏移生成成对买入挂单"
        sell_reason = "价格进入溢价区，按库存偏移生成成对卖出挂单"
        max_buy_levels = len(spreads)
        max_sell_levels = len(spreads)
        if scenario is not None:
            buy_size_fraction = max(0.0, min(1.0, scenario.buy_size_fraction))
            sell_size_fraction = max(0.0, min(1.0, scenario.sell_size_fraction))
            buy_discount_multiplier = max(1.0, scenario.buy_discount_multiplier)
            state = scenario.scenario_state
            if state in {"UPTREND_PROBE_ENTRY", "UPTREND_PULLBACK_ENTRY", "RECOVERY_AFTER_DROP"}:
                allow_buy_pairs = allow_buy_pairs or context.target_inventory.available_buy_notional > 0
                allow_sell_pairs = False if state != "UPTREND_PULLBACK_ENTRY" else allow_sell_pairs
                buy_trigger = "trend_probe_entry" if state == "UPTREND_PROBE_ENTRY" else "recovery_entry" if state == "RECOVERY_AFTER_DROP" else "pullback_entry"
                buy_reason = scenario.reason_cn
                max_buy_levels = min(max_buy_levels, 2)
            elif state == "UPTREND_HOLD_EXPANSION":
                allow_buy_pairs = False
                allow_sell_pairs = context.target_inventory.current_fraction > context.target_inventory.upper_fraction
                sell_reason = scenario.reason_cn
            elif state == "UPTREND_EXHAUSTION_TAKE_PROFIT":
                allow_buy_pairs = False
                allow_sell_pairs = context.target_inventory.allowed_sell_quantity > 0
                sell_trigger = "uptrend_take_profit"
                sell_reason = scenario.reason_cn
                max_sell_levels = min(max_sell_levels, 2)
            elif state == "DOWNTREND_DEFENSIVE":
                allow_buy_pairs = context.target_inventory.current_fraction < context.target_inventory.lower_fraction and context.target_inventory.available_buy_notional > 0
                allow_sell_pairs = context.target_inventory.current_fraction > context.target_inventory.target_fraction
                buy_trigger = "defensive_deep_discount_buy"
                buy_reason = scenario.reason_cn
                max_buy_levels = min(max_buy_levels, 1)
            elif state == "LOW_VOL_OBSERVE" and "DEEP_DISCOUNT_BUY" in scenario.allowed_actions:
                allow_buy_pairs = context.target_inventory.current_fraction < context.target_inventory.lower_fraction and context.target_inventory.available_buy_notional > 0
                allow_sell_pairs = False
                buy_trigger = "low_vol_deep_discount_buy"
                buy_reason = scenario.reason_cn
                max_buy_levels = min(max_buy_levels, 1)

        pair_count = self._pair_count()
        buy_budget = max(0.0, context.target_inventory.available_buy_notional * max(0.0, skew.buy_weight) * buy_size_fraction)
        sell_budget_qty = max(0.0, min(context.base_balance, context.target_inventory.allowed_sell_quantity * max(0.0, skew.sell_weight) * sell_size_fraction))
        buy_levels = min(pair_count, max_buy_levels)
        sell_levels = min(pair_count, max_sell_levels)

        if allow_buy_pairs and buy_budget > 0:
            per_level_notional = min(
                max(0.0, self.settings.order_target_notional),
                buy_budget / max(1, buy_levels),
            )
            for index, spread in enumerate(spreads[:buy_levels]):
                spread = spread * buy_discount_multiplier
                if scenario is not None and scenario.buy_anchor_price > 0 and scenario.buy_anchor_price < context.price:
                    spread = max(spread, context.price / scenario.buy_anchor_price - 1.0)
                bid_price = price_reference * (1.0 - spread)
                ask_target = price_reference * (1.0 + spread)
                pair_id = f"{context.symbol}:{policy_state.lower()}:{buy_trigger}:buy:{index}:{context.timestamp_ms}"
                proposals.append(
                    OrderProposal(
                        symbol=context.symbol,
                        side="BUY",
                        trigger=buy_trigger,
                        ladder_group="pair_market_making",
                        quantity=per_level_notional / bid_price if bid_price > 0 else 0.0,
                        notional=per_level_notional,
                        urgent=False,
                        tier_index=index,
                        target_spread_pct=spread,
                        target_fraction=1.0 / max(1, buy_levels),
                        score=context.composite_decision.buy_score,
                        reason_cn=buy_reason,
                        source="pair_market_making",
                        pair_id=pair_id,
                        pair_role="bid",
                        intended_counter_price=ask_target,
                        expected_pair_net_edge_pct=self._pair_net_edge_pct(bid_price, ask_target),
                    )
                )

        if allow_sell_pairs and sell_budget_qty > 0:
            per_level_quantity = sell_budget_qty / max(1, sell_levels)
            for index, spread in enumerate(spreads[:sell_levels]):
                ask_price = price_reference * (1.0 + spread)
                bid_target = price_reference * (1.0 - spread)
                pair_id = f"{context.symbol}:{policy_state.lower()}:{sell_trigger}:sell:{index}:{context.timestamp_ms}"
                proposals.append(
                    OrderProposal(
                        symbol=context.symbol,
                        side="SELL",
                        trigger=sell_trigger,
                        ladder_group="pair_market_making",
                        quantity=per_level_quantity,
                        notional=per_level_quantity * ask_price,
                        urgent=False,
                        tier_index=index,
                        target_spread_pct=spread,
                        target_fraction=1.0 / max(1, sell_levels),
                        score=context.composite_decision.sell_score,
                        reason_cn=sell_reason,
                        source="pair_market_making",
                        pair_id=pair_id,
                        pair_role="ask",
                        intended_counter_price=bid_target,
                        expected_pair_net_edge_pct=self._pair_net_edge_pct(bid_target, ask_price),
                    )
                )
        return self._merge_small_tiers(proposals)

    def _merge_small_tiers(self, proposals: List[OrderProposal]) -> List[OrderProposal]:
        if not self.settings.order_tier_merge_enabled:
            return proposals
        threshold = max(self.settings.order_tier_merge_min_notional, self.settings.min_effective_order_notional)
        if threshold <= 0:
            return proposals
        merged: List[OrderProposal] = []
        pending: List[OrderProposal] = []
        for proposal in proposals:
            if proposal.notional >= threshold or proposal.urgent:
                if pending:
                    merged.append(self._merge_group(pending))
                    pending = []
                merged.append(proposal)
                continue
            pending.append(proposal)
            if sum(item.notional for item in pending) >= threshold:
                merged.append(self._merge_group(pending))
                pending = []
        if pending:
            merged.append(self._merge_group(pending))
        return merged

    @staticmethod
    def _initial_inventory_release(context: PolicyContext) -> float:
        release = context.activation_state.get("initial_inventory_release")
        if not isinstance(release, dict) or not release.get("enabled"):
            return 0.0
        try:
            return max(0.0, float(release.get("remaining_quantity", 0.0) or 0.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _merge_group(group: Sequence[OrderProposal]) -> OrderProposal:
        first = group[0]
        notional = sum(item.notional for item in group)
        quantity = sum(item.quantity for item in group)
        spread = sum(item.target_spread_pct * item.notional for item in group) / notional if notional > 0 else first.target_spread_pct
        edge = min(item.expected_pair_net_edge_pct for item in group)
        tiers = ",".join(str(item.tier_index) for item in group)
        return replace(
            first,
            quantity=quantity,
            notional=notional,
            target_spread_pct=spread,
            target_fraction=sum(item.target_fraction for item in group),
            reason_cn=f"{first.reason_cn}；已合并小档位 {tiers}",
            pair_id=f"{first.pair_id}:merged:{tiers}",
            expected_pair_net_edge_pct=edge,
        )

    def _pair_count(self) -> int:
        return max(1, min(self.settings.order_levels_per_side, self.settings.order_max_open_per_side))

    def _effective_pending_buyback(self, context: PolicyContext) -> float:
        pending = self._float(context.activation_state.get("pending_buyback_quantity"))
        if pending <= 0:
            return 0.0
        min_notional = max(
            context.filters.min_notional,
            self.settings.min_order_notional,
            self.settings.grid_min_order_notional,
        )
        if pending * context.price < min_notional:
            return 0.0
        return pending

    def _counter_buyback_proposal(self, context: PolicyContext, pending_qty: float) -> OrderProposal | None:
        if not self.settings.pair_counter_buyback_enabled:
            return None
        if context.quote_balance <= 0 or context.price <= 0:
            return None
        state = context.activation_state
        release_price = self._float(state.get("last_release_price")) or self._float(state.get("last_grid_sell_price"))
        target_price = self._float(state.get("target_buyback_price"))
        release_trigger = str(state.get("last_release_trigger") or "")
        if target_price <= 0 and release_trigger in {"target_rebalance_sell", "uptrend_take_profit"} and release_price > 0:
            target_price = release_price * (
                1.0
                - self.settings.pair_counter_buyback_min_net_edge_pct
                - (2.0 * self.settings.maker_fee_pct)
                - self.settings.pair_edge_safety_buffer_pct
            )
        if target_price <= 0:
            return None
        target_price = min(target_price, context.price)
        notional = min(
            context.quote_balance,
            self.settings.order_target_notional,
            pending_qty * target_price,
        )
        if notional <= 0:
            return None
        spread = max(0.0, context.price / target_price - 1.0)
        pair_id = str(state.get("last_release_pair_id") or f"{context.symbol}:counter_buyback:{context.timestamp_ms}")
        edge = (
            self._pair_net_edge_pct(target_price, release_price)
            if release_price > target_price > 0
            else self.settings.pair_counter_buyback_min_net_edge_pct
        )
        return OrderProposal(
            symbol=context.symbol,
            side="BUY",
            trigger="pair_counter_buyback",
            ladder_group="buyback",
            quantity=notional / target_price,
            notional=notional,
            urgent=False,
            tier_index=0,
            target_spread_pct=spread,
            target_fraction=1.0,
            score=max(context.composite_decision.buy_score, 0.75),
            reason_cn="已有释放卖出等待回补，按目标回补价生成买入挂单",
            source="pair_counter_buyback",
            pair_id=pair_id,
            pair_role="bid",
            intended_counter_price=release_price,
            expected_pair_net_edge_pct=edge,
        )

    def _recovery_probe_proposal(self, context: PolicyContext) -> OrderProposal | None:
        if context.price <= 0 or context.quote_balance <= 0:
            return None
        if context.direction_decision is None:
            return None
        limit_price = min(context.price, context.direction_decision.buy_zone_price)
        if limit_price <= 0:
            return None
        max_notional = context.target_inventory.total_equity * max(0.0, self.settings.recovery_probe_max_equity_fraction)
        notional = min(
            context.quote_balance,
            context.target_inventory.available_buy_notional,
            self.settings.order_target_notional,
            max_notional,
        )
        if notional <= 0:
            return None
        spread = max(0.0, context.price / limit_price - 1.0)
        pair_id = f"{context.symbol}:recovery_probe:{context.timestamp_ms}"
        return OrderProposal(
            symbol=context.symbol,
            side="BUY",
            trigger="recovery_probe_entry",
            ladder_group="recovery_probe",
            quantity=notional / limit_price,
            notional=notional,
            urgent=False,
            tier_index=0,
            target_spread_pct=spread,
            target_fraction=1.0,
            score=max(context.composite_decision.buy_score, 0.7),
            reason_cn="亏损保护下的低风险恢复建仓，小额试探买入",
            source="recovery_probe",
            pair_id=pair_id,
            pair_role="bid",
            intended_counter_price=limit_price * (1.0 + self.settings.min_pair_net_edge_pct + (2.0 * self.settings.maker_fee_pct)),
            expected_pair_net_edge_pct=max(context.direction_decision.expected_net_edge_pct, self.settings.min_pair_net_edge_pct),
        )

    def _emergency_stop_fraction(self, context: PolicyContext) -> float:
        if self._ai_extreme(context.ai_assessment):
            return min(1.0, max(0.0, self.settings.exit_emergency_stop_fraction))
        stage = int(self._float(context.activation_state.get("risk_exit_stage")))
        confirmations = int(self._float(context.activation_state.get("emergency_stop_confirmation_bars")))
        if stage <= 0:
            return min(1.0, max(0.0, self.settings.emergency_stop_max_fraction))
        if confirmations >= max(1, self.settings.emergency_stop_full_exit_confirmation_bars):
            return min(1.0, max(0.0, self.settings.emergency_stop_second_stage_fraction))
        if self.settings.emergency_stop_full_exit_ai_extreme_only:
            return 0.0
        return min(1.0, max(0.0, self.settings.emergency_stop_max_fraction))

    def _spread_levels(self, context: PolicyContext) -> List[float]:
        base = self._parse_spread_levels()
        scenario = context.scenario_decision
        if scenario is None or scenario.scenario_state != "RANGE_MARKET_MAKING":
            return base
        indicators = scenario.indicators if isinstance(scenario.indicators, dict) else {}
        atr_pct = self._float(indicators.get("atr_pct"))
        if atr_pct <= 0:
            return base
        fee_buffer = self.settings.maker_fee_pct * 2.0 + self.settings.pair_edge_safety_buffer_pct
        min_spread = max(
            self.settings.min_range_spread_pct,
            (self.settings.min_pair_net_edge_pct + fee_buffer) / 2.0,
        )
        adaptive_first = max(min_spread, min(base[0], atr_pct * self.settings.range_spread_atr_multiplier))
        adjusted = [adaptive_first]
        for level in base[1:]:
            adjusted.append(max(level, adaptive_first))
        return adjusted

    def _parse_spread_levels(self) -> List[float]:
        values: List[float] = []
        for raw in str(self.settings.pair_spread_levels).split(","):
            try:
                value = float(raw.strip())
            except (TypeError, ValueError):
                continue
            if value > 0:
                values.append(value)
        return values or [0.0035, 0.0055, 0.0080, 0.0110, 0.0150]
    def _pair_net_edge_pct(self, buy_price: float, sell_price: float) -> float:
        if buy_price <= 0 or sell_price <= 0:
            return 0.0
        gross_edge = sell_price / buy_price - 1.0
        return gross_edge - self.settings.maker_fee_pct - self.settings.maker_fee_pct - self.settings.pair_edge_safety_buffer_pct

    @staticmethod
    def _float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _ai_extreme(assessment: AiRiskAssessment) -> bool:
        text = f"{assessment.status} {assessment.veto_reason}".lower()
        return assessment.risk_score >= 0.9 or "extreme" in text or "极端" in text

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(2.0, value))


class OrderProposalFilter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.guard = TradeProfitabilityGuard(settings)

    def filter(
        self,
        proposals: Iterable[OrderProposal],
        *,
        context: PolicyContext,
        locks: Sequence[ProtectionLock],
    ) -> tuple[List[OrderProposal], List[ProposalFilterResult]]:
        accepted: List[OrderProposal] = []
        results: List[ProposalFilterResult] = []
        active_locks = [lock for lock in locks if lock.active]
        for proposal in proposals:
            result = self._filter_one(proposal, context=context, active_locks=active_locks)
            results.append(result)
            if result.allowed:
                accepted.append(proposal)
        return accepted, results

    def _filter_one(
        self,
        proposal: OrderProposal,
        *,
        context: PolicyContext,
        active_locks: Sequence[ProtectionLock],
    ) -> ProposalFilterResult:
        hard_lock = self._blocking_lock(proposal, active_locks)
        if hard_lock:
            return self._blocked(proposal, hard_lock.lock_type.lower(), hard_lock.reason_cn)
        if proposal.quantity <= 0 or proposal.notional <= 0:
            return self._blocked(proposal, "proposal_size_zero", "订单提案数量或金额为零")
        min_notional = max(
            context.filters.min_notional,
            self.settings.min_order_notional,
            0.0 if proposal.urgent else self.settings.min_effective_order_notional,
        )
        if proposal.notional < min_notional:
            return self._blocked(
                proposal,
                "proposal_below_effective_notional",
                f"提案金额 {proposal.notional:.2f} 低于有效下单金额 {min_notional:.2f}",
            )
        if proposal.side.upper() == "BUY":
            if proposal.expected_pair_net_edge_pct < self.settings.min_pair_net_edge_pct:
                return ProposalFilterResult(
                    symbol=proposal.symbol,
                    side=proposal.side,
                    trigger=proposal.trigger,
                    ladder_group=proposal.ladder_group,
                    allowed=False,
                    reason="pair_net_edge_too_small",
                    reason_cn=(
                        f"预期成对净边际 {proposal.expected_pair_net_edge_pct:.2%} 低于 "
                        f"{self.settings.min_pair_net_edge_pct:.2%}"
                    ),
                    quantity=proposal.quantity,
                    notional=proposal.notional,
                    net_edge_pct=proposal.expected_pair_net_edge_pct,
                    required_edge_pct=self.settings.min_pair_net_edge_pct,
                    pair_id=proposal.pair_id,
                    pair_role=proposal.pair_role,
                    expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
                )
            buy_limit = context.price * (1.0 - proposal.target_spread_pct)
            buyback_trigger = proposal.trigger in {"pair_counter_buyback", "grid_buyback"}
            if context.direction_decision is not None and buy_limit > context.direction_decision.buy_zone_price and not buyback_trigger:
                scenario_state = context.scenario_decision.scenario_state if context.scenario_decision is not None else ""
                if proposal.trigger not in {"trend_probe_entry", "pullback_entry", "recovery_entry"} or scenario_state not in {
                    "UPTREND_PROBE_ENTRY",
                    "UPTREND_PULLBACK_ENTRY",
                    "RECOVERY_AFTER_DROP",
                }:
                    return self._blocked(
                        proposal,
                        "direction_buy_zone_not_reached",
                        f"买单价 {buy_limit:.4f} 仍高于折价买入区 {context.direction_decision.buy_zone_price:.4f}",
                    )
            reserved_quote = sum(
                order.reserved_quote for order in context.open_orders if order.side.upper() == "BUY"
            )
            if proposal.notional > max(0.0, context.quote_balance - reserved_quote):
                return self._blocked(proposal, "quote_balance_insufficient", "可用现金不足，不能提交买单提案")
            if self._has_duplicate_open_order(proposal, context.open_orders):
                return self._blocked(proposal, "duplicate_open_ladder_order", "同一方向、触发源和梯队已有挂单，保持原挂单")
        if proposal.side.upper() == "SELL":
            if proposal.trigger == "initial_inventory_release_sell":
                reserved_base = sum(
                    order.reserved_base for order in context.open_orders if order.side.upper() == "SELL"
                )
                if proposal.quantity > max(0.0, context.base_balance - reserved_base):
                    return self._blocked(proposal, "base_balance_insufficient", "可卖持仓不足，不能提交初始库存释放单")
                if self._has_duplicate_open_order(proposal, context.open_orders):
                    return self._blocked(proposal, "duplicate_open_ladder_order", "初始库存释放单已存在，等待触价")
                return ProposalFilterResult(
                    symbol=proposal.symbol,
                    side=proposal.side,
                    trigger=proposal.trigger,
                    ladder_group=proposal.ladder_group,
                    allowed=True,
                    reason="initial_inventory_release_allowed",
                    reason_cn="初始库存释放单通过，首笔释放盈亏不计入 Boti 操作盈亏",
                    quantity=proposal.quantity,
                    notional=proposal.notional,
                    net_edge_pct=0.0,
                    required_edge_pct=0.0,
                    pair_id=proposal.pair_id,
                    pair_role=proposal.pair_role,
                    expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
                )
            if (
                context.direction_decision is not None
                and context.price * (1.0 + proposal.target_spread_pct) < context.direction_decision.sell_zone_price
                and not self._is_risk_sell(proposal)
            ):
                return self._blocked(
                    proposal,
                    "direction_sell_zone_not_reached",
                    f"卖单价 {context.price * (1.0 + proposal.target_spread_pct):.4f} 仍低于溢价卖出区 {context.direction_decision.sell_zone_price:.4f}",
                )
            reserved_base = sum(
                order.reserved_base for order in context.open_orders if order.side.upper() == "SELL"
            )
            if proposal.quantity > max(0.0, context.base_balance - reserved_base):
                return self._blocked(proposal, "base_balance_insufficient", "可卖持仓不足，不能提交卖单提案")
            cost_block = self._cost_protection_block(proposal, context)
            if cost_block is not None:
                return cost_block
            if not self._is_risk_sell(proposal):
                if proposal.expected_pair_net_edge_pct < self.settings.min_pair_net_edge_pct:
                    return ProposalFilterResult(
                        symbol=proposal.symbol,
                        side=proposal.side,
                        trigger=proposal.trigger,
                        ladder_group=proposal.ladder_group,
                        allowed=False,
                        reason="pair_net_edge_too_small",
                        reason_cn=(
                            f"预期成对净边际 {proposal.expected_pair_net_edge_pct:.2%} 低于 "
                            f"{self.settings.min_pair_net_edge_pct:.2%}"
                        ),
                        quantity=proposal.quantity,
                        notional=proposal.notional,
                        net_edge_pct=proposal.expected_pair_net_edge_pct,
                        required_edge_pct=self.settings.min_pair_net_edge_pct,
                        pair_id=proposal.pair_id,
                        pair_role=proposal.pair_role,
                        expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
                    )
            if self._has_duplicate_open_order(proposal, context.open_orders):
                return self._blocked(proposal, "duplicate_open_ladder_order", "同一方向、触发源和梯队已有挂单，保持原挂单")

        return ProposalFilterResult(
            symbol=proposal.symbol,
            side=proposal.side,
            trigger=proposal.trigger,
            ladder_group=proposal.ladder_group,
            allowed=True,
            reason="proposal_allowed",
            reason_cn="订单提案通过保护、库存、金额和收益闸门",
            quantity=proposal.quantity,
            notional=proposal.notional,
            pair_id=proposal.pair_id,
            pair_role=proposal.pair_role,
            expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
        )

    def _blocking_lock(self, proposal: OrderProposal, locks: Sequence[ProtectionLock]) -> ProtectionLock | None:
        for lock in locks:
            if lock.lock_type in {"DRAWDOWN_GUARD", "STOPLOSS_GUARD", "AI_EXTREME_RISK", "LOW_PROFIT_PAIR_LOCK"}:
                if lock.lock_type == "DRAWDOWN_GUARD" and proposal.trigger == "recovery_probe_entry":
                    continue
                if proposal.trigger not in {"stop_loss", "emergency_stop"}:
                    return lock
            if lock.lock_type == "PAIR_LOCK_AFTER_STOP" and proposal.side.upper() == "BUY":
                return lock
        return None

    def _cost_protection_block(
        self,
        proposal: OrderProposal,
        context: PolicyContext,
    ) -> ProposalFilterResult | None:
        if not self.settings.sell_cost_protection_enabled:
            return None
        if self._is_risk_sell(proposal) and self.settings.allow_below_cost_sell_for_risk_exit:
            return None
        if self.settings.allow_below_cost_sell_for_rebalance:
            return None
        cost_price = self._effective_cost_price(context)
        if cost_price <= 0:
            return None
        sell_price = context.price * (1.0 + proposal.target_spread_pct)
        protection_price = cost_price * (1.0 + self.settings.sell_cost_protection_buffer_pct)
        if sell_price >= protection_price:
            return None
        return ProposalFilterResult(
            symbol=proposal.symbol,
            side=proposal.side,
            trigger=proposal.trigger,
            ladder_group=proposal.ladder_group,
            allowed=False,
            reason="below_cost_sell_blocked",
            reason_cn=(
                f"普通卖出价 {sell_price:.4f} 低于成本保护线 {protection_price:.4f}，"
                "拦截库存再平衡卖出，避免把存量亏损确认为 Boti 已实现亏损"
            ),
            quantity=proposal.quantity,
            notional=proposal.notional,
            net_edge_pct=proposal.expected_pair_net_edge_pct,
            required_edge_pct=self.settings.min_pair_net_edge_pct,
            pair_id=proposal.pair_id,
            pair_role=proposal.pair_role,
            expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
        )

    def _effective_cost_price(self, context: PolicyContext) -> float:
        real_cost = self._float(context.activation_state.get("real_average_entry_price"))
        if real_cost > 0:
            return real_cost
        if context.position_average_entry_price > 0:
            return context.position_average_entry_price
        return self._float(context.activation_state.get("synced_average_entry_price"))

    @staticmethod
    def _is_risk_sell(proposal: OrderProposal) -> bool:
        return proposal.trigger in {"stop_loss", "emergency_stop", "trailing_stop", "max_hold_exit"}

    @staticmethod
    def _ai_extreme(assessment: AiRiskAssessment) -> bool:
        text = f"{assessment.status} {assessment.veto_reason}".lower()
        return assessment.risk_score >= 0.9 or "extreme" in text or "极端" in text

    @staticmethod
    def _float(value: object) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _has_duplicate_open_order(proposal: OrderProposal, open_orders: Sequence[ManagedOrder]) -> bool:
        for order in open_orders:
            if proposal.pair_id and order.pair_id == proposal.pair_id and order.pair_role == proposal.pair_role:
                return True
            if (
                order.side.upper() == proposal.side.upper()
                and order.trigger == proposal.trigger
                and order.ladder_group == proposal.ladder_group
            ):
                return True
        return False

    @staticmethod
    def _blocked(proposal: OrderProposal, reason: str, reason_cn: str) -> ProposalFilterResult:
        return ProposalFilterResult(
            symbol=proposal.symbol,
            side=proposal.side,
            trigger=proposal.trigger,
            ladder_group=proposal.ladder_group,
            allowed=False,
            reason=reason,
            reason_cn=reason_cn,
            quantity=proposal.quantity,
            notional=proposal.notional,
            pair_id=proposal.pair_id,
            pair_role=proposal.pair_role,
            expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
        )


class PolicyEngine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.protections = ProtectionManager(settings)
        self.proposals = InventorySkewOrderProposalEngine(settings)
        self.filters = OrderProposalFilter(settings)

    def evaluate(self, context: PolicyContext) -> PolicyDecision:
        locks = self.protections.evaluate(context)
        skew = self.proposals.summarize(context)
        state = self._policy_state(context=context, locks=locks)
        raw_proposals = self.proposals.generate(context, state, skew)
        accepted, filter_results = self.filters.filter(raw_proposals, context=context, locks=locks)
        blockers = [result.reason_cn for result in filter_results if not result.allowed]
        action = "HOLD"
        if accepted:
            action = accepted[0].side.upper()
        elif state == "RISK_REDUCTION":
            action = "RISK_EXIT"
        return PolicyDecision(
            symbol=context.symbol,
            policy_state=state,
            mode_reason_cn=self._state_reason_cn(state, locks),
            recommended_action=action,
            protection_locks=list(locks),
            order_proposals=accepted,
            proposal_filter_results=filter_results,
            inventory_skew_summary=skew,
            direction_decision=context.direction_decision,
            scenario_decision=context.scenario_decision,
            merged_order_proposals=list(raw_proposals),
            blockers=blockers,
        )

    def _policy_state(self, *, context: PolicyContext, locks: Sequence[ProtectionLock]) -> str:
        active_lock_types = {lock.lock_type for lock in locks if lock.active}
        if context.exit_reason in {"stop_loss", "emergency_stop"} or context.composite_decision.recommended_action == "RISK_EXIT":
            return "RISK_REDUCTION"
        if "DRAWDOWN_GUARD" in active_lock_types and self._can_recovery_probe(context):
            return "RECOVERY_PROBE_ENTRY"
        if {"STOPLOSS_GUARD", "DRAWDOWN_GUARD", "AI_EXTREME_RISK", "LOW_PROFIT_PAIR_LOCK"} & active_lock_types:
            return "OBSERVE_ONLY"
        if "PAIR_LOCK_AFTER_STOP" in active_lock_types:
            return "PAIR_LOCKED_AFTER_STOP"
        if any(lock.lock_type == "PAIR_LOCK_AFTER_STOP" and not lock.active for lock in locks):
            return "RECOVERY_ENTRY"
        target = context.target_inventory
        if target.current_fraction < target.lower_fraction or target.current_fraction > target.upper_fraction:
            return "INVENTORY_REBALANCE"
        return "MARKET_MAKING"

    @staticmethod
    def _state_reason_cn(state: str, locks: Sequence[ProtectionLock]) -> str:
        active = [lock.reason_cn for lock in locks if lock.active]
        if active:
            return "；".join(active)
        labels = {
            "MARKET_MAKING": "库存接近目标区间，维持双边做市观察",
            "INVENTORY_REBALANCE": "库存偏离目标区间，优先生成回归目标仓位的订单提案",
            "RISK_REDUCTION": "硬风险触发，优先保护性退出",
            "PAIR_LOCKED_AFTER_STOP": "风险退出后锁定交易对，等待恢复入场条件",
            "RECOVERY_ENTRY": "风险退出后恢复条件满足，允许受控重建仓位",
            "RECOVERY_PROBE_ENTRY": "亏损保护生效，允许小额恢复建仓试探",
            "OBSERVE_ONLY": "保护层触发，仅观察或保留硬风险退出",
            "LOW_PROFIT_PAIR_LOCK": "最近完成 pair 质量偏低，暂停新的成对挂单提案",
        }
        return labels.get(state, state)

    def _can_recovery_probe(self, context: PolicyContext) -> bool:
        if not self.settings.recovery_probe_entry_enabled:
            return False
        target = context.target_inventory
        if target.current_fraction >= target.lower_fraction * 0.8:
            return False
        if target.available_buy_notional <= 0 or context.quote_balance <= 0:
            return False
        if context.ai_assessment.risk_score >= self.settings.recovery_probe_ai_risk_threshold:
            return False
        if self._ai_extreme(context.ai_assessment):
            return False
        if context.direction_decision is None:
            return False
        if context.price > context.direction_decision.buy_zone_price:
            return False
        if context.direction_decision.expected_net_edge_pct < self.settings.min_pair_net_edge_pct:
            return False
        count = int(ProtectionManager._float(context.activation_state.get("recovery_probe_daily_count")))
        return count < max(1, self.settings.recovery_probe_max_daily_count)

    @staticmethod
    def _ai_extreme(assessment: AiRiskAssessment) -> bool:
        text = f"{assessment.status} {assessment.veto_reason}".lower()
        return assessment.risk_score >= 0.9 or "extreme" in text or "极端" in text
