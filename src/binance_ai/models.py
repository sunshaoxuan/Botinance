from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
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
    tick_size: float = 0.0


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: float = 0.0
    time_in_force: str = "GTC"
    client_order_id: str = ""
    trigger: str = ""
    expires_at_ms: int = 0
    tier_index: int = 0
    ladder_group: str = ""
    target_fraction: float = 0.0
    target_spread_pct: float = 0.0
    created_reference_price: float = 0.0
    created_signal_action: str = ""


def make_client_order_id(
    *,
    symbol: str,
    side: str,
    trigger: str = "",
    tier_index: int = 0,
    timestamp_ms: int = 0,
) -> str:
    """Create a compact client order id used by paper mode and Binance clientOrderId."""
    clean_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())[:8] or "SYM"
    clean_side = (side.upper()[:1] or "O")
    clean_trigger = "".join(ch for ch in (trigger or "order").lower() if ch.isalnum())[:6] or "order"
    seed = f"{symbol}|{side}|{trigger}|{tier_index}|{timestamp_ms}"
    digest = hashlib.blake2s(seed.encode("utf-8"), digest_size=4).hexdigest()
    return f"boti_{clean_symbol}_{clean_side}{tier_index}_{clean_trigger}_{digest}"[:36]


@dataclass(frozen=True)
class ManagedOrder:
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float
    limit_price: float
    time_in_force: str
    status: str
    created_at_ms: int
    updated_at_ms: int
    expires_at_ms: int
    trigger: str = ""
    external_order_id: str = ""
    filled_quantity: float = 0.0
    remaining_quantity: float = 0.0
    average_fill_price: float = 0.0
    reserved_quote: float = 0.0
    reserved_base: float = 0.0
    entry_candle_close_time: int = 0
    last_reason: str = ""
    tier_index: int = 0
    ladder_group: str = ""
    target_fraction: float = 0.0
    target_spread_pct: float = 0.0
    created_reference_price: float = 0.0
    created_signal_action: str = ""


@dataclass(frozen=True)
class OrderLifecycleEvent:
    timestamp_ms: int
    symbol: str
    client_order_id: str
    event_type: str
    status: str
    side: str
    quantity: float
    limit_price: float
    fill_price: float = 0.0
    filled_quantity: float = 0.0
    reason: str = ""
    trigger: str = ""
    external_order_id: str = ""
    target_spread_pct: float = 0.0
    current_spread_pct: float = 0.0
    reprice_tolerance_pct: float = 0.0
    open_order_action: str = ""


@dataclass(frozen=True)
class CycleDecision:
    symbol: str
    signal: TradeSignal
    order: OrderRequest | None
    execution_result: Dict[str, object]


@dataclass(frozen=True)
class CompositeDecision:
    symbol: str
    scenario: str
    recommended_action: str
    buy_score: float
    sell_score: float
    hold_score: float
    risk_score: float
    target_position_fraction: float
    recommended_notional: float
    blockers: List[str] = field(default_factory=list)
    explanation_cn: str = ""
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    target_position_summary: Dict[str, object] = field(default_factory=dict)
    entry_protection: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FairValueSummary:
    symbol: str
    method: str
    current_price: float
    fair_value: float
    ema_value: float
    vwap_value: float
    range_midpoint: float
    atr: float
    atr_pct: float
    buy_zone_price: float
    sell_zone_price: float
    buy_discount_pct: float
    sell_premium_pct: float
    volatility_buffer_pct: float
    lookback_bars: int


@dataclass(frozen=True)
class DirectionDecision:
    symbol: str
    mode: str
    recommended_action: str
    price_zone: str
    current_price: float
    fair_value: float
    buy_zone_price: float
    sell_zone_price: float
    expected_net_edge_pct: float
    allow_buy: bool
    allow_sell: bool
    allow_risk_exit: bool
    reason_cn: str
    blockers: List[str] = field(default_factory=list)
    fair_value_summary: Dict[str, object] = field(default_factory=dict)
    paired_order_state: Dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ProtectionLock:
    symbol: str
    lock_type: str
    active: bool
    reason_cn: str
    unlock_conditions: List[str] = field(default_factory=list)
    remaining_bars: int = 0
    reference_price: float = 0.0
    unlock_price: float = 0.0


