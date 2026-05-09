from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from binance_ai.models import TradeSignal


@dataclass(frozen=True)
class BacktestConfig:
    symbol: str
    quote_asset: str
    date_from: str
    date_to: str
    output_dir: str
    initial_quote_balance: float
    main_interval: str
    entry_interval: str
    trend_interval: str
    fast_window: int
    slow_window: int
    entry_fast_window: int
    entry_slow_window: int
    trend_fast_window: int
    trend_slow_window: int
    risk_per_trade: float
    min_order_notional: float
    stop_loss_pct: float
    take_profit_pct: float
    trailing_stop_pct: float
    max_hold_bars: int
    walk_forward: bool = False
    train_days: int = 90
    test_days: int = 30
    step_days: int = 30


@dataclass(frozen=True)
class BacktestTrade:
    segment_index: int
    symbol: str
    side: str
    entry_time_ms: int
    exit_time_ms: int
    entry_price: float
    exit_price: float
    quantity: float
    notional: float
    realized_pnl: float
    return_pct: float
    hold_bars: int
    hold_hours: float
    mfe_pct: float
    mae_pct: float
    exit_reason: str
    entry_signal_reason: str
    exit_signal_reason: str


@dataclass(frozen=True)
class EquityPoint:
    segment_index: int
    timestamp_ms: int
    close_price: float
    quote_balance: float
    market_value: float
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    net_pnl: float
    drawdown_pct: float = 0.0


@dataclass(frozen=True)
class BacktestSummary:
    symbol: str
    date_from: str
    date_to: str
    total_return_pct: float
    max_drawdown_pct: float
    win_rate: float
    avg_win_loss_ratio: float
    profit_factor: float
    trade_count: int
    completed_trade_count: int
    avg_hold_bars: float
    avg_hold_hours: float
    expectancy_per_trade: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    best_trade_pct: float
    worst_trade_pct: float
    avg_mfe_pct: float
    avg_mae_pct: float
    initial_quote_balance: float
    ending_quote_balance: float
    ending_total_equity: float
    position_open: bool
    open_position_quantity: float


@dataclass(frozen=True)
class BacktestDatasetInfo:
    symbol: str
    interval: str
    candle_count: int
    cache_key: str
    cache_hit: bool
    path: str


@dataclass(frozen=True)
class BacktestRunResult:
    summary: BacktestSummary
    trades: List[BacktestTrade]
    equity_curve: List[EquityPoint]
    dataset_infos: List[BacktestDatasetInfo]
    config: BacktestConfig


@dataclass(frozen=True)
class WalkForwardSegmentResult:
    segment_index: int
    train_from: str
    train_to: str
    test_from: str
    test_to: str
    summary: BacktestSummary
    baseline_expectancy_per_trade: float
    beats_baseline: bool


@dataclass(frozen=True)
class OpenBacktestPosition:
    symbol: str
    entry_time_ms: int
    entry_bar_index: int
    entry_price: float
    quantity: float
    entry_signal: TradeSignal
    entry_signal_reason: str
    max_close_since_entry: float
    min_close_since_entry: float


@dataclass(frozen=True)
class BacktestDataset:
    candles_by_interval: Dict[str, List["Candle"]]
    infos: List[BacktestDatasetInfo]


@dataclass(frozen=True)
class BacktestManifest:
    config: BacktestConfig
    dataset_infos: List[BacktestDatasetInfo]
    walk_forward: bool
    segment_count: int
    baseline_summary: Optional[BacktestSummary] = None
    notes: List[str] = field(default_factory=list)
