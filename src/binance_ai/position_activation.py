from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import AccountSnapshot, OrderRequest, PortfolioSnapshot, PositionSnapshot, SymbolFilters


@dataclass(frozen=True)
class PositionActivationDecision:
    action: str
    trigger: str
    reason: str
    quantity: float = 0.0
    order: OrderRequest | None = None
    state_update: Dict[str, object] | None = None


class PositionActivationEngine:
    def __init__(self, settings: Settings, client: BinanceSpotClient) -> None:
        self.settings = settings
        self.client = client

    def evaluate(
        self,
        *,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        filters: SymbolFilters,
        snapshot: PortfolioSnapshot,
        timestamp_ms: int,
    ) -> PositionActivationDecision:
        if not self.settings.position_activation_enabled:
            return PositionActivationDecision("HOLD", "", "position_activation_disabled")
        if self.settings.position_activation_mode != "active_grid":
            return PositionActivationDecision("HOLD", "", f"unsupported_activation_mode:{self.settings.position_activation_mode}")

        state = self._normalized_state(snapshot.activation_state.get(symbol), timestamp_ms)
        if int(state["daily_trade_count"]) >= self.settings.grid_max_daily_trades:
            return PositionActivationDecision("HOLD", "", "grid_daily_trade_limit_reached", state_update=state)

        pending_qty = float(state["pending_buyback_quantity"])
        last_sell_price = float(state["last_grid_sell_price"])
        if pending_qty > 0 and last_sell_price > 0:
            buyback_price = last_sell_price * (1.0 - self.settings.grid_buyback_step_pct)
            if price <= buyback_price:
                quantity = self.client.quantize_quantity(pending_qty, filters.step_size)
                decision = self._build_buyback(symbol, price, quantity, account, filters, state)
                return decision
            state["last_trigger"] = "grid_wait_buyback"
            state["last_reason"] = f"等待回补：当前价 {price:.8f} 高于回补价 {buyback_price:.8f}"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)

        position = snapshot.positions.get(symbol)
        if position is None or position.quantity <= 0:
            state["last_trigger"] = "grid_no_position"
            state["last_reason"] = "无持仓，仓位激活不卖出"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)

        unrealized_pct = (price - position.average_entry_price) / position.average_entry_price if position.average_entry_price > 0 else 0.0
        if unrealized_pct >= self.settings.grid_sell_step_pct:
            return self._build_grid_sell(
                symbol=symbol,
                price=price,
                position=position,
                filters=filters,
                state=state,
                trigger="grid_profit_sell",
                reason=f"浮盈 {unrealized_pct:.4%} 达到网格卖出阈值",
            )
        if unrealized_pct < 0 and self.settings.grid_allow_loss_recovery_sell:
            cost_basis_source = str(state.get("cost_basis_source", ""))
            if cost_basis_source and cost_basis_source != "binance_my_trades_fifo":
                state["last_trigger"] = "grid_loss_recovery_blocked"
                state["last_reason"] = "真实成本未确认，禁止亏损修复网格卖出"
                return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)
            loss_recovery_threshold = max(0.0, self.settings.grid_loss_recovery_sell_step_pct)
            if abs(unrealized_pct) < loss_recovery_threshold:
                state["last_trigger"] = "grid_loss_recovery_wait"
                state["last_reason"] = (
                    f"浮亏 {unrealized_pct:.4%} 未达到亏损修复卖出阈值 "
                    f"{loss_recovery_threshold:.4%}"
                )
                return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)
            return self._build_grid_sell(
                symbol=symbol,
                price=price,
                position=position,
                filters=filters,
                state=state,
                trigger="grid_loss_recovery_sell",
                reason=f"浮亏 {unrealized_pct:.4%}，按亏损修复网格释放资金",
            )

        state["last_trigger"] = "grid_hold"
        state["last_reason"] = f"浮盈 {unrealized_pct:.4%} 未达到网格卖出阈值"
        return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)

    def apply_success(
        self,
        *,
        snapshot: PortfolioSnapshot,
        symbol: str,
        decision: PositionActivationDecision,
        fill_price: float,
        timestamp_ms: int,
    ) -> PortfolioSnapshot:
        state = self._normalized_state(snapshot.activation_state.get(symbol), timestamp_ms)
        state["daily_trade_count"] = int(state["daily_trade_count"]) + 1
        state["last_trigger"] = decision.trigger
        state["last_reason"] = decision.reason
        state["last_trade_timestamp_ms"] = timestamp_ms

        if decision.trigger in self.release_sell_triggers():
            state["last_grid_sell_price"] = fill_price
            state["pending_buyback_quantity"] = float(state["pending_buyback_quantity"]) + decision.quantity
        elif decision.trigger == "grid_buyback":
            remaining = max(0.0, float(state["pending_buyback_quantity"]) - decision.quantity)
            state["pending_buyback_quantity"] = remaining
            if remaining <= 0:
                state["last_grid_sell_price"] = 0.0

        activation_state = dict(snapshot.activation_state)
        activation_state[symbol] = state
        return PortfolioSnapshot(
            quote_asset=snapshot.quote_asset,
            quote_balance=snapshot.quote_balance,
            initial_quote_balance=snapshot.initial_quote_balance,
            positions=snapshot.positions,
            realized_pnl=snapshot.realized_pnl,
            activation_state=activation_state,
            open_orders=snapshot.open_orders,
            reserved_quote_balance=snapshot.reserved_quote_balance,
            reserved_base_balances=snapshot.reserved_base_balances,
        )

    def apply_state_update(
        self,
        *,
        snapshot: PortfolioSnapshot,
        symbol: str,
        decision: PositionActivationDecision,
        timestamp_ms: int,
    ) -> PortfolioSnapshot:
        state = self._normalized_state(snapshot.activation_state.get(symbol), timestamp_ms)
        if decision.state_update:
            state.update(decision.state_update)
        activation_state = dict(snapshot.activation_state)
        activation_state[symbol] = state
        return PortfolioSnapshot(
            quote_asset=snapshot.quote_asset,
            quote_balance=snapshot.quote_balance,
            initial_quote_balance=snapshot.initial_quote_balance,
            positions=snapshot.positions,
            realized_pnl=snapshot.realized_pnl,
            activation_state=activation_state,
            open_orders=snapshot.open_orders,
            reserved_quote_balance=snapshot.reserved_quote_balance,
            reserved_base_balances=snapshot.reserved_base_balances,
        )

    def _build_grid_sell(
        self,
        *,
        symbol: str,
        price: float,
        position: PositionSnapshot,
        filters: SymbolFilters,
        state: Dict[str, object],
        trigger: str,
        reason: str,
    ) -> PositionActivationDecision:
        core_quantity = position.quantity * self.settings.grid_min_core_position_fraction
        max_sell_quantity = max(0.0, position.quantity - core_quantity)
        desired_quantity = position.quantity * self.settings.grid_sell_fraction
        quantity = self.client.quantize_quantity(min(desired_quantity, max_sell_quantity), filters.step_size)
        notional = quantity * price
        min_notional = max(filters.min_notional, self.settings.min_order_notional)

        if quantity <= 0 or quantity < filters.min_qty:
            state["last_trigger"] = "grid_sell_blocked"
            state["last_reason"] = f"网格卖出数量低于最小数量：{quantity}"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)
        if notional < min_notional:
            state["last_trigger"] = "grid_sell_blocked"
            state["last_reason"] = f"网格卖出金额低于最小成交额：{notional:.8f}"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)

        return PositionActivationDecision(
            action="SELL",
            trigger=trigger,
            reason=reason,
            quantity=quantity,
            order=OrderRequest(symbol=symbol, side="SELL", order_type="MARKET", quantity=quantity),
            state_update=state,
        )

    def _build_buyback(
        self,
        symbol: str,
        price: float,
        quantity: float,
        account: AccountSnapshot,
        filters: SymbolFilters,
        state: Dict[str, object],
    ) -> PositionActivationDecision:
        notional = quantity * price
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        if quantity <= 0 or quantity < filters.min_qty:
            state["last_trigger"] = "grid_buyback_blocked"
            state["last_reason"] = f"回补数量低于最小数量：{quantity}"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)
        if notional < min_notional:
            state["last_trigger"] = "grid_buyback_blocked"
            state["last_reason"] = f"回补金额低于最小成交额：{notional:.8f}"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)
        if account.balance_of(self.settings.quote_asset) < notional:
            state["last_trigger"] = "grid_buyback_blocked"
            state["last_reason"] = "回补资金不足"
            return PositionActivationDecision("HOLD", "", str(state["last_reason"]), state_update=state)
        return PositionActivationDecision(
            action="BUY",
            trigger="grid_buyback",
            reason="价格回落到网格回补线",
            quantity=quantity,
            order=OrderRequest(symbol=symbol, side="BUY", order_type="MARKET", quantity=quantity),
            state_update=state,
        )

    @staticmethod
    def _day_key(timestamp_ms: int) -> str:
        return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    def _normalized_state(self, raw_state: object, timestamp_ms: int) -> Dict[str, object]:
        state = dict(raw_state) if isinstance(raw_state, dict) else {}
        day_key = self._day_key(timestamp_ms)
        if state.get("daily_trade_day") != day_key:
            state["daily_trade_day"] = day_key
            state["daily_trade_count"] = 0
        state.setdefault("pending_buyback_quantity", 0.0)
        state.setdefault("last_grid_sell_price", 0.0)
        state.setdefault("last_trigger", "")
        state.setdefault("last_reason", "")
        state.setdefault("last_trade_timestamp_ms", 0)
        return state

    @staticmethod
    def release_sell_triggers() -> set[str]:
        return {
            "grid_profit_sell",
            "grid_loss_recovery_sell",
            "strategy_release_sell",
            "take_profit_release_sell",
            "trailing_stop_release_sell",
            "max_hold_release_sell",
        }
