from __future__ import annotations

import time
from dataclasses import replace
from typing import Dict, Tuple

from binance_ai.models import AccountSnapshot, OrderRequest, PortfolioSnapshot, PositionSnapshot


class PortfolioStateEngine:
    def __init__(self, quote_asset: str, fee_rate: float = 0.0) -> None:
        self.quote_asset = quote_asset
        self.fee_rate = max(0.0, fee_rate)

    def account_snapshot(self, snapshot: PortfolioSnapshot) -> AccountSnapshot:
        balances = {self.quote_asset: snapshot.quote_balance}
        for symbol, position in snapshot.positions.items():
            base_asset = symbol[: -len(self.quote_asset)] if symbol.endswith(self.quote_asset) else symbol
            balances[base_asset] = position.quantity
        return AccountSnapshot(balances=balances)

    def mark_to_market(
        self,
        snapshot: PortfolioSnapshot,
        symbol: str,
        mark_price: float,
        timestamp_ms: int,
        candle_close_time_ms: int,
    ) -> PortfolioSnapshot:
        position = snapshot.positions.get(symbol)
        if position is None:
            return snapshot

        updated_position = PositionSnapshot(
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            opened_at_ms=position.opened_at_ms or timestamp_ms,
            entry_candle_close_time=position.entry_candle_close_time or candle_close_time_ms,
            highest_price=max(position.highest_price or position.average_entry_price, mark_price),
        )
        if updated_position == position:
            return snapshot

        positions = dict(snapshot.positions)
        positions[symbol] = updated_position
        return replace(snapshot, positions=positions)

    def apply_order(
        self,
        snapshot: PortfolioSnapshot,
        order: OrderRequest,
        fill_price: float,
        min_notional: float | None = None,
        min_qty: float | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> Tuple[PortfolioSnapshot, Dict[str, object]]:
        positions = dict(snapshot.positions)
        realized_pnl_delta = 0.0
        notional = order.quantity * fill_price
        fee = notional * self.fee_rate
        applied_timestamp_ms = timestamp_ms or int(time.time() * 1000)

        if min_qty is not None and (order.quantity < min_qty or order.quantity <= 0):
            return snapshot, {
                "status": "BLOCKED",
                "reason": "paper_order_below_min_qty",
                "min_qty": min_qty,
                "quantity": order.quantity,
            }
        if min_notional is not None and notional < min_notional:
            return snapshot, {
                "status": "BLOCKED",
                "reason": "paper_order_below_min_notional",
                "min_notional": min_notional,
                "final_notional": notional,
            }

        if order.side == "BUY":
            gross_cost = notional + fee
            if gross_cost > snapshot.quote_balance:
                return snapshot, {
                    "status": "BLOCKED",
                    "reason": f"paper_insufficient_{self.quote_asset.lower()}",
                    "required_quote": gross_cost,
                }
            cost_basis = notional + fee
            existing = positions.get(order.symbol)
            if existing is None:
                updated_position = PositionSnapshot(
                    quantity=order.quantity,
                    average_entry_price=cost_basis / order.quantity,
                    opened_at_ms=applied_timestamp_ms,
                    entry_candle_close_time=entry_candle_close_time_ms or applied_timestamp_ms,
                    highest_price=fill_price,
                )
            else:
                new_quantity = existing.quantity + order.quantity
                avg_price = ((existing.quantity * existing.average_entry_price) + cost_basis) / new_quantity
                updated_position = PositionSnapshot(
                    quantity=new_quantity,
                    average_entry_price=avg_price,
                    opened_at_ms=existing.opened_at_ms or applied_timestamp_ms,
                    entry_candle_close_time=existing.entry_candle_close_time or entry_candle_close_time_ms or applied_timestamp_ms,
                    highest_price=max(existing.highest_price or existing.average_entry_price, fill_price),
                )
            positions[order.symbol] = updated_position
            updated_snapshot = replace(snapshot, quote_balance=snapshot.quote_balance - gross_cost, positions=positions)
        else:
            existing = positions.get(order.symbol)
            if existing is None or order.quantity > existing.quantity:
                return snapshot, {"status": "BLOCKED", "reason": "paper_position_not_available"}
            proceeds = order.quantity * fill_price
            cost_basis = order.quantity * existing.average_entry_price
            net_proceeds = proceeds - fee
            realized_pnl_delta = net_proceeds - cost_basis
            remaining = existing.quantity - order.quantity
            if remaining <= 0:
                positions.pop(order.symbol, None)
            else:
                positions[order.symbol] = PositionSnapshot(
                    quantity=remaining,
                    average_entry_price=existing.average_entry_price,
                    opened_at_ms=existing.opened_at_ms,
                    entry_candle_close_time=existing.entry_candle_close_time,
                    highest_price=existing.highest_price,
                )
            updated_snapshot = replace(
                snapshot,
                quote_balance=snapshot.quote_balance + net_proceeds,
                positions=positions,
                realized_pnl=snapshot.realized_pnl + realized_pnl_delta,
            )

        return updated_snapshot, {
            "status": "PAPER_FILLED",
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "fill_price": fill_price,
            "notional": notional,
            "fee": fee,
            "fee_rate": self.fee_rate,
            "net_notional": notional + fee if order.side == "BUY" else notional - fee,
            "realized_pnl_delta": realized_pnl_delta,
            "timestamp_ms": applied_timestamp_ms,
        }

    def equity_summary(self, snapshot: PortfolioSnapshot, mark_prices: Dict[str, float]) -> Dict[str, float]:
        market_value = 0.0
        unrealized_pnl = 0.0
        for symbol, position in snapshot.positions.items():
            price = mark_prices.get(symbol, 0.0)
            market_value += position.quantity * price
            unrealized_pnl += position.quantity * (price - position.average_entry_price)
        total_equity = snapshot.quote_balance + market_value
        return {
            "quote_balance": snapshot.quote_balance,
            "market_value": market_value,
            "total_equity": total_equity,
            "realized_pnl": snapshot.realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "net_pnl": total_equity - snapshot.initial_quote_balance,
        }
