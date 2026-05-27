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
    grid_min_order_notional: float = 0.0
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
    order_reprice_tolerance_pct: float = 0.002
    order_reprice_min_age_seconds: int = 120
    order_reprice_compare_mode: str = "tier_spread"
    order_passive_offset_pct: float = 0.0002
    order_urgent_cross_pct: float = 0.001
    order_max_open_per_symbol: int = 6
    order_max_open_per_side: int = 5
    order_ladder_enabled: bool = True
    pair_market_making_enabled: bool = True
    order_levels_per_side: int = 5
    pair_spread_levels: str = "0.0035,0.0055,0.0080,0.0110,0.0150"
    target_position_fraction: float = 0.60
    target_inventory_enabled: bool = True
    target_position_strong_down: float = 0.20
    target_position_weak_down: float = 0.35
    target_position_range: float = 0.55
    target_position_strong_up: float = 0.70
    target_position_emergency: float = 0.05
    target_position_band_pct: float = 0.08
    min_cash_reserve_fraction: float = 0.10
    entry_ladder_tiers: str = "0.0025:0.50,0.0050:0.50"
    exit_ladder_tiers: str = "0.0040:0.50,0.0080:0.50"
    live_order_execution_enabled: bool = False
    min_net_edge_pct: float = 0.001
    min_effective_order_notional: float = 5000.0
    order_target_notional: float = 8000.0
    max_daily_turnover_fraction: float = 1.0
    max_daily_turnover_hard_block: bool = True
    max_daily_realized_loss_pct: float = 0.01
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
    composite_decision_enabled: bool = True
    buy_score_threshold: float = 0.62
    sell_score_threshold: float = 0.68
    risk_exit_score_threshold: float = 0.82
    entry_protection_bars: int = 8
    entry_protection_allow_emergency_stop: bool = True
    recovery_turnover_fraction: float = 0.5
    min_expected_net_edge_pct: float = 0.0025
    policy_engine_enabled: bool = True
    pair_lock_after_risk_exit_candles: int = 12
    pair_lock_require_trend_stable: bool = True
    pair_lock_require_net_edge: bool = True
    stoploss_guard_lookback_candles: int = 48
    stoploss_guard_trade_limit: int = 2
    stoploss_guard_lock_candles: int = 24
    max_drawdown_guard_pct: float = 0.015
    inventory_skew_enabled: bool = True
    inventory_target_base_pct: float = 0.55
    inventory_range_multiplier: float = 1.5
    order_proposal_min_net_edge_pct: float = 0.0025
    hanging_orders_enabled: bool = True
    hanging_orders_cancel_pct: float = 0.02
    maker_fee_pct: float = 0.001
    pair_edge_safety_buffer_pct: float = 0.0005
    cooldown_candles_after_pair_complete: int = 2
    low_profit_pair_lookback: int = 6
    low_profit_pair_min_avg_net_edge_pct: float = 0.0015
    low_profit_pair_lock_candles: int = 12
    direction_engine_enabled: bool = False
    legacy_direct_order_fallback: bool = True
    trend_follow_enabled: bool = False
    fair_value_method: str = "ema_vwap_blend"
    fair_value_lookback_bars: int = 60
    volatility_buffer_atr_multiplier: float = 0.35
    buy_zone_min_discount_pct: float = 0.0025
    sell_zone_min_premium_pct: float = 0.0025
    min_pair_net_edge_pct: float = 0.0045
    allow_risk_sell_below_sell_zone: bool = True
    scenario_engine_enabled: bool = True
    trend_probe_entry_fraction: float = 0.25
    recovery_entry_fraction: float = 0.20
    uptrend_expansion_min_periods: int = 2
    uptrend_exhaustion_gap_pct: float = 0.0015
    downtrend_buy_discount_multiplier: float = 1.8
    low_vol_atr_pct: float = 0.0008
    order_tier_merge_enabled: bool = True
    order_tier_merge_min_notional: float = 5000.0
    external_signal_enabled: bool = True
    external_signal_refresh_seconds: int = 60
    external_signal_stale_seconds: int = 180
    external_signal_timeout_seconds: int = 3
    external_signal_local_weight: float = 0.60
    external_signal_external_weight: float = 0.40
    external_signal_sources: str = "binance_futures,okx,bybit"
    external_symbol_binance_futures_xrpjpy: str = "XRPUSDT"
    external_symbol_okx_xrpjpy: str = "XRP-USDT-SWAP"
    external_symbol_bybit_xrpjpy: str = "XRPUSDT"
    external_signal_min_sources: int = 2
    external_signal_can_change_direction: bool = True
    external_signal_can_trigger_risk_off: bool = True
    db_enabled: bool = True
    db_driver: str = "postgres"
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "botinance"
    db_user: str = "botinance"
    db_password_env: str = "BOTINANCE_DB_PASSWORD"
    db_write_mode: str = "dual"
    db_read_mode: str = "prefer_db"
    db_query_timeout_seconds: int = 5
    db_partition_mode: str = "monthly"
    dashboard_order_source: str = "postgres"

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
        grid_min_order_notional=float(os.getenv("GRID_MIN_ORDER_NOTIONAL", "0")),
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
        order_reprice_tolerance_pct=float(os.getenv("ORDER_REPRICE_TOLERANCE_PCT", "0.0020")),
        order_reprice_min_age_seconds=int(os.getenv("ORDER_REPRICE_MIN_AGE_SECONDS", "120")),
        order_reprice_compare_mode=os.getenv("ORDER_REPRICE_COMPARE_MODE", "tier_spread").strip().lower(),
        order_passive_offset_pct=float(os.getenv("ORDER_PASSIVE_OFFSET_PCT", "0.0002")),
        order_urgent_cross_pct=float(os.getenv("ORDER_URGENT_CROSS_PCT", "0.001")),
        order_max_open_per_symbol=int(os.getenv("ORDER_MAX_OPEN_PER_SYMBOL", "6")),
        order_max_open_per_side=int(os.getenv("ORDER_MAX_OPEN_PER_SIDE", "5")),
        order_ladder_enabled=_parse_bool(os.getenv("ORDER_LADDER_ENABLED"), True),
        pair_market_making_enabled=_parse_bool(os.getenv("PAIR_MARKET_MAKING_ENABLED"), True),
        order_levels_per_side=int(os.getenv("ORDER_LEVELS_PER_SIDE", "5")),
        pair_spread_levels=os.getenv("PAIR_SPREAD_LEVELS", "0.0035,0.0055,0.0080,0.0110,0.0150").strip(),
        target_position_fraction=float(os.getenv("TARGET_POSITION_FRACTION", "0.60")),
        target_inventory_enabled=_parse_bool(os.getenv("TARGET_INVENTORY_ENABLED"), True),
        target_position_strong_down=float(os.getenv("TARGET_POSITION_STRONG_DOWN", "0.20")),
        target_position_weak_down=float(os.getenv("TARGET_POSITION_WEAK_DOWN", "0.35")),
        target_position_range=float(os.getenv("TARGET_POSITION_RANGE", "0.55")),
        target_position_strong_up=float(os.getenv("TARGET_POSITION_STRONG_UP", "0.70")),
        target_position_emergency=float(os.getenv("TARGET_POSITION_EMERGENCY", "0.05")),
        target_position_band_pct=float(os.getenv("TARGET_POSITION_BAND_PCT", "0.08")),
        min_cash_reserve_fraction=float(os.getenv("MIN_CASH_RESERVE_FRACTION", "0.10")),
        entry_ladder_tiers=os.getenv("ENTRY_LADDER_TIERS", "0.0025:0.50,0.0050:0.50").strip(),
        exit_ladder_tiers=os.getenv("EXIT_LADDER_TIERS", "0.0040:0.50,0.0080:0.50").strip(),
        live_order_execution_enabled=_parse_bool(os.getenv("LIVE_ORDER_EXECUTION_ENABLED"), False),
        min_net_edge_pct=float(os.getenv("MIN_NET_EDGE_PCT", "0.001")),
        min_effective_order_notional=float(os.getenv("MIN_EFFECTIVE_ORDER_NOTIONAL", "5000")),
        order_target_notional=float(os.getenv("ORDER_TARGET_NOTIONAL", "8000")),
        max_daily_turnover_fraction=float(os.getenv("MAX_DAILY_TURNOVER_FRACTION", "3.0")),
        max_daily_turnover_hard_block=_parse_bool(os.getenv("MAX_DAILY_TURNOVER_HARD_BLOCK"), False),
        max_daily_realized_loss_pct=float(os.getenv("MAX_DAILY_REALIZED_LOSS_PCT", "0.01")),
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
        composite_decision_enabled=_parse_bool(os.getenv("COMPOSITE_DECISION_ENABLED"), True),
        buy_score_threshold=float(os.getenv("BUY_SCORE_THRESHOLD", "0.62")),
        sell_score_threshold=float(os.getenv("SELL_SCORE_THRESHOLD", "0.68")),
        risk_exit_score_threshold=float(os.getenv("RISK_EXIT_SCORE_THRESHOLD", "0.82")),
        entry_protection_bars=int(os.getenv("ENTRY_PROTECTION_BARS", "8")),
        entry_protection_allow_emergency_stop=_parse_bool(os.getenv("ENTRY_PROTECTION_ALLOW_EMERGENCY_STOP"), True),
        recovery_turnover_fraction=float(os.getenv("RECOVERY_TURNOVER_FRACTION", "0.5")),
        min_expected_net_edge_pct=float(os.getenv("MIN_EXPECTED_NET_EDGE_PCT", os.getenv("MIN_NET_EDGE_PCT", "0.0025"))),
        policy_engine_enabled=_parse_bool(os.getenv("POLICY_ENGINE_ENABLED"), True),
        pair_lock_after_risk_exit_candles=int(os.getenv("PAIR_LOCK_AFTER_RISK_EXIT_CANDLES", "12")),
        pair_lock_require_trend_stable=_parse_bool(os.getenv("PAIR_LOCK_REQUIRE_TREND_STABLE"), True),
        pair_lock_require_net_edge=_parse_bool(os.getenv("PAIR_LOCK_REQUIRE_NET_EDGE"), True),
        stoploss_guard_lookback_candles=int(os.getenv("STOPLOSS_GUARD_LOOKBACK_CANDLES", "48")),
        stoploss_guard_trade_limit=int(os.getenv("STOPLOSS_GUARD_TRADE_LIMIT", "2")),
        stoploss_guard_lock_candles=int(os.getenv("STOPLOSS_GUARD_LOCK_CANDLES", "24")),
        max_drawdown_guard_pct=float(os.getenv("MAX_DRAWDOWN_GUARD_PCT", "0.015")),
        inventory_skew_enabled=_parse_bool(os.getenv("INVENTORY_SKEW_ENABLED"), True),
        inventory_target_base_pct=float(os.getenv("INVENTORY_TARGET_BASE_PCT", "0.55")),
        inventory_range_multiplier=float(os.getenv("INVENTORY_RANGE_MULTIPLIER", "1.5")),
        order_proposal_min_net_edge_pct=float(os.getenv("ORDER_PROPOSAL_MIN_NET_EDGE_PCT", os.getenv("MIN_EXPECTED_NET_EDGE_PCT", "0.0025"))),
        hanging_orders_enabled=_parse_bool(os.getenv("HANGING_ORDERS_ENABLED"), True),
        hanging_orders_cancel_pct=float(os.getenv("HANGING_ORDERS_CANCEL_PCT", "0.02")),
        maker_fee_pct=float(os.getenv("MAKER_FEE_PCT", os.getenv("TRADING_FEE_RATE", "0.001"))),
        pair_edge_safety_buffer_pct=float(os.getenv("PAIR_EDGE_SAFETY_BUFFER_PCT", "0.0005")),
        cooldown_candles_after_pair_complete=int(os.getenv("COOLDOWN_CANDLES_AFTER_PAIR_COMPLETE", "2")),
        low_profit_pair_lookback=int(os.getenv("LOW_PROFIT_PAIR_LOOKBACK", "6")),
        low_profit_pair_min_avg_net_edge_pct=float(os.getenv("LOW_PROFIT_PAIR_MIN_AVG_NET_EDGE_PCT", "0.0015")),
        low_profit_pair_lock_candles=int(os.getenv("LOW_PROFIT_PAIR_LOCK_CANDLES", "12")),
        direction_engine_enabled=_parse_bool(os.getenv("DIRECTION_ENGINE_ENABLED"), True),
        legacy_direct_order_fallback=_parse_bool(os.getenv("LEGACY_DIRECT_ORDER_FALLBACK"), False),
        trend_follow_enabled=_parse_bool(os.getenv("TREND_FOLLOW_ENABLED"), False),
        fair_value_method=os.getenv("FAIR_VALUE_METHOD", "ema_vwap_blend").strip().lower(),
        fair_value_lookback_bars=int(os.getenv("FAIR_VALUE_LOOKBACK_BARS", "60")),
        volatility_buffer_atr_multiplier=float(os.getenv("VOLATILITY_BUFFER_ATR_MULTIPLIER", "0.35")),
        buy_zone_min_discount_pct=float(os.getenv("BUY_ZONE_MIN_DISCOUNT_PCT", "0.0025")),
        sell_zone_min_premium_pct=float(os.getenv("SELL_ZONE_MIN_PREMIUM_PCT", "0.0025")),
        min_pair_net_edge_pct=float(os.getenv("MIN_PAIR_NET_EDGE_PCT", "0.0045")),
        allow_risk_sell_below_sell_zone=_parse_bool(os.getenv("ALLOW_RISK_SELL_BELOW_SELL_ZONE"), True),
        scenario_engine_enabled=_parse_bool(os.getenv("SCENARIO_ENGINE_ENABLED"), True),
        trend_probe_entry_fraction=float(os.getenv("TREND_PROBE_ENTRY_FRACTION", "0.25")),
        recovery_entry_fraction=float(os.getenv("RECOVERY_ENTRY_FRACTION", "0.20")),
        uptrend_expansion_min_periods=int(os.getenv("UPTREND_EXPANSION_MIN_PERIODS", "2")),
        uptrend_exhaustion_gap_pct=float(os.getenv("UPTREND_EXHAUSTION_GAP_PCT", "0.0015")),
        downtrend_buy_discount_multiplier=float(os.getenv("DOWNTREND_BUY_DISCOUNT_MULTIPLIER", "1.8")),
        low_vol_atr_pct=float(os.getenv("LOW_VOL_ATR_PCT", "0.0008")),
        order_tier_merge_enabled=_parse_bool(os.getenv("ORDER_TIER_MERGE_ENABLED"), True),
        order_tier_merge_min_notional=float(os.getenv("ORDER_TIER_MERGE_MIN_NOTIONAL", "5000")),
        external_signal_enabled=_parse_bool(os.getenv("EXTERNAL_SIGNAL_ENABLED"), True),
        external_signal_refresh_seconds=int(os.getenv("EXTERNAL_SIGNAL_REFRESH_SECONDS", "60")),
        external_signal_stale_seconds=int(os.getenv("EXTERNAL_SIGNAL_STALE_SECONDS", "180")),
        external_signal_timeout_seconds=int(os.getenv("EXTERNAL_SIGNAL_TIMEOUT_SECONDS", "3")),
        external_signal_local_weight=float(os.getenv("EXTERNAL_SIGNAL_LOCAL_WEIGHT", "0.60")),
        external_signal_external_weight=float(os.getenv("EXTERNAL_SIGNAL_EXTERNAL_WEIGHT", "0.40")),
        external_signal_sources=os.getenv("EXTERNAL_SIGNAL_SOURCES", "binance_futures,okx,bybit").strip(),
        external_symbol_binance_futures_xrpjpy=os.getenv("EXTERNAL_SYMBOL_BINANCE_FUTURES_XRPJPY", "XRPUSDT").strip().upper(),
        external_symbol_okx_xrpjpy=os.getenv("EXTERNAL_SYMBOL_OKX_XRPJPY", "XRP-USDT-SWAP").strip().upper(),
        external_symbol_bybit_xrpjpy=os.getenv("EXTERNAL_SYMBOL_BYBIT_XRPJPY", "XRPUSDT").strip().upper(),
        external_signal_min_sources=int(os.getenv("EXTERNAL_SIGNAL_MIN_SOURCES", "2")),
        external_signal_can_change_direction=_parse_bool(os.getenv("EXTERNAL_SIGNAL_CAN_CHANGE_DIRECTION"), True),
        external_signal_can_trigger_risk_off=_parse_bool(os.getenv("EXTERNAL_SIGNAL_CAN_TRIGGER_RISK_OFF"), True),
        db_enabled=_parse_bool(os.getenv("DB_ENABLED"), True),
        db_driver=os.getenv("DB_DRIVER", "postgres").strip().lower(),
        db_host=os.getenv("DB_HOST", "127.0.0.1").strip(),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "botinance").strip(),
        db_user=os.getenv("DB_USER", "botinance").strip(),
        db_password_env=os.getenv("DB_PASSWORD_ENV", "BOTINANCE_DB_PASSWORD").strip(),
        db_write_mode=os.getenv("DB_WRITE_MODE", "dual").strip().lower(),
        db_read_mode=os.getenv("DB_READ_MODE", "prefer_db").strip().lower(),
        db_query_timeout_seconds=int(os.getenv("DB_QUERY_TIMEOUT_SECONDS", "5")),
        db_partition_mode=os.getenv("DB_PARTITION_MODE", "monthly").strip().lower(),
        dashboard_order_source=os.getenv("DASHBOARD_ORDER_SOURCE", "postgres").strip().lower(),
    )
