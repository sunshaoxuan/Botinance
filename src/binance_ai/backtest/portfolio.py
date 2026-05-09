from __future__ import annotations

from binance_ai.models import AccountSnapshot, OrderRequest, PortfolioSnapshot, PositionSnapshot
from binance_ai.paper.state_engine import PortfolioStateEngine


class BacktestPortfolioEngine:
    def __init__(self, quote_asset: str, initial_quote_balance: float) -> None:
        self.engine = PortfolioStateEngine(quote_asset)
        self.snapshot = PortfolioSnapshot(
            quote_asset=quote_asset,
            quote_balance=initial_quote_balance,
            initial_quote_balance=initial_quote_balance,
        )

    def account_snapshot(self) -> AccountSnapshot:
        return self.engine.account_snapshot(self.snapshot)

    def position_snapshot(self, symbol: str) -> PositionSnapshot | None:
        return self.snapshot.positions.get(symbol)

    def mark_to_market(
        self,
        symbol: str,
        mark_price: float,
        timestamp_ms: int,
        candle_close_time_ms: int,
    ) -> None:
        self.snapshot = self.engine.mark_to_market(
            self.snapshot,
            symbol=symbol,
            mark_price=mark_price,
            timestamp_ms=timestamp_ms,
            candle_close_time_ms=candle_close_time_ms,
        )

    def apply_order(
        self,
        order: OrderRequest,
        fill_price: float,
        min_notional: float | None = None,
        min_qty: float | None = None,
        timestamp_ms: int | None = None,
        entry_candle_close_time_ms: int | None = None,
    ) -> dict[str, object]:
        self.snapshot, result = self.engine.apply_order(
            self.snapshot,
            order,
            fill_price,
            min_notional=min_notional,
            min_qty=min_qty,
            timestamp_ms=timestamp_ms,
            entry_candle_close_time_ms=entry_candle_close_time_ms,
        )
        return result

    def equity_summary(self, mark_prices: dict[str, float]) -> dict[str, float]:
        return self.engine.equity_summary(self.snapshot, mark_prices)
