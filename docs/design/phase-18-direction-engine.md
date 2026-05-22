# P18 Direction Engine

## Problem

The previous decision path still allowed trend to become direction:

- `TargetInventoryEngine` increased target XRP allocation in `strong_up` and reduced it in `weak_down`.
- `CompositeDecisionEngine` added positive momentum to buy score and negative momentum to sell score.
- `TradingEngine` still allowed legacy direct order branches after the policy layer.

That combination can produce buy-high / sell-low behavior. P18 makes direction explicit and blocks legacy order submission when the direction engine is enabled.

## Model

`DirectionDecisionEngine` decides whether price is in a tradable zone before any non-risk order can be submitted.

- `FairValueEngine` blends EMA, VWAP, and range midpoint.
- `BUY_ZONE` is below fair value by configured discount plus ATR buffer.
- `SELL_ZONE` is above fair value by configured premium plus ATR buffer.
- Non-risk BUY is allowed only in `BUY_ZONE` with positive expected net edge.
- Non-risk SELL is allowed only in `SELL_ZONE` with positive expected net edge.
- Risk exits can sell below the sell zone, but must be marked as `RISK_EXIT`.

This follows the same separation used by mature bot systems: strategy mode, inventory skew, order proposal, proposal filtering, then GTC order lifecycle.

## Execution Rules

- With `DIRECTION_ENGINE_ENABLED=true` and `LEGACY_DIRECT_ORDER_FALLBACK=false`, old direct branches (`strategy_buy`, `strategy_sell`, `target_rebuild_buy`, `target_rebalance_sell`) cannot submit orders.
- Old strategy, target inventory, composite score, and AI risk are inputs only.
- Policy proposal filtering checks the direction decision before accepting non-risk proposals.
- Every decision report records `direction_decision`, `fair_value_summary`, `price_zone`, `expected_net_edge_pct`, and `paired_order_state`.

## Defaults

```env
DIRECTION_ENGINE_ENABLED=true
LEGACY_DIRECT_ORDER_FALLBACK=false
TREND_FOLLOW_ENABLED=false
FAIR_VALUE_METHOD=ema_vwap_blend
FAIR_VALUE_LOOKBACK_BARS=60
VOLATILITY_BUFFER_ATR_MULTIPLIER=0.35
BUY_ZONE_MIN_DISCOUNT_PCT=0.0025
SELL_ZONE_MIN_PREMIUM_PCT=0.0025
MIN_PAIR_NET_EDGE_PCT=0.0035
ALLOW_RISK_SELL_BELOW_SELL_ZONE=true
```

Trend following is disabled by default. If enabled later, it must require multi-timeframe confirmation and positive after-fee edge.

## Dashboard

The trading tab displays direction details:

- fair value
- buy zone
- sell zone
- current price zone
- expected net edge
- Chinese reason for action or rejection

The decision ledger also records direction mode, price zone, and direction reason.
