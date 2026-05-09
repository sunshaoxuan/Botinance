from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int


@dataclass(frozen=True)
class TradeSignal:
    symbol: str
    action: SignalAction
    confidence: float
    reason: str
    regime: str = ""


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    step_size: float
    min_qty: float
    min_notional: float


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    order_type: str
    quantity: float


@dataclass(frozen=True)
class CycleDecision:
    symbol: str
    signal: TradeSignal
    order: OrderRequest | None
    execution_result: Dict[str, object]


@dataclass(frozen=True)
class BuyDecisionDiagnostic:
    symbol: str
    signal_action: str
    signal_reason: str
    has_position: bool
    quote_balance: float
    quote_budget: float
    effective_notional: float
    min_notional_required: float
    price: float
    raw_quantity: float
    adjusted_quantity: float
    final_notional: float
    min_notional_passed: bool
    min_qty: float
    eligible_signal: bool
    eligible_risk: bool
    ai_allow_entry: bool
    ai_risk_score: float
    ai_position_multiplier: float
    ai_veto_reason: str
    eligible_to_buy: bool
    blocker: str
    blocker_details: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class PositionDiagnostic:
    symbol: str
    quantity: float
    average_entry_price: float
    mark_price: float
    highest_price: float
    unrealized_pnl: float
    stop_loss_price: float
    take_profit_price: float
    trailing_stop_price: float
    bars_held: int
    opened_at_ms: int
    entry_candle_close_time: int
    exit_watch_reason: str


@dataclass(frozen=True)
class AiRiskAssessment:
    symbol: str
    status: str
    allow_entry: bool
    risk_score: float
    position_multiplier: float
    veto_reason: str
    raw_payload: str = ""


@dataclass(frozen=True)
class SchedulingDiagnostic:
    symbol: str
    should_run_decision: bool
    decision_reason: str
    latest_closed_candle_close_time: int
    last_decision_candle_close_time: int
    current_price: float
    last_decision_price: float
    price_move_pct: float
    new_candle_available: bool
    threshold_triggered: bool
    exit_triggered: bool
    has_position: bool


@dataclass(frozen=True)
class AccountSnapshot:
    balances: Dict[str, float]

    def balance_of(self, asset: str) -> float:
        return self.balances.get(asset, 0.0)


@dataclass(frozen=True)
class CycleReport:
    timestamp_ms: int
    decisions: List[CycleDecision]
    buy_diagnostics: List[BuyDecisionDiagnostic]
    position_diagnostics: List[PositionDiagnostic]
    scheduling_diagnostics: List[SchedulingDiagnostic]
    ai_risk_assessments: List[AiRiskAssessment]
    market_prices: Dict[str, float]
    market_snapshots: List[Dict[str, object]]
    news_evidence: List["NewsItem"]
    news_refresh_status: str
    news_last_updated_ms: int
    news_next_refresh_ms: int
    cycle_mode: str
    cycle_reason: str
    quote_asset_balance: float
    simulation_mode: bool
    total_equity: float
    realized_pnl: float
    unrealized_pnl: float
    net_pnl: float
    llm_analysis: Optional["LlmAnalysis"] = None


@dataclass(frozen=True)
class LlmAnalysis:
    status: str
    provider: str
    model: str
    regime_cn: str
    summary_cn: str
    action_bias_cn: str
    confidence: float
    risk_note_cn: str
    raw_text: str = ""
    error: str = ""


@dataclass(frozen=True)
class PositionSnapshot:
    quantity: float
    average_entry_price: float
    opened_at_ms: int = 0
    entry_candle_close_time: int = 0
    highest_price: float = 0.0


@dataclass(frozen=True)
class PortfolioSnapshot:
    quote_asset: str
    quote_balance: float
    initial_quote_balance: float
    positions: Dict[str, PositionSnapshot] = field(default_factory=dict)
    realized_pnl: float = 0.0


@dataclass(frozen=True)
class NewsItem:
    source: str
    title: str
    url: str
    published_at_ms: int
    category: str
    matched_keywords: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class NewsCollectionResult:
    items: List[NewsItem]
    refresh_status: str
    last_updated_ms: int
    next_refresh_ms: int
