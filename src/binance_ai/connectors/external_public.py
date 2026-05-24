from __future__ import annotations

import json
import time
from typing import Any, Dict, List
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class PublicRestError(RuntimeError):
    pass


class PublicRestClient:
    def __init__(self, base_url: str, timeout_seconds: int = 8) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        query = f"?{urlencode(params)}" if params else ""
        request = Request(f"{self.base_url}{path}{query}", headers={"User-Agent": "Botinance/1.0"})
        started = time.monotonic()
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                payload = response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError) as exc:
            raise PublicRestError(str(exc)) from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PublicRestError("invalid_json") from exc
        if isinstance(data, dict):
            data.setdefault("_source_latency_ms", int((time.monotonic() - started) * 1000))
        return data


class BinanceFuturesPublicClient(PublicRestClient):
    def __init__(self, timeout_seconds: int = 8) -> None:
        super().__init__("https://fapi.binance.com", timeout_seconds=timeout_seconds)

    def premium_index(self, symbol: str) -> Dict[str, Any]:
        return self._get("/fapi/v1/premiumIndex", {"symbol": symbol})

    def open_interest(self, symbol: str) -> Dict[str, Any]:
        return self._get("/fapi/v1/openInterest", {"symbol": symbol})

    def open_interest_history(self, symbol: str, period: str = "5m", limit: int = 2) -> List[Dict[str, Any]]:
        payload = self._get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})
        return payload if isinstance(payload, list) else []

    def long_short_ratio(self, symbol: str, period: str = "5m", limit: int = 2) -> List[Dict[str, Any]]:
        payload = self._get("/futures/data/globalLongShortAccountRatio", {"symbol": symbol, "period": period, "limit": limit})
        return payload if isinstance(payload, list) else []

    def taker_buy_sell_ratio(self, symbol: str, period: str = "5m", limit: int = 2) -> List[Dict[str, Any]]:
        payload = self._get("/futures/data/takerlongshortRatio", {"symbol": symbol, "period": period, "limit": limit})
        return payload if isinstance(payload, list) else []


class OkxPublicClient(PublicRestClient):
    def __init__(self, timeout_seconds: int = 8) -> None:
        super().__init__("https://www.okx.com", timeout_seconds=timeout_seconds)

    def mark_price(self, symbol: str) -> Dict[str, Any]:
        return self._get("/api/v5/public/mark-price", {"instType": "SWAP", "instId": symbol})

    def open_interest(self, symbol: str) -> Dict[str, Any]:
        return self._get("/api/v5/public/open-interest", {"instType": "SWAP", "instId": symbol})

    def funding_rate(self, symbol: str) -> Dict[str, Any]:
        return self._get("/api/v5/public/funding-rate", {"instId": symbol})

    def long_short_ratio(self, currency: str = "XRP", period: str = "5m") -> Dict[str, Any]:
        return self._get("/api/v5/rubik/stat/contracts/long-short-account-ratio", {"ccy": currency, "period": period})

    def taker_volume(self, currency: str = "XRP", period: str = "5m") -> Dict[str, Any]:
        return self._get("/api/v5/rubik/stat/taker-volume", {"ccy": currency, "instType": "SWAP", "period": period})


class BybitPublicClient(PublicRestClient):
    def __init__(self, timeout_seconds: int = 8) -> None:
        super().__init__("https://api.bybit.com", timeout_seconds=timeout_seconds)

    def ticker(self, symbol: str) -> Dict[str, Any]:
        return self._get("/v5/market/tickers", {"category": "linear", "symbol": symbol})

    def open_interest(self, symbol: str, interval: str = "5min", limit: int = 2) -> Dict[str, Any]:
        return self._get("/v5/market/open-interest", {"category": "linear", "symbol": symbol, "intervalTime": interval, "limit": limit})

    def funding_history(self, symbol: str, limit: int = 1) -> Dict[str, Any]:
        return self._get("/v5/market/funding/history", {"category": "linear", "symbol": symbol, "limit": limit})

    def long_short_ratio(self, symbol: str, period: str = "5min", limit: int = 1) -> Dict[str, Any]:
        return self._get("/v5/market/account-ratio", {"category": "linear", "symbol": symbol, "period": period, "limit": limit})