@dataclass(frozen=True)
class OrderProposal:
    symbol: str
    side: str
    trigger: str
    ladder_group: str
    quantity: float
    notional: float
    urgent: bool = False
    tier_index: int = 0
    target_spread_pct: float = 0.0
    target_fraction: float = 0.0
    score: float = 0.0
    reason_cn: str = ""
    source: str = "policy"
    tiers_raw: str = ""


@dataclass(frozen=True)
class ProposalFilterResult:
    symbol: str
    side: str
    trigger: str
    ladder_group: str
    allowed: bool
    reason: str
    reason_cn: str
    quantity: float = 0.0
    notional: float = 0.0
    net_edge_pct: float = 0.0
    required_edge_pct: float = 0.0


@dataclass(frozen=True)
class InventorySkewSummary:
    symbol: str
    current_fraction: float
    target_fraction: float
    lower_fraction: float
    upper_fraction: float
    skew: float
    buy_weight: float
    sell_weight: float
    reason_cn: str


@dataclass(frozen=True)
class PolicyDecision:
    symbol: str
    policy_state: str
    mode_reason_cn: str
    recommended_action: str
    protection_locks: List[ProtectionLock] = field(default_factory=list)
    order_proposals: List[OrderProposal] = field(default_factory=list)
    proposal_filter_results: List[ProposalFilterResult] = field(default_factory=list)
    inventory_skew_summary: InventorySkewSummary | None = None
    direction_decision: DirectionDecision | None = None
    blockers: List[str] = field(default_factory=list)


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
class SellDecisionDiagnostic:
    symbol: str
    has_position: bool
    quantity: float
    average_entry_price: float
    mark_price: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    strategy_signal: str
    strategy_reason: str
    exit_reason: str
    stop_loss_price: float
    take_profit_price: float
    trailing_stop_price: float
    max_hold_bars: int
    bars_held: int
    activation_trigger: str
    eligible_to_sell: bool
    recommended_sell_quantity: float
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
class DecisionLedgerEntry:
    timestamp_ms: int
    cycle_mode: str
    symbol: str
    price: float
    has_position: bool
    position_quantity: float
    average_entry_price: float
    unrealized_pnl: float
    total_equity: float
    buy_signal: str
    buy_blocker: str
    sell_signal: str
    sell_blocker: str
    ai_allow_entry: bool
    ai_risk_score: float
    final_action: str
    execution_status: str
    execution_reason: str
    news_refresh_status: str
    decision_state: str = ""
    guard_result: str = ""
    net_edge_pct: float = 0.0
    cooldown_remaining_bars: int = 0
    policy_state: str = ""
    policy_reason: str = ""
    direction_mode: str = ""
    price_zone: str = ""
    direction_reason: str = ""


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
    sell_diagnostics: List[SellDecisionDiagnostic]
    position_diagnostics: List[PositionDiagnostic]
    scheduling_diagnostics: List[SchedulingDiagnostic]
    decision_ledger: List[DecisionLedgerEntry]
    composite_decisions: List[CompositeDecision]
    policy_decisions: List[PolicyDecision]
    direction_decisions: List[DirectionDecision]
    order_lifecycle_events: List[OrderLifecycleEvent]
    open_orders: List[ManagedOrder]
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
    activation_state: Dict[str, Dict[str, object]] = field(default_factory=dict)
    open_orders: Dict[str, ManagedOrder] = field(default_factory=dict)
    reserved_quote_balance: float = 0.0
    reserved_base_balances: Dict[str, float] = field(default_factory=dict)
    fills: List[Dict[str, object]] = field(default_factory=list)


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
