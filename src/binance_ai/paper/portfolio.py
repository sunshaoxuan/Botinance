from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict

from binance_ai.models import AccountSnapshot, ManagedOrder, OrderLifecycleEvent, OrderRequest, PortfolioSnapshot, PositionSnapshot
from binance_ai.paper.state_engine import PortfolioStateEngine


class PaperPortfolio:
    def __init__(self, quote_asset: str, initial_quote_balance: float, state_path: Path, fee_rate: float = 0.0) -> None:
        self.quote_asset = quote_asset
        self.initial_quote_balance = initial_quote_balance
        self.state_path = state_path
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = PortfolioStateEngine(quote_asset, fee_rate=fee_rate)

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
        open_orders = {
            client_order_id: ManagedOrder(
                client_order_id=str(item.get("client_order_id", client_order_id)),
                symbol=str(item.get("symbol", "")),
                side=str(item.get("side", "")),
                order_type=str(item.get("order_type", "LIMIT")),
                quantity=float(item.get("quantity", 0.0)),
                limit_price=float(item.get("limit_price", 0.0)),
                time_in_force=str(item.get("time_in_force", "GTC")),
                status=str(item.get("status", "OPEN")),
                created_at_ms=int(item.get("created_at_ms", 0)),
                updated_at_ms=int(item.get("updated_at_ms", 0)),
                expires_at_ms=int(item.get("expires_at_ms", 0)),
                trigger=str(item.get("trigger", "")),
                external_order_id=str(item.get("external_order_id", "")),
                filled_quantity=float(item.get("filled_quantity", 0.0)),
                remaining_quantity=float(item.get("remaining_quantity", item.get("quantity", 0.0))),
                average_fill_price=float(item.get("average_fill_price", 0.0)),
                reserved_quote=float(item.get("reserved_quote", 0.0)),
                reserved_base=float(item.get("reserved_base", 0.0)),
                entry_candle_close_time=int(item.get("entry_candle_close_time", 0)),
                last_reason=str(item.get("last_reason", "")),
            )
            for client_order_id, item in payload.get("open_orders", {}).items()
            if isinstance(item, dict)
        }
        snapshot = PortfolioSnapshot(
            quote_asset=payload["quote_asset"],
            quote_balance=float(payload["quote_balance"]),
            initial_quote_balance=float(payload["initial_quote_balance"]),
            positions=positions,
            realized_pnl=float(payload.get("realized_pnl", 0.0)),
            activation_state=payload.get("activation_state", {}),
            open_orders=open_orders,
            reserved_quote_balance=float(payload.get("reserved_quote_balance", 0.0)),
            reserved_base_balances={
                symbol: float(quantity)
                for symbol, quantity in payload.get("reserved_base_balances", {}).items()
            },
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
        return self.engine.account_snapshot(self.load_snapshot())

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
        updated = self.engine.mark_to_market(
            snapshot,
            symbol=symbol,
            mark_price=mark_price,
            timestamp_ms=timestamp_ms,
            candle_close_time_ms=candle_close_time_ms,
        )
        if updated == snapshot:
            return
        self.save_snapshot(updated)

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
        updated, result = self.engine.apply_order(
            snapshot,
            order,
            fill_price,
            min_notional=min_notional,
            min_qty=min_qty,
            timestamp_ms=timestamp_ms,
            entry_candle_close_time_ms=entry_candle_close_time_ms,
        )
        if updated != snapshot:
            self.save_snapshot(updated)
        return result

    def submit_limit_order(
        self,
        order: OrderRequest,
        min_notional: float | None = None,
        min_qty: float | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> tuple[Dict[str, object], OrderLifecycleEvent]:
        applied_timestamp_ms = timestamp_ms or int(time.time() * 1000)
        snapshot = self.load_snapshot()
        updated, result, event = self.engine.submit_limit_order(
            snapshot,
            order,
            min_notional=min_notional,
            min_qty=min_qty,
            timestamp_ms=applied_timestamp_ms,
            entry_candle_close_time_ms=entry_candle_close_time_ms or applied_timestamp_ms,
        )
        if updated != snapshot:
            self.save_snapshot(updated)
        return result, event

    def cancel_open_order(
        self,
        client_order_id: str,
        reason: str,
        timestamp_ms: int,
        status: str = "CANCELED",
    ) -> OrderLifecycleEvent | None:
        snapshot = self.load_snapshot()
        updated, event = self.engine.cancel_open_order(
            snapshot,
            client_order_id=client_order_id,
            reason=reason,
            timestamp_ms=timestamp_ms,
            status=status,
        )
        if updated != snapshot:
            self.save_snapshot(updated)
        return event

    def fill_open_order(
        self,
        client_order_id: str,
        fill_price: float,
        timestamp_ms: int,
        entry_candle_close_time_ms: int | None = None,
    ) -> tuple[Dict[str, object], OrderLifecycleEvent | None]:
        snapshot = self.load_snapshot()
        updated, result, event = self.engine.fill_open_order(
            snapshot,
            client_order_id=client_order_id,
            fill_price=fill_price,
            timestamp_ms=timestamp_ms,
            entry_candle_close_time_ms=entry_candle_close_time_ms,
        )
        if updated != snapshot:
            self.save_snapshot(updated)
        return result, event

    def open_orders(self, symbol: str | None = None) -> Dict[str, ManagedOrder]:
        orders = self.load_snapshot().open_orders
        if symbol is None:
            return orders
        return {
            client_order_id: order
            for client_order_id, order in orders.items()
            if order.symbol == symbol and order.status == "OPEN"
        }

    def equity_summary(self, mark_prices: Dict[str, float]) -> Dict[str, float]:
        return self.engine.equity_summary(self.load_snapshot(), mark_prices)

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
            activation_state=snapshot.activation_state,
            open_orders=snapshot.open_orders,
            reserved_quote_balance=snapshot.reserved_quote_balance,
            reserved_base_balances=snapshot.reserved_base_balances,
        )
