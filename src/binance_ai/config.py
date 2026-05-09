from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlparse

from binance_ai.secrets import load_encrypted_secrets, parse_env_file


def _parse_bool(value: str, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _load_config_environment(path: Path) -> None:
    public_values = parse_env_file(path)
    for key, value in public_values.items():
        os.environ.setdefault(key, value)

    secret_values = load_encrypted_secrets(public_values, path)
    for key, value in secret_values.items():
        os.environ.setdefault(key, value)


def _normalize_base_url(value: str) -> str:
    raw = value.strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme:
        return f"http://{raw}".rstrip("/")
    return raw.rstrip("/")


@dataclass(frozen=True)
class Settings:
    api_key: str
    api_secret: str
    base_url: str
    recv_window: int
    trading_symbols: List[str]
    max_active_symbols: int
    quote_asset: str
    kline_interval: str
    kline_limit: int
    fast_window: int
    slow_window: int
    risk_per_trade: float
    min_order_notional: float
    paper_quote_balance: float
    dry_run: bool
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_timeout_seconds: int
    news_refresh_seconds: int
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    max_hold_bars: int
    decision_price_move_threshold_pct: float = 0.005

    @property
    def active_symbol_limit(self) -> Optional[int]:
        if self.max_active_symbols <= 0:
            return None
        return self.max_active_symbols

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)


def load_settings() -> Settings:
    _load_config_environment(Path(".env"))

    symbols = [
        symbol.strip().upper()
        for symbol in os.getenv("TRADING_SYMBOLS", "BTCUSDT,ETHUSDT,BNBUSDT").split(",")
        if symbol.strip()
    ]

    return Settings(
        api_key=os.getenv("BINANCE_API_KEY", "").strip(),
        api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
        base_url=os.getenv("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
        recv_window=int(os.getenv("BINANCE_RECV_WINDOW", "5000")),
        trading_symbols=symbols,
        max_active_symbols=int(os.getenv("MAX_ACTIVE_SYMBOLS", "3")),
        quote_asset=os.getenv("QUOTE_ASSET", "USDT").strip().upper(),
        kline_interval=os.getenv("KLINE_INTERVAL", "1h").strip(),
        kline_limit=int(os.getenv("KLINE_LIMIT", "250")),
        fast_window=int(os.getenv("FAST_WINDOW", "20")),
        slow_window=int(os.getenv("SLOW_WINDOW", "50")),
        risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.10")),
        min_order_notional=float(os.getenv("MIN_ORDER_NOTIONAL", "25")),
        paper_quote_balance=float(os.getenv("PAPER_QUOTE_BALANCE", "1000")),
        dry_run=_parse_bool(os.getenv("DRY_RUN"), True),
        llm_base_url=_normalize_base_url(os.getenv("LLM_BASE_URL", "").strip()),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "gpt-5.5").strip(),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "20")),
        news_refresh_seconds=int(os.getenv("NEWS_REFRESH_SECONDS", "120")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.01")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.02")),
        trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "0.0075")),
        max_hold_bars=int(os.getenv("MAX_HOLD_BARS", "24")),
        decision_price_move_threshold_pct=float(os.getenv("DECISION_PRICE_MOVE_THRESHOLD_PCT", "0.005")),
    )
