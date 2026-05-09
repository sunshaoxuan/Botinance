from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import AccountSnapshot, BuyDecisionDiagnostic, Candle, OrderRequest, PositionDiagnostic, PositionSnapshot, SymbolFilters


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str
    order: OrderRequest | None = None


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
    ) -> RiskDecision:
        quote_balance = account.balance_of(self.settings.quote_asset)
        quote_budget = quote_balance * self.settings.risk_per_trade
        notional = min(quote_budget, quote_balance)
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        raw_quantity = notional / price if price > 0 else 0.0
        quantity = self.client.quantize_quantity(raw_quantity, filters.step_size)
        final_notional = quantity * price

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
    ) -> BuyDecisionDiagnostic:
        quote_balance = account.balance_of(self.settings.quote_asset)
        quote_budget = quote_balance * self.settings.risk_per_trade
        notional = min(quote_budget, quote_balance)
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        raw_quantity = notional / price if price > 0 else 0.0
        quantity = self.client.quantize_quantity(raw_quantity, filters.step_size)
        final_notional = quantity * price

        blocker_details = []
        eligible_signal = signal_action == "BUY" and not has_position
        if signal_action != "BUY":
            blocker_details.append("当前策略信号不是买入")
        if has_position:
            blocker_details.append("当前已经有持仓，不重复买入")

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
    ) -> RiskDecision:
        quantity = self.client.quantize_quantity(base_asset_balance, filters.step_size)
        final_notional = quantity * price
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        if quantity < filters.min_qty or quantity <= 0:
            return RiskDecision(False, f"position_too_small_to_sell:{quantity}")
        if final_notional < min_notional:
            return RiskDecision(False, f"sell_notional_below_min_notional:{final_notional:.2f}")
        return RiskDecision(
            approved=True,
            reason="sell_order_approved",
            order=OrderRequest(symbol=symbol, side="SELL", order_type="MARKET", quantity=quantity),
        )

    def determine_exit_reason(
        self,
        price: float,
        position: PositionSnapshot,
        candles: Sequence[Candle],
        current_timestamp_ms: int,
    ) -> str | None:
        entry_price = position.average_entry_price
        stop_loss_price = entry_price * (1.0 - self.settings.stop_loss_pct)
        take_profit_price = entry_price * (1.0 + self.settings.take_profit_pct)
        highest_price = max(position.highest_price or entry_price, price)
        trailing_stop_price = highest_price * (1.0 - self.settings.trailing_stop_pct)

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
        stop_loss_price = entry_price * (1.0 - self.settings.stop_loss_pct)
        take_profit_price = entry_price * (1.0 + self.settings.take_profit_pct)
        trailing_stop_price = highest_price * (1.0 - self.settings.trailing_stop_pct)
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
