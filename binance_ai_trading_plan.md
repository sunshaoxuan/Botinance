# Binance AI Trading Tool Plan

Date: 2026-05-09

## 1. Feasibility

Yes. Binance can be integrated programmatically through official APIs instead of automating the web trading UI.

Global Binance:
- Spot order placement: `POST /api/v3/order`
- Spot market data stream: `wss://stream.binance.com:9443`
- Spot testnet: `https://testnet.binance.vision/api`
- USD-M futures order placement: `POST /fapi/v1/order`
- COIN-M futures order placement: `POST /dapi/v1/order`

Binance.US:
- REST base endpoint: `https://api.binance.us`
- WebSocket API base endpoint: `wss://ws-api.binance.us:443/ws-api/v3`
- WebSocket Streams base endpoint: `wss://stream.binance.us:9443`

## 2. Practical Boundary

What you can access:
- Order placement, cancel, query
- Account balances and fills
- Real-time trades, order book, klines, ticker
- User data stream for order/account events

What this is not:
- Not direct control of the Binance web page "trading hall"
- Not a guaranteed-profit system

## 3. Compliance and Security Baseline

Must enforce:
- Use the exchange that matches the account jurisdiction
- Enable API trading permission only
- Disable withdrawal permission
- Prefer IP whitelist
- Keep all order execution deterministic and auditable

Notes:
- Binance API Key Product Terms effective 2026-01-05 place responsibility for API activity and losses on the user.
- Binance.US availability depends on state/region support.
- Binance Ai Pro exists officially, but for a custom tool you should build on the standard APIs first.

## 4. Recommended Product Shape

Do not let an LLM generate raw orders directly.

Use a 4-layer design:

1. Data layer
- REST pull for historical klines, trades, exchange info
- WebSocket for live trades, best bid/ask, depth, user data
- Local time-series store for replay and backtest

2. Strategy layer
- Deterministic alpha models first
- Example signals: momentum, mean reversion, breakout, funding/basis spread, microstructure imbalance
- LLM used for research summarization, regime classification, parameter suggestion, anomaly explanation

3. Risk layer
- Symbol whitelist
- Max position per symbol
- Max daily loss
- Max leverage
- Max slippage
- Kill switch
- Cooldown after loss streak

4. Execution layer
- Order sizing
- Pre-trade checks against balance, filters, tick size, lot size
- Smart choice between market, limit, post-only, reduce-only
- Retry/reconcile logic for timeout and disconnect cases

## 5. Best Initial Scope

Phase 1:
- Binance Spot only
- 1 to 3 symbols only
- Paper trading first
- No leverage
- No autonomous parameter changes

Phase 2:
- Real small-size live trading
- Daily risk cap
- Manual approval mode for strategy changes

Phase 3:
- Add futures only after spot live metrics are stable
- Add portfolio-level risk controls

## 6. Suggested System Modules

- `connectors/binance_spot.py`
- `connectors/binance_us.py`
- `marketdata/ws_client.py`
- `marketdata/historical_loader.py`
- `strategy/base.py`
- `strategy/momentum.py`
- `strategy/regime_filter.py`
- `risk/engine.py`
- `execution/router.py`
- `execution/reconciler.py`
- `portfolio/account_state.py`
- `backtest/engine.py`
- `paper/simulator.py`
- `llm/research_agent.py`
- `llm/strategy_assistant.py`
- `api/server.py`
- `ui/dashboard.py`

## 7. Recommended Tech Stack

- Python
- `httpx` or `aiohttp`
- `websockets`
- `pydantic`
- `pandas` + `numpy`
- `duckdb` or `postgres`
- `redis` for ephemeral state
- `FastAPI`
- `ccxt` only if you want multi-exchange abstraction later; use native Binance API first

## 8. Metrics That Matter

- Net PnL
- Sharpe / Sortino
- Max drawdown
- Win rate
- Profit factor
- Slippage
- Order rejection rate
- WebSocket reconnect frequency
- Time from signal to fill

## 9. First Build Order

1. Connectivity test
2. Historical data loader
3. Paper trading engine
4. One simple deterministic strategy
5. Backtest and walk-forward validation
6. Risk engine
7. Small-size live execution
8. LLM assistant for research only
9. LLM-assisted parameter workflow with human approval

## 10. Main Design Principle

The AI should be an assistant around a deterministic trading core, not the sole actor that directly controls money movement.
