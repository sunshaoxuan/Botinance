from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Sequence

from binance_ai.config import Settings
from binance_ai.models import (
    AiRiskAssessment,
    Candle,
    CompositeDecision,
    InventorySkewSummary,
    ManagedOrder,
    OrderProposal,
    PolicyDecision,
    ProposalFilterResult,
    ProtectionLock,
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
        if policy_state == "RISK_REDUCTION" and context.has_position and context.exit_reason:
            if context.exit_reason == "emergency_stop":
                sell_fraction = self.settings.exit_emergency_stop_fraction
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
                )
            ]

        proposals: List[OrderProposal] = []
        if policy_state in {"PAIR_LOCKED_AFTER_STOP", "RECOVERY_ENTRY", "INVENTORY_REBALANCE", "MARKET_MAKING"}:
            buy_notional = context.target_inventory.available_buy_notional * skew.buy_weight
            if buy_notional > 0 and context.composite_decision.buy_score >= self.settings.buy_score_threshold * 0.85:
                proposals.append(
                    OrderProposal(
                        symbol=context.symbol,
                        side="BUY",
                        trigger="recovery_entry" if policy_state == "RECOVERY_ENTRY" else "target_rebuild_buy",
                        ladder_group="entry",
                        quantity=buy_notional / context.price if context.price > 0 else 0.0,
                        notional=buy_notional,
                        urgent=False,
                        score=context.composite_decision.buy_score,
                        reason_cn="仓位低于目标区间，按库存偏移生成建仓/补仓提案",
                        source="inventory_skew",
                        tiers_raw=self.settings.entry_ladder_tiers,
                    )
                )

        if policy_state in {"INVENTORY_REBALANCE", "MARKET_MAKING"} and context.has_position:
            sell_qty = context.target_inventory.allowed_sell_quantity * skew.sell_weight
            if sell_qty > 0 and context.composite_decision.sell_score >= self.settings.sell_score_threshold * 0.85:
                proposals.append(
                    OrderProposal(
                        symbol=context.symbol,
                        side="SELL",
                        trigger="target_rebalance_sell",
                        ladder_group="exit",
                        quantity=min(context.base_balance, sell_qty),
                        notional=min(context.base_balance, sell_qty) * context.price,
                        urgent=False,
                        score=context.composite_decision.sell_score,
                        reason_cn="仓位高于目标区间，按库存偏移生成减仓提案",
                        source="inventory_skew",
                        tiers_raw=self.settings.exit_ladder_tiers,
                    )
                )
        return proposals

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
            reserved_quote = sum(
                order.reserved_quote for order in context.open_orders if order.side.upper() == "BUY"
            )
            if proposal.notional > max(0.0, context.quote_balance - reserved_quote):
                return self._blocked(proposal, "quote_balance_insufficient", "可用现金不足，不能提交买单提案")
            if self._has_duplicate_open_order(proposal, context.open_orders):
                return self._blocked(proposal, "duplicate_open_ladder_order", "同一方向、触发源和梯队已有挂单，保持原挂单")
        if proposal.side.upper() == "SELL":
            reserved_base = sum(
                order.reserved_base for order in context.open_orders if order.side.upper() == "SELL"
            )
            if proposal.quantity > max(0.0, context.base_balance - reserved_base):
                return self._blocked(proposal, "base_balance_insufficient", "可卖持仓不足，不能提交卖单提案")
            if proposal.trigger not in {"stop_loss", "emergency_stop"}:
                expected_buyback = context.price * (1.0 - self.settings.grid_buyback_step_pct)
                guard = self.guard.inspect_release(
                    context.price,
                    expected_buyback,
                    min_net_edge_pct=self.settings.order_proposal_min_net_edge_pct,
                )
                if not guard.allowed:
                    return ProposalFilterResult(
                        symbol=proposal.symbol,
                        side=proposal.side,
                        trigger=proposal.trigger,
                        ladder_group=proposal.ladder_group,
                        allowed=False,
                        reason=guard.reason,
                        reason_cn=(
                            f"预期卖出/回补价差 {guard.net_edge_pct:.2%} 小于手续费和安全垫 "
                            f"{guard.required_edge_pct:.2%}"
                        ),
                        quantity=proposal.quantity,
                        notional=proposal.notional,
                        net_edge_pct=guard.net_edge_pct,
                        required_edge_pct=guard.required_edge_pct,
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
        )

    def _blocking_lock(self, proposal: OrderProposal, locks: Sequence[ProtectionLock]) -> ProtectionLock | None:
        for lock in locks:
            if lock.lock_type in {"DRAWDOWN_GUARD", "STOPLOSS_GUARD", "AI_EXTREME_RISK"}:
                if proposal.trigger not in {"stop_loss", "emergency_stop"}:
                    return lock
            if lock.lock_type == "PAIR_LOCK_AFTER_STOP" and proposal.side.upper() == "BUY":
                return lock
        return None

    @staticmethod
    def _has_duplicate_open_order(proposal: OrderProposal, open_orders: Sequence[ManagedOrder]) -> bool:
        for order in open_orders:
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
            blockers=blockers,
        )

    def _policy_state(self, *, context: PolicyContext, locks: Sequence[ProtectionLock]) -> str:
        active_lock_types = {lock.lock_type for lock in locks if lock.active}
        if context.exit_reason in {"stop_loss", "emergency_stop"} or context.composite_decision.recommended_action == "RISK_EXIT":
            return "RISK_REDUCTION"
        if {"STOPLOSS_GUARD", "DRAWDOWN_GUARD", "AI_EXTREME_RISK"} & active_lock_types:
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
            "OBSERVE_ONLY": "保护层触发，仅观察或保留硬风险退出",
        }
        return labels.get(state, state)
