from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict

from binance_ai.models import AccountSnapshot, OrderRequest, PortfolioSnapshot, PositionSnapshot


class PaperPortfolio:
    def __init__(self, quote_asset: str, initial_quote_balance: float, state_path: Path) -> None:
        self.quote_asset = quote_asset
        self.initial_quote_balance = initial_quote_balance
        self.state_path = state_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)

    def load_snapshot(self) -> PortfolioSnapshot:
        if not self.state_path.exists():
            snapshot = PortfolioSnapshot(
                quote_asset=self.quote_asset,
                quote_balance=self.initial_quote_balance,
                initial_quote_balance=self.initial_quote_balance,
            )
            self.save_snapshot(snapshot)
            return snapshot

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        positions = {
            symbol: PositionSnapshot(
                quantity=float(item["quantity"]),
                average_entry_price=float(item["average_entry_price"]),
                opened_at_ms=int(item.get("opened_at_ms", 0)),
                entry_candle_close_time=int(item.get("entry_candle_close_time", 0)),
                highest_price=float(item.get("highest_price", 0.0)),
            )
            for symbol, item in payload.get("positions", {}).items()
        }
        snapshot = PortfolioSnapshot(
            quote_asset=payload["quote_asset"],
            quote_balance=float(payload["quote_balance"]),
            initial_quote_balance=float(payload["initial_quote_balance"]),
            positions=positions,
            realized_pnl=float(payload.get("realized_pnl", 0.0)),
        )
        migrated = self._backfill_position_metadata(snapshot)
        if migrated != snapshot:
            self.save_snapshot(migrated)
        return migrated

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        self.state_path.write_text(
            json.dumps(asdict(snapshot), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def account_snapshot(self) -> AccountSnapshot:
        snapshot = self.load_snapshot()
        balances = {self.quote_asset: snapshot.quote_balance}
        for symbol, position in snapshot.positions.items():
            base_asset = symbol[: -len(self.quote_asset)] if symbol.endswith(self.quote_asset) else symbol
            balances[base_asset] = position.quantity
        return AccountSnapshot(balances=balances)

    def position_snapshot(self, symbol: str) -> PositionSnapshot | None:
        return self.load_snapshot().positions.get(symbol)

    def mark_to_market(
        self,
        symbol: str,
        mark_price: float,
        timestamp_ms: int,
        candle_close_time_ms: int,
    ) -> None:
        snapshot = self.load_snapshot()
        position = snapshot.positions.get(symbol)
        if position is None:
            return

        updated_position = PositionSnapshot(
            quantity=position.quantity,
            average_entry_price=position.average_entry_price,
            opened_at_ms=position.opened_at_ms or timestamp_ms,
            entry_candle_close_time=position.entry_candle_close_time or candle_close_time_ms,
            highest_price=max(position.highest_price or position.average_entry_price, mark_price),
        )
        if updated_position == position:
            return

        positions = dict(snapshot.positions)
        positions[symbol] = updated_position
        self.save_snapshot(
            PortfolioSnapshot(
                quote_asset=snapshot.quote_asset,
                quote_balance=snapshot.quote_balance,
                initial_quote_balance=snapshot.initial_quote_balance,
                positions=positions,
                realized_pnl=snapshot.realized_pnl,
            )
        )

    def apply_order(
        self,
        order: OrderRequest,
        fill_price: float,
        min_notional: float | None = None,
        min_qty: float | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> Dict[str, object]:
        snapshot = self.load_snapshot()
        positions = dict(snapshot.positions)
        realized_pnl_delta = 0.0
        notional = order.quantity * fill_price
        applied_timestamp_ms = timestamp_ms or int(time.time() * 1000)

        if min_qty is not None and (order.quantity < min_qty or order.quantity <= 0):
            return {
                "status": "BLOCKED",
                "reason": "paper_order_below_min_qty",
                "min_qty": min_qty,
                "quantity": order.quantity,
            }
        if min_notional is not None and notional < min_notional:
            return {
                "status": "BLOCKED",
                "reason": "paper_order_below_min_notional",
                "min_notional": min_notional,
                "final_notional": notional,
            }

        if order.side == "BUY":
            cost = notional
            if cost > snapshot.quote_balance:
                return {
                    "status": "BLOCKED",
                    "reason": f"paper_insufficient_{self.quote_asset.lower()}",
                }
            existing = positions.get(order.symbol)
            if existing is None:
                updated_position = PositionSnapshot(
                    quantity=order.quantity,
                    average_entry_price=fill_price,
                    opened_at_ms=applied_timestamp_ms,
                    entry_candle_close_time=entry_candle_close_time_ms or applied_timestamp_ms,
                    highest_price=fill_price,
                )
            else:
                new_quantity = existing.quantity + order.quantity
                avg_price = (
                    (existing.quantity * existing.average_entry_price) + cost
                ) / new_quantity
                updated_position = PositionSnapshot(
                    quantity=new_quantity,
                    average_entry_price=avg_price,
                    opened_at_ms=existing.opened_at_ms or applied_timestamp_ms,
                    entry_candle_close_time=existing.entry_candle_close_time or entry_candle_close_time_ms or applied_timestamp_ms,
                    highest_price=max(existing.highest_price or existing.average_entry_price, fill_price),
                )
            positions[order.symbol] = updated_position
            quote_balance = snapshot.quote_balance - cost
        else:
            existing = positions.get(order.symbol)
            if existing is None or order.quantity > existing.quantity:
                return {"status": "BLOCKED", "reason": "paper_position_not_available"}
            proceeds = order.quantity * fill_price
            cost_basis = order.quantity * existing.average_entry_price
            realized_pnl_delta = proceeds - cost_basis
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
            quote_balance = snapshot.quote_balance + proceeds

        updated = PortfolioSnapshot(
            quote_asset=self.quote_asset,
            quote_balance=quote_balance,
            initial_quote_balance=snapshot.initial_quote_balance,
            positions=positions,
            realized_pnl=snapshot.realized_pnl + realized_pnl_delta,
        )
        self.save_snapshot(updated)

        return {
            "status": "PAPER_FILLED",
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "fill_price": fill_price,
            "notional": notional,
            "realized_pnl_delta": realized_pnl_delta,
            "timestamp_ms": applied_timestamp_ms,
        }

    def equity_summary(self, mark_prices: Dict[str, float]) -> Dict[str, float]:
        snapshot = self.load_snapshot()
        market_value = 0.0
        unrealized_pnl = 0.0
        for symbol, position in snapshot.positions.items():
            price = mark_prices.get(symbol, 0.0)
            position_value = position.quantity * price
            market_value += position_value
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

    def _backfill_position_metadata(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        history_path = self.state_path.parent / "cycle_reports.jsonl"
        if not history_path.exists() or not snapshot.positions:
            return snapshot

        fills: Dict[str, Dict[str, object]] = {}
        for raw_line in history_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                cycle = json.loads(line)
            except json.JSONDecodeError:
                continue
            for decision in cycle.get("decisions", []):
                execution = decision.get("execution_result", {})
                if execution.get("status") != "PAPER_FILLED":
                    continue
                fills[execution.get("symbol", "")] = execution

        changed = False
        positions = dict(snapshot.positions)
        for symbol, position in list(positions.items()):
            fill = fills.get(symbol)
            if not fill or fill.get("side") != "BUY":
                continue
            fill_ts = int(fill.get("timestamp_ms", 0))
            if fill_ts <= 0:
                continue
            needs_backfill = position.opened_at_ms <= 0 or position.entry_candle_close_time <= 0
            needs_correction = (
                position.opened_at_ms > 0 and fill_ts < position.opened_at_ms
            ) or (
                position.entry_candle_close_time > 0 and fill_ts < position.entry_candle_close_time
            )
            if not needs_backfill and not needs_correction:
                continue
            positions[symbol] = PositionSnapshot(
                quantity=position.quantity,
                average_entry_price=position.average_entry_price,
                opened_at_ms=fill_ts,
                entry_candle_close_time=fill_ts,
                highest_price=position.highest_price or position.average_entry_price,
            )
            changed = True

        if not changed:
            return snapshot
        return PortfolioSnapshot(
            quote_asset=snapshot.quote_asset,
            quote_balance=snapshot.quote_balance,
            initial_quote_balance=snapshot.initial_quote_balance,
            positions=positions,
            realized_pnl=snapshot.realized_pnl,
        )
