from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
from typing import Any, Dict, List
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from binance_ai.config import Settings
from binance_ai.models import AccountSnapshot, Candle, OrderRequest, SymbolFilters


class BinanceSpotClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def close(self) -> None:
        return None

    def _request(
        self,
        method: str,
        path: str,
        params: Dict[str, Any] | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Any:
        query = urlencode(params or {})
        url = f"{self.settings.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        request = Request(url=url, method=method, headers=headers or {})
        with urlopen(request, timeout=10) as response:
            payload = response.read().decode("utf-8")
        return json.loads(payload)

    def _public_get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        return self._request("GET", path, params=params)

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Dict[str, Any] | None = None,
    ) -> Any:
        if not self.settings.api_key or not self.settings.api_secret:
            raise RuntimeError("Signed request requires BINANCE_API_KEY and BINANCE_API_SECRET.")

        query = dict(params or {})
        query["recvWindow"] = self.settings.recv_window
        query["timestamp"] = int(time.time() * 1000)

        encoded = urlencode(query)
        signature = hmac.new(
            self.settings.api_secret.encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        query["signature"] = signature

        headers = {"X-MBX-APIKEY": self.settings.api_key}
        return self._request(method, path, params=query, headers=headers)

    def get_klines(self, symbol: str, interval: str, limit: int) -> List[Candle]:
        rows = self._public_get(
            "/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        return self._parse_klines(rows)

    def get_klines_range(
        self,
        symbol: str,
        interval: str,
        start_time_ms: int,
        end_time_ms: int | None = None,
        limit: int = 1000,
    ) -> List[Candle]:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "interval": interval,
            "startTime": start_time_ms,
            "limit": limit,
        }
        if end_time_ms is not None:
            params["endTime"] = end_time_ms
        rows = self._public_get("/api/v3/klines", params=params)
        return self._parse_klines(rows)

    @staticmethod
    def _parse_klines(rows: List[List[Any]]) -> List[Candle]:
        return [
            Candle(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                close_time=int(row[6]),
            )
            for row in rows
        ]

    def get_symbol_price(self, symbol: str) -> float:
        payload = self._public_get("/api/v3/ticker/price", params={"symbol": symbol})
        return float(payload["price"])

    def get_account_snapshot(self) -> AccountSnapshot:
        return AccountSnapshot(balances=self.get_account_balances(include_locked=False))

    def get_account_balances(self, include_locked: bool = False) -> Dict[str, float]:
        payload = self._signed_request("GET", "/api/v3/account")
        balances: Dict[str, float] = {}
        for item in payload.get("balances", []):
            free = float(item.get("free", 0.0))
            locked = float(item.get("locked", 0.0)) if include_locked else 0.0
            total = free + locked
            if total > 0:
                balances[item["asset"]] = total
        return balances

    def get_my_trades(self, symbol: str, limit: int = 1000) -> List[Dict[str, Any]]:
        payload = self._signed_request(
            "GET",
            "/api/v3/myTrades",
            params={"symbol": symbol, "limit": limit},
        )
        return list(payload)

    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        payload = self._public_get("/api/v3/exchangeInfo", params={"symbol": symbol})
        symbol_info = payload["symbols"][0]

        lot_size = next(item for item in symbol_info["filters"] if item["filterType"] == "LOT_SIZE")
        min_notional_filter = next(
            item
            for item in symbol_info["filters"]
            if item["filterType"] in {"MIN_NOTIONAL", "NOTIONAL"}
        )
        return SymbolFilters(
            symbol=symbol,
            step_size=float(lot_size["stepSize"]),
            min_qty=float(lot_size["minQty"]),
            min_notional=float(min_notional_filter["minNotional"]),
        )

    def place_market_order(self, order: OrderRequest) -> Dict[str, Any]:
        return self._signed_request(
            "POST",
            "/api/v3/order",
            params={
                "symbol": order.symbol,
                "side": order.side,
                "type": order.order_type,
                "quantity": self._format_quantity(order.quantity),
            },
        )

    @staticmethod
    def quantize_quantity(quantity: float, step_size: float) -> float:
        if step_size <= 0:
            return quantity
        precision = max(0, round(-math.log10(step_size)))
        steps = math.floor(quantity / step_size)
        quantized = steps * step_size
        return round(quantized, precision)

    @staticmethod
    def _format_quantity(quantity: float) -> str:
        text = f"{quantity:.12f}"
        text = text.rstrip("0").rstrip(".")
        return text or "0"
