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
    trading_fee_rate: float
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
    mtf_entry_interval: str = "15m"
    mtf_entry_fast_window: int = 12
    mtf_entry_slow_window: int = 26
    mtf_trend_interval: str = "4h"
    mtf_trend_fast_window: int = 20
    mtf_trend_slow_window: int = 50
    decision_price_move_threshold_pct: float = 0.005
    position_activation_enabled: bool = True
    position_activation_mode: str = "active_grid"
    grid_sell_step_pct: float = 0.003
    grid_buyback_step_pct: float = 0.0025
    grid_buyback_tiers: str = ""
    grid_sell_fraction: float = 0.25
    grid_min_core_position_fraction: float = 0.25
    grid_max_daily_trades: int = 8
    grid_allow_loss_recovery_sell: bool = True
    grid_loss_recovery_sell_step_pct: float = 0.003
    llm_fallback_enabled: bool = True
    llm_fallback_provider: str = "ollama"
    llm_fallback_base_url: str = ""
    llm_fallback_model: str = "qwen3:14b"
    llm_fallback_timeout_seconds: int = 30
    llm_fallback_num_predict: int = 512
    order_execution_mode: str = "limit_lifecycle"
    order_time_in_force: str = "GTC"
    order_ttl_seconds: int = 180
    order_stale_action: str = "observe"
    order_reprice_enabled: bool = True
    order_reprice_deviation_pct: float = 0.003
    order_cancel_deviation_pct: float = 0.003
    order_passive_offset_pct: float = 0.0002
    order_urgent_cross_pct: float = 0.001
    order_max_open_per_symbol: int = 6
    order_max_open_per_side: int = 3
    order_ladder_enabled: bool = True
    target_position_fraction: float = 0.60
    min_cash_reserve_fraction: float = 0.10
    entry_ladder_tiers: str = "0.0015:0.25,0.0025:0.25,0.0040:0.25"
    exit_ladder_tiers: str = "0.0035:0.25,0.0060:0.25,0.0080:0.25"
    live_order_execution_enabled: bool = False
    min_net_edge_pct: float = 0.001
    buyback_cooldown_bars: int = 5
    buyback_cooldown_allow_emergency_stop: bool = True
    exit_stop_loss_fraction: float = 0.5
    exit_emergency_stop_fraction: float = 1.0
    emergency_stop_confirmation_enabled: bool = True
    exit_trailing_stop_fraction: float = 0.5
    exit_take_profit_fraction: float = 0.5
    exit_max_hold_fraction: float = 0.25
    strategy_sell_fraction: float = 0.5
    ai_can_cancel_buyback: bool = False
    ai_extreme_risk_cancel_buyback: bool = True
    cash_rebuild_enabled: bool = True
    cash_rebuild_max_position_fraction: float = 0.6
    cash_rebuild_min_cash_fraction: float = 0.1
    dynamic_exit_enabled: bool = True
    dynamic_exit_min_multiplier: float = 0.75
    dynamic_exit_max_multiplier: float = 1.35
    dynamic_stop_max_multiplier: float = 1.25
    dynamic_exit_strong_trend_threshold: float = 0.004
    dynamic_exit_volume_lookback: int = 20

    @property
    def active_symbol_limit(self) -> Optional[int]:
        if self.max_active_symbols <= 0:
            return None
        return self.max_active_symbols

    @property
    def llm_enabled(self) -> bool:
        primary_enabled = bool(self.llm_base_url and self.llm_api_key and self.llm_model)
        fallback_enabled = bool(self.llm_fallback_enabled and self.llm_fallback_base_url and self.llm_fallback_model)
        return primary_enabled or fallback_enabled


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
        mtf_entry_interval=os.getenv("MTF_ENTRY_INTERVAL", "15m").strip(),
        mtf_entry_fast_window=int(os.getenv("MTF_ENTRY_FAST_WINDOW", "12")),
        mtf_entry_slow_window=int(os.getenv("MTF_ENTRY_SLOW_WINDOW", "26")),
        mtf_trend_interval=os.getenv("MTF_TREND_INTERVAL", "4h").strip(),
        mtf_trend_fast_window=int(os.getenv("MTF_TREND_FAST_WINDOW", "20")),
        mtf_trend_slow_window=int(os.getenv("MTF_TREND_SLOW_WINDOW", "50")),
        risk_per_trade=float(os.getenv("RISK_PER_TRADE", "0.10")),
        min_order_notional=float(os.getenv("MIN_ORDER_NOTIONAL", "25")),
        trading_fee_rate=float(os.getenv("TRADING_FEE_RATE", "0.001")),
        paper_quote_balance=float(os.getenv("PAPER_QUOTE_BALANCE", "1000")),
        dry_run=_parse_bool(os.getenv("DRY_RUN"), True),
        llm_base_url=_normalize_base_url(os.getenv("LLM_BASE_URL", "").strip()),
        llm_api_key=os.getenv("LLM_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "gpt-5.5").strip(),
        llm_timeout_seconds=int(os.getenv("LLM_TIMEOUT_SECONDS", "20")),
        llm_fallback_enabled=_parse_bool(os.getenv("LLM_FALLBACK_ENABLED"), True),
        llm_fallback_provider=os.getenv("LLM_FALLBACK_PROVIDER", "ollama").strip().lower(),
        llm_fallback_base_url=_normalize_base_url(os.getenv("LLM_FALLBACK_BASE_URL", "http://ccnode.briconbric.com:22545").strip()),
        llm_fallback_model=os.getenv("LLM_FALLBACK_MODEL", "qwen3:14b").strip(),
        llm_fallback_timeout_seconds=int(os.getenv("LLM_FALLBACK_TIMEOUT_SECONDS", "30")),
        llm_fallback_num_predict=int(os.getenv("LLM_FALLBACK_NUM_PREDICT", "512")),
        news_refresh_seconds=int(os.getenv("NEWS_REFRESH_SECONDS", "120")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.01")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.02")),
        trailing_stop_pct=float(os.getenv("TRAILING_STOP_PCT", "0.0075")),
        max_hold_bars=int(os.getenv("MAX_HOLD_BARS", "24")),
        decision_price_move_threshold_pct=float(os.getenv("DECISION_PRICE_MOVE_THRESHOLD_PCT", "0.005")),
        position_activation_enabled=_parse_bool(os.getenv("POSITION_ACTIVATION_ENABLED"), True),
        position_activation_mode=os.getenv("POSITION_ACTIVATION_MODE", "active_grid").strip(),
        grid_sell_step_pct=float(os.getenv("GRID_SELL_STEP_PCT", "0.003")),
        grid_buyback_step_pct=float(os.getenv("GRID_BUYBACK_STEP_PCT", "0.0025")),
        grid_buyback_tiers=os.getenv("GRID_BUYBACK_TIERS", "").strip(),
        grid_sell_fraction=float(os.getenv("GRID_SELL_FRACTION", "0.25")),
        grid_min_core_position_fraction=float(os.getenv("GRID_MIN_CORE_POSITION_FRACTION", "0.25")),
        grid_max_daily_trades=int(os.getenv("GRID_MAX_DAILY_TRADES", "8")),
        grid_allow_loss_recovery_sell=_parse_bool(os.getenv("GRID_ALLOW_LOSS_RECOVERY_SELL"), True),
        grid_loss_recovery_sell_step_pct=float(os.getenv("GRID_LOSS_RECOVERY_SELL_STEP_PCT", "0.003")),
        order_execution_mode=os.getenv("ORDER_EXECUTION_MODE", "limit_lifecycle").strip(),
        order_time_in_force=os.getenv("ORDER_TIME_IN_FORCE", "GTC").strip().upper(),
        order_ttl_seconds=int(os.getenv("ORDER_STALE_SECONDS", os.getenv("ORDER_TTL_SECONDS", "180"))),
        order_stale_action=os.getenv("ORDER_STALE_ACTION", "observe").strip().lower(),
        order_reprice_enabled=_parse_bool(os.getenv("ORDER_REPRICE_ENABLED"), True),
        order_reprice_deviation_pct=float(os.getenv("ORDER_REPRICE_DEVIATION_PCT", os.getenv("ORDER_CANCEL_DEVIATION_PCT", "0.003"))),
        order_cancel_deviation_pct=float(os.getenv("ORDER_CANCEL_DEVIATION_PCT", "0.003")),
        order_passive_offset_pct=float(os.getenv("ORDER_PASSIVE_OFFSET_PCT", "0.0002")),
        order_urgent_cross_pct=float(os.getenv("ORDER_URGENT_CROSS_PCT", "0.001")),
        order_max_open_per_symbol=int(os.getenv("ORDER_MAX_OPEN_PER_SYMBOL", "6")),
        order_max_open_per_side=int(os.getenv("ORDER_MAX_OPEN_PER_SIDE", "3")),
        order_ladder_enabled=_parse_bool(os.getenv("ORDER_LADDER_ENABLED"), True),
        target_position_fraction=float(os.getenv("TARGET_POSITION_FRACTION", "0.60")),
        min_cash_reserve_fraction=float(os.getenv("MIN_CASH_RESERVE_FRACTION", "0.10")),
        entry_ladder_tiers=os.getenv("ENTRY_LADDER_TIERS", "0.0015:0.25,0.0025:0.25,0.0040:0.25").strip(),
        exit_ladder_tiers=os.getenv("EXIT_LADDER_TIERS", "0.0035:0.25,0.0060:0.25,0.0080:0.25").strip(),
        live_order_execution_enabled=_parse_bool(os.getenv("LIVE_ORDER_EXECUTION_ENABLED"), False),
        min_net_edge_pct=float(os.getenv("MIN_NET_EDGE_PCT", "0.001")),
        buyback_cooldown_bars=int(os.getenv("BUYBACK_COOLDOWN_BARS", "5")),
        buyback_cooldown_allow_emergency_stop=_parse_bool(os.getenv("BUYBACK_COOLDOWN_ALLOW_EMERGENCY_STOP"), True),
        exit_stop_loss_fraction=float(os.getenv("EXIT_STOP_LOSS_FRACTION", "0.5")),
        exit_emergency_stop_fraction=float(os.getenv("EXIT_EMERGENCY_STOP_FRACTION", "1.0")),
        emergency_stop_confirmation_enabled=_parse_bool(os.getenv("EMERGENCY_STOP_CONFIRMATION_ENABLED"), True),
        exit_trailing_stop_fraction=float(os.getenv("EXIT_TRAILING_STOP_FRACTION", "0.5")),
        exit_take_profit_fraction=float(os.getenv("EXIT_TAKE_PROFIT_FRACTION", "0.5")),
        exit_max_hold_fraction=float(os.getenv("EXIT_MAX_HOLD_FRACTION", "0.25")),
        strategy_sell_fraction=float(os.getenv("STRATEGY_SELL_FRACTION", "0.5")),
        ai_can_cancel_buyback=_parse_bool(os.getenv("AI_CAN_CANCEL_BUYBACK"), False),
        ai_extreme_risk_cancel_buyback=_parse_bool(os.getenv("AI_EXTREME_RISK_CANCEL_BUYBACK"), True),
        cash_rebuild_enabled=_parse_bool(os.getenv("CASH_REBUILD_ENABLED"), True),
        cash_rebuild_max_position_fraction=float(os.getenv("CASH_REBUILD_MAX_POSITION_FRACTION", "0.6")),
        cash_rebuild_min_cash_fraction=float(os.getenv("CASH_REBUILD_MIN_CASH_FRACTION", "0.1")),
        dynamic_exit_enabled=_parse_bool(os.getenv("DYNAMIC_EXIT_ENABLED"), True),
        dynamic_exit_min_multiplier=float(os.getenv("DYNAMIC_EXIT_MIN_MULTIPLIER", "0.75")),
        dynamic_exit_max_multiplier=float(os.getenv("DYNAMIC_EXIT_MAX_MULTIPLIER", "1.35")),
        dynamic_stop_max_multiplier=float(os.getenv("DYNAMIC_STOP_MAX_MULTIPLIER", "1.25")),
        dynamic_exit_strong_trend_threshold=float(os.getenv("DYNAMIC_EXIT_STRONG_TREND_THRESHOLD", "0.004")),
        dynamic_exit_volume_lookback=int(os.getenv("DYNAMIC_EXIT_VOLUME_LOOKBACK", "20")),
    )
