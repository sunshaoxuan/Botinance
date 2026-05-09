from __future__ import annotations

from typing import Dict

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import OrderRequest, SymbolFilters
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

    def execute(
        self,
        order: OrderRequest,
        fill_price: float,
        filters: SymbolFilters | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> Dict[str, object]:
        symbol_filters = filters or self.client.get_symbol_filters(order.symbol)
        validation = self._validate_exchange_rules(order, fill_price, symbol_filters)
        if validation is not None:
            return validation

        if self.settings.dry_run:
            if self.paper_portfolio is None:
                raise RuntimeError("Dry-run execution requires a paper portfolio.")
            return self.paper_portfolio.apply_order(
                order,
                fill_price,
                min_notional=max(symbol_filters.min_notional, self.settings.min_order_notional),
                min_qty=symbol_filters.min_qty,
                timestamp_ms=timestamp_ms,
                entry_candle_close_time_ms=entry_candle_close_time_ms,
            )
        payload = self.client.place_market_order(order)
        return {
            "status": "LIVE_ORDER_SUBMITTED",
            "response": payload,
        }

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
