from __future__ import annotations

import time
from typing import Dict, List, Tuple

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import Candle, ManagedOrder, OrderLifecycleEvent, OrderRequest, SymbolFilters
from binance_ai.paper.portfolio import PaperPortfolio


class OrderExecutor:
    def __init__(
        self,
        settings: Settings,
        client: BinanceSpotClient,
        paper_portfolio: PaperPortfolio | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.paper_portfolio = paper_portfolio
        self.live_open_orders: Dict[str, ManagedOrder] = {}

    def execute(
        self,
        order: OrderRequest,
        fill_price: float,
        filters: SymbolFilters | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> Dict[str, object]:
        if order.order_type == "LIMIT":
            result, _ = self.submit_limit_order(
                order,
                current_price=fill_price,
                filters=filters,
                timestamp_ms=timestamp_ms,
                entry_candle_close_time_ms=entry_candle_close_time_ms,
            )
            return result

        symbol_filters = filters or self.client.get_symbol_filters(order.symbol)
        validation = self._validate_exchange_rules(order, fill_price, symbol_filters)
        if validation is not None:
            return validation

        if self._use_paper_lifecycle():
            if self.paper_portfolio is None:
                return {"status": "REJECTED", "reason": "simulated_execution_requires_paper_portfolio"}
            return self.paper_portfolio.apply_order(
                order,
                fill_price,
                min_notional=max(symbol_filters.min_notional, self.settings.min_order_notional),
                min_qty=symbol_filters.min_qty,
                timestamp_ms=timestamp_ms,
                entry_candle_close_time_ms=entry_candle_close_time_ms,
            )
        if not self.settings.live_order_execution_enabled:
            return {"status": "REJECTED", "reason": "live_order_execution_disabled"}
        payload = self.client.place_market_order(order)
        return {
            "status": "LIVE_ORDER_SUBMITTED",
            "response": payload,
        }

    def submit_limit_order(
        self,
        order: OrderRequest,
        current_price: float,
        filters: SymbolFilters | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> Tuple[Dict[str, object], OrderLifecycleEvent | None]:
        timestamp_ms = timestamp_ms or int(time.time() * 1000)
        symbol_filters = filters or self.client.get_symbol_filters(order.symbol)
        validation = self._validate_exchange_rules(order, order.limit_price or current_price, symbol_filters)
        if validation is not None:
            event = OrderLifecycleEvent(
                timestamp_ms=timestamp_ms,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                event_type="REJECTED",
                status="REJECTED",
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                reason=str(validation.get("reason", "validation_failed")),
                trigger=order.trigger,
            )
            return validation, event

        max_open = max(0, self.settings.order_max_open_per_symbol)
        if max_open and len(self.open_orders_for_symbol(order.symbol)) >= max_open:
            event = OrderLifecycleEvent(
                timestamp_ms=timestamp_ms,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                event_type="REJECTED",
                status="REJECTED",
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                reason="max_open_orders_per_symbol_reached",
                trigger=order.trigger,
            )
            return {
                "status": "REJECTED",
                "reason": "max_open_orders_per_symbol_reached",
                "symbol": order.symbol,
                "client_order_id": order.client_order_id,
            }, event

        if self._use_paper_lifecycle():
            if self.paper_portfolio is None:
                event = OrderLifecycleEvent(
                    timestamp_ms=timestamp_ms,
                    symbol=order.symbol,
                    client_order_id=order.client_order_id,
                    event_type="REJECTED",
                    status="REJECTED",
                    side=order.side,
                    quantity=order.quantity,
                    limit_price=order.limit_price,
                    reason="simulated_execution_requires_paper_portfolio",
                    trigger=order.trigger,
                )
                return {"status": "REJECTED", "reason": event.reason}, event
            result, event = self.paper_portfolio.submit_limit_order(
                order,
                min_notional=max(symbol_filters.min_notional, self.settings.min_order_notional),
                min_qty=symbol_filters.min_qty,
                timestamp_ms=timestamp_ms,
                entry_candle_close_time_ms=entry_candle_close_time_ms,
            )
            return result, event

        if not self.settings.live_order_execution_enabled:
            event = OrderLifecycleEvent(
                timestamp_ms=timestamp_ms,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                event_type="REJECTED",
                status="REJECTED",
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                reason="live_order_execution_disabled",
                trigger=order.trigger,
            )
            return {"status": "REJECTED", "reason": "live_order_execution_disabled"}, event

        try:
            payload = self.client.place_limit_order(order)
            external_order_id = str(payload.get("orderId", ""))
            client_order_id = order.client_order_id or str(payload.get("clientOrderId", ""))
            status = self._normalize_live_status(str(payload.get("status", "NEW")))
            event = OrderLifecycleEvent(
                timestamp_ms=timestamp_ms,
                symbol=order.symbol,
                client_order_id=client_order_id,
                event_type="SUBMITTED",
                status=status,
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                reason="live_limit_order_submitted",
                trigger=order.trigger,
                external_order_id=external_order_id,
            )
            managed = self._managed_from_live_payload(payload, order, timestamp_ms, entry_candle_close_time_ms)
            if managed.status not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                self.live_open_orders[managed.client_order_id] = managed
            return {
                "status": "ORDER_OPEN",
                "symbol": order.symbol,
                "side": order.side,
                "quantity": order.quantity,
                "limit_price": order.limit_price,
                "client_order_id": client_order_id,
                "external_order_id": external_order_id,
                "response": payload,
                "trigger": order.trigger,
            }, event
        except Exception as exc:  # noqa: BLE001 - order status is genuinely unknown after transport failures.
            if order.client_order_id:
                self.live_open_orders[order.client_order_id] = ManagedOrder(
                    client_order_id=order.client_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    order_type="LIMIT",
                    quantity=order.quantity,
                    limit_price=order.limit_price,
                    time_in_force=order.time_in_force,
                    status="UNKNOWN",
                    created_at_ms=timestamp_ms,
                    updated_at_ms=timestamp_ms,
                    expires_at_ms=order.expires_at_ms,
                    trigger=order.trigger,
                    remaining_quantity=order.quantity,
                    entry_candle_close_time=entry_candle_close_time_ms or 0,
                    last_reason=str(exc),
                )
            event = OrderLifecycleEvent(
                timestamp_ms=timestamp_ms,
                symbol=order.symbol,
                client_order_id=order.client_order_id,
                event_type="UNKNOWN",
                status="UNKNOWN",
                side=order.side,
                quantity=order.quantity,
                limit_price=order.limit_price,
                reason=str(exc),
                trigger=order.trigger,
            )
            return {"status": "UNKNOWN", "reason": str(exc), "client_order_id": order.client_order_id}, event

    def process_open_orders(
        self,
        *,
        symbol: str,
        candles: List[Candle],
        current_price: float,
        timestamp_ms: int,
    ) -> Tuple[List[Dict[str, object]], List[OrderLifecycleEvent]]:
        if self._use_paper_lifecycle() and self.paper_portfolio is not None:
            return self._process_paper_open_orders(symbol, candles, current_price, timestamp_ms)
        if not self.settings.live_order_execution_enabled:
            return [], []
        return self._process_live_open_orders(symbol, timestamp_ms, current_price)

    def cancel_open_orders_for_symbol(
        self,
        *,
        symbol: str,
        reason: str,
        timestamp_ms: int,
    ) -> List[OrderLifecycleEvent]:
        events: List[OrderLifecycleEvent] = []
        if self._use_paper_lifecycle() and self.paper_portfolio is not None:
            for client_order_id in list(self.paper_portfolio.open_orders(symbol).keys()):
                event = self.paper_portfolio.cancel_open_order(client_order_id, reason, timestamp_ms)
                if event is not None:
                    events.append(event)
            return events
        if not self.settings.live_order_execution_enabled:
            return events

        for managed in list(self.live_open_orders.values()):
            if managed.symbol != symbol:
                continue
            try:
                raw = self.client.cancel_order(
                    managed.symbol,
                    order_id=managed.external_order_id or None,
                    client_order_id=managed.client_order_id or None,
                )
                updated = self._managed_from_live_payload(raw, managed, timestamp_ms)
                status = self._normalize_live_status(str(raw.get("status", updated.status)))
                event = self._event_from_managed(
                    updated,
                    timestamp_ms,
                    event_type=status if status != "OPEN" else "CANCELED",
                    reason=reason,
                )
                events.append(event)
                if status in {"CANCELED", "EXPIRED", "REJECTED", "FILLED"}:
                    self.live_open_orders.pop(managed.client_order_id, None)
                else:
                    self.live_open_orders[updated.client_order_id] = updated
            except Exception as exc:  # noqa: BLE001
                unknown = self._replace_managed_order(
                    managed,
                    status="UNKNOWN",
                    updated_at_ms=timestamp_ms,
                    last_reason=str(exc),
                )
                self.live_open_orders[unknown.client_order_id] = unknown
                events.append(
                    self._event_from_managed(
                        unknown,
                        timestamp_ms,
                        event_type="UNKNOWN",
                        reason=str(exc),
                    )
                )
        return events

    def open_orders_for_symbol(self, symbol: str) -> List[ManagedOrder]:
        if self._use_paper_lifecycle() and self.paper_portfolio is not None:
            return list(self.paper_portfolio.open_orders(symbol).values())
        if not self.settings.live_order_execution_enabled:
            return []
        return [order for order in self.live_open_orders.values() if order.symbol == symbol]

    def all_open_orders(self) -> List[ManagedOrder]:
        if self._use_paper_lifecycle() and self.paper_portfolio is not None:
            return list(self.paper_portfolio.open_orders().values())
        if not self.settings.live_order_execution_enabled:
            return []
        return list(self.live_open_orders.values())

    def _validate_exchange_rules(
        self,
        order: OrderRequest,
        fill_price: float,
        filters: SymbolFilters,
    ) -> Dict[str, object] | None:
        final_notional = order.quantity * fill_price
        min_notional = max(filters.min_notional, self.settings.min_order_notional)

        if order.quantity < filters.min_qty or order.quantity <= 0:
            return {
                "status": "BLOCKED",
                "reason": "execution_quantity_below_min_qty",
                "min_qty": filters.min_qty,
                "quantity": order.quantity,
            }
        if final_notional < min_notional:
            return {
                "status": "BLOCKED",
                "reason": "execution_notional_below_min_notional",
                "min_notional": min_notional,
                "final_notional": final_notional,
            }
        return None

    def _use_paper_lifecycle(self) -> bool:
        return self.settings.dry_run or not self.settings.live_order_execution_enabled

    def _process_paper_open_orders(
        self,
        symbol: str,
        candles: List[Candle],
        current_price: float,
        timestamp_ms: int,
    ) -> Tuple[List[Dict[str, object]], List[OrderLifecycleEvent]]:
        if self.paper_portfolio is None:
            return [], []
        results: List[Dict[str, object]] = []
        events: List[OrderLifecycleEvent] = []
        for managed in list(self.paper_portfolio.open_orders(symbol).values()):
            fill_candle = self._matching_candle(managed, candles)
            if fill_candle is not None:
                result, event = self.paper_portfolio.fill_open_order(
                    managed.client_order_id,
                    fill_price=managed.limit_price,
                    timestamp_ms=timestamp_ms,
                    entry_candle_close_time_ms=fill_candle.close_time,
                )
                results.append(result)
                if event is not None:
                    events.append(event)
                continue

            if self._is_stale(managed, timestamp_ms):
                events.append(
                    self._event_from_managed(
                        managed,
                        timestamp_ms,
                        event_type="STALE",
                        reason="order_stale_observed",
                    )
                )
        return results, events

    def _process_live_open_orders(
        self,
        symbol: str,
        timestamp_ms: int,
        current_price: float,
    ) -> Tuple[List[Dict[str, object]], List[OrderLifecycleEvent]]:
        if self.settings.dry_run:
            return [], []
        results: List[Dict[str, object]] = []
        events: List[OrderLifecycleEvent] = []
        queried_ids: set[str] = set()

        for managed in list(self.live_open_orders.values()):
            if managed.symbol != symbol:
                continue
            queried_ids.add(managed.client_order_id)
            try:
                raw = self.client.query_order(
                    managed.symbol,
                    order_id=managed.external_order_id or None,
                    client_order_id=managed.client_order_id or None,
                )
                updated = self._managed_from_live_payload(raw, managed, timestamp_ms)
                event_type = self._event_type_for_status(updated.status)
                events.append(
                    self._event_from_managed(
                        updated,
                        timestamp_ms,
                        event_type=event_type,
                        reason="live_order_status_query",
                    )
                )
                if updated.status in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                    self.live_open_orders.pop(managed.client_order_id, None)
                    results.append(
                        {
                            "status": f"LIVE_{updated.status}",
                            "symbol": updated.symbol,
                            "side": updated.side,
                            "quantity": updated.quantity,
                            "filled_quantity": updated.filled_quantity,
                            "fill_price": updated.average_fill_price,
                            "client_order_id": updated.client_order_id,
                            "external_order_id": updated.external_order_id,
                            "trigger": updated.trigger,
                        }
                    )
                else:
                    if self._is_stale(updated, timestamp_ms):
                        events.append(
                            self._event_from_managed(
                                updated,
                                timestamp_ms,
                                event_type="STALE",
                                reason="order_stale_observed",
                            )
                        )
                    self.live_open_orders[updated.client_order_id] = updated
            except Exception as exc:  # noqa: BLE001
                unknown = self._replace_managed_order(
                    managed,
                    status="UNKNOWN",
                    updated_at_ms=timestamp_ms,
                    last_reason=str(exc),
                )
                self.live_open_orders[unknown.client_order_id] = unknown
                events.append(
                    self._event_from_managed(
                        unknown,
                        timestamp_ms,
                        event_type="UNKNOWN",
                        reason=str(exc),
                    )
                )

        try:
            for raw in self.client.get_open_orders(symbol):
                managed = self._managed_from_live_payload(raw, None, timestamp_ms)
                if managed.client_order_id:
                    self.live_open_orders[managed.client_order_id] = managed
                if managed.client_order_id not in queried_ids:
                    events.append(
                        self._event_from_managed(
                            managed,
                            timestamp_ms,
                            event_type="SYNC",
                            reason="live_open_order_sync",
                        )
                    )
        except Exception as exc:  # noqa: BLE001
            events.append(
                OrderLifecycleEvent(
                    timestamp_ms=timestamp_ms,
                    symbol=symbol,
                    client_order_id="",
                    event_type="UNKNOWN",
                    status="UNKNOWN",
                    side="",
                    quantity=0.0,
                    limit_price=0.0,
                    reason=str(exc),
                )
            )
        return results, events

    @staticmethod
    def _matching_candle(order: ManagedOrder, candles: List[Candle]) -> Candle | None:
        for candle in candles:
            if order.entry_candle_close_time and candle.close_time <= order.entry_candle_close_time:
                continue
            if order.side == "BUY" and candle.low <= order.limit_price:
                return candle
            if order.side == "SELL" and candle.high >= order.limit_price:
                return candle
        return None

    def classify_open_order_action(
        self,
        order: ManagedOrder,
        *,
        current_price: float,
        timestamp_ms: int,
        signal_action: str = "",
        ai_allow_entry: bool = True,
    ) -> Dict[str, object]:
        side = order.side.upper()
        signal = signal_action.upper()
        stale = self._is_stale(order, timestamp_ms)
        if order.status == "UNKNOWN":
            return {"action": "UNKNOWN_WAIT", "reason": "order_status_unknown_wait", "is_stale": stale}
        if side == "BUY" and not ai_allow_entry:
            return {"action": "CANCEL", "reason": "ai_risk_worsened_cancel_open_buy", "is_stale": stale}
        if side == "BUY" and signal == "SELL":
            return {"action": "CANCEL", "reason": "signal_reversed_cancel_open_buy", "is_stale": stale}
        if side == "SELL" and signal == "BUY":
            return {"action": "CANCEL", "reason": "signal_reversed_cancel_open_sell", "is_stale": stale}
        if self.settings.order_reprice_enabled and self._should_reprice_for_deviation(order, current_price):
            reason = "order_stale_reprice_requested" if stale else "order_reprice_deviation_requested"
            return {"action": "REPRICE", "reason": reason, "is_stale": stale}
        if stale:
            return {"action": "KEEP", "reason": "order_stale_observed", "is_stale": stale}
        return {"action": "KEEP", "reason": "open_order_waiting_for_touch", "is_stale": stale}

    def _should_reprice_for_deviation(self, order: ManagedOrder, current_price: float) -> bool:
        threshold = max(0.0, self.settings.order_reprice_deviation_pct)
        if threshold <= 0 or order.limit_price <= 0 or current_price <= 0:
            return False
        if order.side == "BUY":
            return current_price > order.limit_price * (1.0 + threshold)
        return current_price < order.limit_price * (1.0 - threshold)

    @staticmethod
    def _is_stale(order: ManagedOrder, timestamp_ms: int) -> bool:
        return bool(order.expires_at_ms and timestamp_ms >= order.expires_at_ms)

    @staticmethod
    def _normalize_live_status(status: str) -> str:
        normalized = status.upper()
        if normalized == "NEW":
            return "OPEN"
        if normalized in {"PARTIALLY_FILLED", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
            return normalized
        return "UNKNOWN"

    def _managed_from_live_payload(
        self,
        payload: Dict[str, object],
        fallback: OrderRequest | ManagedOrder | None,
        timestamp_ms: int,
        entry_candle_close_time_ms: int | None = None,
    ) -> ManagedOrder:
        fallback_client_id = fallback.client_order_id if fallback is not None else ""
        fallback_symbol = fallback.symbol if fallback is not None else ""
        fallback_side = fallback.side if fallback is not None else ""
        fallback_quantity = fallback.quantity if fallback is not None else 0.0
        fallback_limit = fallback.limit_price if fallback is not None else 0.0
        fallback_tif = fallback.time_in_force if fallback is not None else "GTC"
        fallback_trigger = fallback.trigger if fallback is not None else ""
        fallback_expires = fallback.expires_at_ms if fallback is not None else 0
        fallback_external = fallback.external_order_id if isinstance(fallback, ManagedOrder) else ""
        fallback_created = fallback.created_at_ms if isinstance(fallback, ManagedOrder) else timestamp_ms
        fallback_entry_close = fallback.entry_candle_close_time if isinstance(fallback, ManagedOrder) else 0

        client_order_id = str(payload.get("clientOrderId") or fallback_client_id)
        quantity = self._to_float(payload.get("origQty"), fallback_quantity)
        filled_quantity = self._to_float(payload.get("executedQty"), 0.0)
        cumulative_quote = self._to_float(
            payload.get("cummulativeQuoteQty", payload.get("cumulativeQuoteQty")),
            0.0,
        )
        limit_price = self._to_float(payload.get("price"), fallback_limit)
        average_fill_price = cumulative_quote / filled_quantity if filled_quantity > 0 and cumulative_quote > 0 else 0.0
        if average_fill_price <= 0 and filled_quantity > 0:
            average_fill_price = limit_price
        return ManagedOrder(
            client_order_id=client_order_id,
            symbol=str(payload.get("symbol") or fallback_symbol),
            side=str(payload.get("side") or fallback_side),
            order_type="LIMIT",
            quantity=quantity,
            limit_price=limit_price,
            time_in_force=str(payload.get("timeInForce") or fallback_tif or "GTC"),
            status=self._normalize_live_status(str(payload.get("status", "NEW"))),
            created_at_ms=fallback_created,
            updated_at_ms=timestamp_ms,
            expires_at_ms=fallback_expires,
            trigger=fallback_trigger,
            external_order_id=str(payload.get("orderId") or fallback_external),
            filled_quantity=filled_quantity,
            remaining_quantity=max(quantity - filled_quantity, 0.0),
            average_fill_price=average_fill_price,
            entry_candle_close_time=entry_candle_close_time_ms or fallback_entry_close,
            last_reason="live_order_payload",
        )

    @staticmethod
    def _replace_managed_order(order: ManagedOrder, **updates: object) -> ManagedOrder:
        fields = {
            "client_order_id": order.client_order_id,
            "symbol": order.symbol,
            "side": order.side,
            "order_type": order.order_type,
            "quantity": order.quantity,
            "limit_price": order.limit_price,
            "time_in_force": order.time_in_force,
            "status": order.status,
            "created_at_ms": order.created_at_ms,
            "updated_at_ms": order.updated_at_ms,
            "expires_at_ms": order.expires_at_ms,
            "trigger": order.trigger,
            "external_order_id": order.external_order_id,
            "filled_quantity": order.filled_quantity,
            "remaining_quantity": order.remaining_quantity,
            "average_fill_price": order.average_fill_price,
            "reserved_quote": order.reserved_quote,
            "reserved_base": order.reserved_base,
            "entry_candle_close_time": order.entry_candle_close_time,
            "last_reason": order.last_reason,
        }
        fields.update(updates)
        return ManagedOrder(**fields)

    @staticmethod
    def _event_type_for_status(status: str) -> str:
        if status in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "UNKNOWN"}:
            return status
        if status == "PARTIALLY_FILLED":
            return "PARTIALLY_FILLED"
        return "SYNC"

    @staticmethod
    def _event_from_managed(
        managed: ManagedOrder,
        timestamp_ms: int,
        *,
        event_type: str,
        reason: str,
    ) -> OrderLifecycleEvent:
        return OrderLifecycleEvent(
            timestamp_ms=timestamp_ms,
            symbol=managed.symbol,
            client_order_id=managed.client_order_id,
            event_type=event_type,
            status=managed.status,
            side=managed.side,
            quantity=managed.quantity,
            limit_price=managed.limit_price,
            fill_price=managed.average_fill_price if managed.status == "FILLED" else 0.0,
            filled_quantity=managed.filled_quantity,
            reason=reason,
            trigger=managed.trigger,
            external_order_id=managed.external_order_id,
        )

    @staticmethod
    def _to_float(value: object, default: float = 0.0) -> float:
        try:
            if value is None or value == "":
                return default
            return float(value)
        except (TypeError, ValueError):
            return default
