# Phase 17: Policy Engine

## Summary
P17 stops adding isolated fixes around `emergency_stop`, `buyback`, and `target_rebuild`. Boti moves to a mature auto-trading structure:

1. Trading mode detection
2. Protection layer
3. Inventory-skew order proposals
4. Order lifecycle execution

The implementation keeps simulation as the default. Live execution remains guarded by `DRY_RUN=false` and `LIVE_ORDER_EXECUTION_ENABLED=true`.

References:

- Hummingbot Pure Market Making: https://hummingbot.org/strategies/v1-strategies/pure-market-making/
- Hummingbot Order Refresh Tolerance: https://hummingbot.org/strategies/v1-strategies/strategy-configs/order-refresh-tolerance/
- Hummingbot Hanging Orders: https://hummingbot.org/strategy-configs/hanging-orders/
- Freqtrade Protections: https://www.freqtrade.io/en/stable/plugins/

## Current Problems
The current engine can place and manage GTC limit orders, but the decision layer is still too fragmented.

- Low sell, high buy: risk exits can sell into weakness, while later target rebuild can buy back above the risk-exit price.
- Risk exit and rebuild are disconnected: `emergency_stop` writes state, but later buy logic can still be evaluated by separate branches.
- SELL orders can hang without a clear policy explanation: the order lifecycle knows why it waits, but the decision layer does not present a market-making policy.
- Composite scoring and order lifecycle are disconnected: scores change signals, then old direct branches still decide order generation.
- Rules compete for priority: stop-loss, take-profit, target inventory, activation grid, and strategy signals can each create orders independently.

## New Architecture
### PolicyEngine
The main decision entrypoint. It receives strategy signal, risk exit reason, composite score, target inventory, open orders, account state, activation state, and AI risk. It outputs one `PolicyDecision`.

The engine does not submit orders. It only returns:

- `policy_state`
- `protection_locks`
- `order_proposals`
- `proposal_filter_results`
- `inventory_skew_summary`
- Chinese explanations for dashboard and replay

### ProtectionManager
The protection layer. It decides whether a pair is locked, whether active trading is allowed, and why.

Protections:

- `PairLockAfterRiskExit`: after `stop_loss` or `emergency_stop`, normal buy/rebuild is locked until cooldown, trend stability, and net-edge conditions pass.
- `StoplossGuard`: repeated risk exits within a lookback window lock the pair.
- `DrawdownGuard`: daily realized drawdown beyond budget blocks active trading.
- `CooldownPeriod`: entry protection and buyback cooldown are treated as policy locks instead of scattered branch checks.

### InventorySkewOrderProposalEngine
Generates buy/sell proposals from target inventory and account state.

Principles:

- Inventory is managed around a target fraction.
- High inventory lowers buy priority and increases sell priority.
- Low inventory increases buy priority and reduces sell priority.
- Strategy and composite signals are inputs, not direct order commands.

### OrderProposalFilter
All proposals pass the same filters before entering the existing limit-order lifecycle.

Filters:

- Protection locks
- AI extreme risk
- Minimum effective notional
- Fee and expected net edge
- Daily risk budget
- Existing order spread tolerance
- Exchange quantity and notional limits

### ExecutionAdapter
TradingEngine converts accepted proposals to existing `OrderRequest` and submits them through `_submit_ladder_orders()`. P14/P15 GTC semantics are preserved:

- Time only marks stale.
- Orders are not canceled by time alone.
- Reprice only happens when spread structure fails.
- Existing `client_order_id`, `ladder_group`, `tier_index`, fee, and PnL accounting remain authoritative.

## State Machine
### MARKET_MAKING
Normal balanced two-sided policy. Orders are proposed around inventory target and tier spreads.

### INVENTORY_REBALANCE
Current inventory is outside the target band. The engine favors proposals that move the account back toward target.

### RISK_REDUCTION
Hard risk is active. The engine only allows protective sell proposals and cancels incompatible active orders.

### PAIR_LOCKED_AFTER_STOP
A recent stop-loss or emergency stop has locked the pair. Normal buy/rebuild proposals are filtered until unlock conditions pass.

### RECOVERY_ENTRY
The pair was locked after a risk exit and now satisfies cooldown, trend stability, and net-edge conditions. Controlled re-entry is allowed.

### OBSERVE_ONLY
Trading is blocked by drawdown guard, repeated stop-loss guard, AI extreme risk, or missing market/account data.

## Protection Rules
### Pair Lock After Risk Exit
Inputs:

- `activation_state.last_risk_exit_price`
- `activation_state.risk_exit_reentry_price`
- `activation_state.last_risk_exit_timestamp_ms`
- recent candle trend and volatility

Unlock conditions:

- Lock candles elapsed.
- Price is not above the fee-adjusted re-entry line when `PAIR_LOCK_REQUIRE_NET_EDGE=true`.
- Trend is no longer deteriorating when `PAIR_LOCK_REQUIRE_TREND_STABLE=true`.

### Stoploss Guard
Repeated stop-loss or emergency exits inside `STOPLOSS_GUARD_LOOKBACK_CANDLES` produce an observe-only lock.

### Drawdown Guard
If Boti daily realized PnL is below `MAX_DRAWDOWN_GUARD_PCT`, active trading stops. Risk exits remain allowed.

## Order Strategy
The policy layer never directly places orders.

1. Build raw proposals from risk, inventory skew, and composite score.
2. Filter proposals.
3. Submit only accepted proposals to the current GTC limit-order lifecycle.
4. Record rejected proposals for dashboard and replay.

Proposal types:

- `risk_exit`: protective sell
- `inventory_entry`: target inventory buy
- `inventory_exit`: target inventory sell
- `recovery_entry`: controlled buy after pair lock unlocks
- `market_making_buy`: passive buy proposal
- `market_making_sell`: passive sell proposal

## Runtime Fields
`CycleReport` adds:

- `policy_decisions`

Each `PolicyDecision` contains:

- `policy_state`
- `protection_locks`
- `order_proposals`
- `proposal_filter_results`
- `inventory_skew_summary`

Dashboard API derives:

- `policy_state_summary`
- `protection_summary`
- `order_proposal_summary`

## Config
```env
POLICY_ENGINE_ENABLED=true
PAIR_LOCK_AFTER_RISK_EXIT_CANDLES=12
PAIR_LOCK_REQUIRE_TREND_STABLE=true
PAIR_LOCK_REQUIRE_NET_EDGE=true
STOPLOSS_GUARD_LOOKBACK_CANDLES=48
STOPLOSS_GUARD_TRADE_LIMIT=2
STOPLOSS_GUARD_LOCK_CANDLES=24
MAX_DRAWDOWN_GUARD_PCT=0.015
INVENTORY_SKEW_ENABLED=true
INVENTORY_TARGET_BASE_PCT=0.55
INVENTORY_RANGE_MULTIPLIER=1.5
ORDER_PROPOSAL_MIN_NET_EDGE_PCT=0.0025
```

## Acceptance Criteria
- Risk exit enters `PAIR_LOCKED_AFTER_STOP`.
- Normal buy/rebuild is blocked while pair lock conditions fail.
- Recovery entry is allowed only after cooldown, trend stability, and net-edge conditions pass.
- Inventory skew reduces buy proposals in high-inventory states and reduces sell proposals in low-inventory states.
- Rejected proposals record a Chinese reason.
- Existing GTC order semantics remain unchanged.
- `PYTHONPATH=src python3 -m unittest discover -s tests` passes.
- A 24-hour remote replay can group every cycle by policy mode, proposal, rejection, fill, fee, and Boti PnL.
