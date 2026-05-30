# Binance AI Trader

API-first Binance spot trading skeleton. It fetches market data, evaluates a deterministic strategy, applies risk controls, and can submit spot market orders through Binance REST APIs.

## Current scope

- Spot trading only
- Default limit: up to 3 configured trading pairs
- Historical kline pull via REST
- Deterministic momentum strategy
- Multi-timeframe resonance: `15m` entry, `1h` decision, `4h` trend filter
- Risk-based sizing
- Dry-run by default

## Why this shape

The trading core should stay deterministic. LLM or AI modules can be added later for:

- market regime classification
- parameter proposals
- anomaly explanation
- research summarization

They should not directly bypass risk checks and submit raw orders.

Current AI integration is limited to:

- Chinese market summary and risk notes
- entry veto or position-size reduction before a buy order is built

It cannot force an entry that the rules engine does not already allow.
If the primary ChatGPT-compatible endpoint is unavailable, Botinance can fall back to an Ollama endpoint configured with `LLM_FALLBACK_*`; the default fallback is `qwen3:14b` at `http://ccnode.briconbric.com:22545`.

## Setup

1. Create a virtual environment.
2. Install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

3. Copy `.env.example` to `.env` and fill only non-sensitive settings.
4. Put sensitive keys in a temporary plaintext `.env`, then migrate them into an encrypted file:

```bash
PYTHONPATH=src python3 -m binance_ai.secrets migrate-dotenv
```

This command:

- rewrites `.env` into a git-safe public config file
- writes encrypted secrets to `.secrets.enc`
- stores the decryption passphrase in macOS Keychain

5. Keep `DRY_RUN=true` until you have reviewed the logs and sizing behavior.

## Run once

```bash
PYTHONPATH=src python3 -m binance_ai.main
```

The runtime loads:

- public settings from `.env`
- sensitive settings from `.secrets.enc`
- the decryption passphrase from macOS Keychain

## Run in a loop

```bash
PYTHONPATH=src python3 -m binance_ai.main --loop --sleep-seconds 300
```

## Offline backtest

Single-run backtest:

```bash
PYTHONPATH=src python3 -m binance_ai.backtest.main \
  --symbol XRPJPY \
  --from 2026-03-01 \
  --to 2026-05-01 \
  --output-dir runtime_backtest
```

Walk-forward backtest:

```bash
PYTHONPATH=src python3 -m binance_ai.backtest.main \
  --symbol XRPJPY \
  --from 2025-12-01 \
  --to 2026-05-01 \
  --output-dir runtime_backtest_walk \
  --walk-forward \
  --train-days 90 \
  --test-days 30 \
  --step-days 30
```

Backtest output files:

- `summary.json`
- `trades.csv`
- `equity_curve.csv`
- `segments.json`
- `run_manifest.json`

Backtest defaults:

- single symbol only
- main interval close price as fill price
- no fee or slippage model
- no news or LLM gating in the official evaluation path

## Direct scripts

Continuous paper monitoring:

```bash
chmod +x run_paper_monitor.sh show_paper_status.sh
./run_paper_monitor.sh
```

Change the monitoring interval:

```bash
SLEEP_SECONDS=60 ./run_paper_monitor.sh
```

Inspect the latest simulated result and paper portfolio:

```bash
./show_paper_status.sh
```

Visual dashboard with auto-refresh:

```bash
chmod +x run_visual_dashboard.sh stop_visual_dashboard.sh boti_status.sh boti_health.sh
./run_visual_dashboard.sh
```

The script starts both Botinance processes through the cross-platform service manager:

- `monitor`: continuous paper/live-decision loop
- `dashboard`: local web dashboard

Default URL:

```text
http://127.0.0.1:8765
```

Check status and health:

```bash
./boti_status.sh
./boti_health.sh
```

Stop both processes:

```bash
./stop_visual_dashboard.sh
```

Windows PowerShell uses the same Python service manager:

```powershell
.\run_visual_dashboard.ps1
.\boti_status.ps1
.\boti_health.ps1
.\stop_visual_dashboard.ps1
```

Optional PostgreSQL runtime storage:

```bash
export BOTINANCE_DB_PASSWORD='choose-a-local-secret'
docker compose up -d postgres
PYTHONPATH=src python3 -m binance_ai.storage.migrate_runtime \
  --runtime-dir runtime_visual \
  --database-url "postgresql://botinance:${BOTINANCE_DB_PASSWORD}@127.0.0.1:5432/botinance"
```

When `DB_READ_MODE=prefer_db`, the order table and ops summary query PostgreSQL first.
Set `DB_FALLBACK_TO_FILE=true` to allow fallback to JSON files if PostgreSQL is unavailable.
Set `DB_FALLBACK_TO_FILE=false` to make PostgreSQL the hard dependency.
Storage health is available at `/api/ops/storage`.

Make sure the PostgreSQL password is in your environment before start:

```bash
export BOTINANCE_DB_PASSWORD='choose-a-local-secret'
```

Production Windows hosts should sync code through Git instead of file copy:

```powershell
.\Update-Botinance.ps1
```

`Update-Botinance.ps1` fetches `origin/main`, compares it with local `HEAD`, resets to the new commit when needed, and restarts Botinance through `Start-Botinance.ps1`. If there is no code update, it still runs a health check and restarts the local service when the monitor or dashboard is stale.

Linux/macOS can call the manager directly:

```bash
PYTHONPATH=src python3 -m binance_ai.service_manager start
PYTHONPATH=src python3 -m binance_ai.service_manager status
PYTHONPATH=src python3 -m binance_ai.service_manager health
PYTHONPATH=src python3 -m binance_ai.service_manager stop
```

For 24-hour hosting, run the same command under the platform supervisor:

- Windows: Task Scheduler, NSSM, or WinSW; configure auto-start on boot and restart on failure.
- Linux: `systemd`; configure `Restart=always`.
- macOS: `launchd`; configure `KeepAlive=true`.

Health is considered bad if the dashboard is unreachable, the monitor process is missing, or `latest_report.json` is older than `BOTI_STALE_SECONDS` seconds. The default stale threshold is `180`.

The dashboard is a five-tab Botinance interface:

- `实时交易`: main-interval candlesticks, volume bars, paper fills, exit lines, AI veto markers, live position state, scenario state
- `AI 决策`: GPT-5.5 assessment, rule signal, AI verdict, risk-gate explanation, evidence sources
- `回测分析`: `runtime_backtest_walk` first, then fallback to `runtime_backtest_check`
- `风险控制`: buy-decision chain, minimum notional checks, budget, rounded quantity, exit-risk lines, current blockers
- `系统日志`: refresh/decision cycle state, news refresh state, runtime cycle summaries, data-source health

Real-time view overlays:

- `BUY / SELL` markers only for actual `PAPER_FILLED` events
- `止损 / 止盈 / 跟踪止损` lines when a position exists
- `AI 否决` markers when the strategy wanted `BUY` but the AI risk gate blocked entry

Backtest view consumes P6 output files directly:

- `summary.json`
- `segments.json`
- `equity_curve.csv`
- `trades.csv`
- `run_manifest.json`

Two-layer monitoring defaults:

- fast layer: market scan every `3` seconds
- slow layer: news and announcement refresh every `120` seconds
- decision layer: only executes trading decisions on a new closed candle or a configured price-threshold event

You can override the slow layer interval in `.env` with `NEWS_REFRESH_SECONDS=60` or another value.
You can override the decision threshold with `DECISION_PRICE_MOVE_THRESHOLD_PCT=0.005`.
Paper trading and backtest fills include quote-asset fees through `TRADING_FEE_RATE`; the default is `0.001` (`0.1%`) per fill.

Seed the paper portfolio from the current Binance account without enabling live trading:

```bash
PYTHONPATH=src python3 -m binance_ai.tools.sync_paper_from_account --output-dir runtime_visual
```

This stops the current visual monitor if `runtime_visual/monitor.pid` is active, archives and removes old simulated runtime files from the target runtime directory, then writes a fresh `paper_state.json` using real account balances and current market prices as the paper cost basis.

## Multi-timeframe strategy

`P5` upgrades the old single `1h` crossover into a resonance model:

- `15m`: entry momentum confirmation
- `1h`: primary buy or sell trigger
- `4h`: trend direction filter

Default parameters:

```env
KLINE_INTERVAL=1h
FAST_WINDOW=20
SLOW_WINDOW=50
MTF_ENTRY_INTERVAL=15m
MTF_ENTRY_FAST_WINDOW=12
MTF_ENTRY_SLOW_WINDOW=26
MTF_TREND_INTERVAL=4h
MTF_TREND_FAST_WINDOW=20
MTF_TREND_SLOW_WINDOW=50
```

The dashboard and runtime report now expose:

- per-symbol market structure (`uptrend`, `downtrend`, etc.)
- `15m / 1h / 4h` interval summaries
- the full MTF signal reason used by the strategy

Dashboard API additions:

- `live_main_interval_bars`
- `live_trade_markers`
- `live_ai_veto_markers`
- `sell_diagnostics`
- `decision_ledger`
- `position_activation_state`
- `position_activation_markers`
- `backtest_summary`
- `backtest_segments`
- `backtest_equity_curve`
- `backtest_trades`
- `backtest_manifest`
- `scenario_decision`
- `scenario_decisions`
- `policy_state_summary`
- `order_proposal_summary`

## Position activation

`P8` adds a paper-only active grid layer for held positions. It does not replace stop loss, take profit, trailing stop, or strategy `SELL`; it runs after those checks.

Default paper settings:

```env
POSITION_ACTIVATION_ENABLED=true
POSITION_ACTIVATION_MODE=active_grid
GRID_SELL_STEP_PCT=0.003
GRID_BUYBACK_STEP_PCT=0.0025
GRID_SELL_FRACTION=0.25
GRID_MIN_CORE_POSITION_FRACTION=0.25
GRID_MAX_DAILY_TRADES=8
GRID_ALLOW_LOSS_RECOVERY_SELL=true
```

Every cycle now records:

- `sell_diagnostics`: whether Boti should sell, why it should not sell, or how much it would sell
- `decision_ledger`: per-cycle decision snapshots for historical review
- `activation_state`: pending buyback quantity, last grid sell price, daily grid count, and last activation reason

## Ultra-short paper profile

For short-horizon paper testing, keep `DRY_RUN=true` and use a 1m main cycle with a 5m trend filter. The visual runner can still refresh every 3 seconds, but decisions are gated by the 1m closed candle, price-move threshold, or active exit/order events. This keeps REST usage well below Binance Spot API rate-limit headers while allowing quick simulated limit-order lifecycle testing.

Current XRPJPY paper-test profile:

```env
KLINE_INTERVAL=1m
KLINE_LIMIT=180
FAST_WINDOW=3
SLOW_WINDOW=9
MTF_ENTRY_INTERVAL=1m
MTF_ENTRY_FAST_WINDOW=2
MTF_ENTRY_SLOW_WINDOW=5
MTF_TREND_INTERVAL=5m
MTF_TREND_FAST_WINDOW=6
MTF_TREND_SLOW_WINDOW=18
DECISION_PRICE_MOVE_THRESHOLD_PCT=0.0015
STOP_LOSS_PCT=0.003
TAKE_PROFIT_PCT=0.004
TRAILING_STOP_PCT=0.0025
GRID_SELL_STEP_PCT=0.0015
GRID_BUYBACK_STEP_PCT=0.0012
GRID_SELL_FRACTION=0.15
GRID_MAX_DAILY_TRADES=30
ORDER_STALE_SECONDS=45
ORDER_STALE_ACTION=observe
ORDER_REPRICE_ENABLED=true
ORDER_REPRICE_DEVIATION_PCT=0.0015
ORDER_CANCEL_DEVIATION_PCT=0.0015
```

`ORDER_STALE_SECONDS` 是 Boti 对托管限价单的陈旧检查时间。Binance `GTC` 限价单不会自然过期；订单陈旧后默认只记录 `order_stale_observed` 并继续等待触价，只有风险变差、信号反转或满足重定价条件时才撤单。

## Scenario adaptive strategy

`P20` adds a scenario layer before policy proposals. Boti now classifies the current market into one primary scenario, then routes order proposals through the same pair-market-making, inventory-skew, protection, and limit-order lifecycle.

Supported scenarios:

- `RANGE_MARKET_MAKING`: five-level bid and ask pair proposals with inventory skew.
- `UPTREND_PROBE_ENTRY`: small confirmation buy when MA6 expands above MA18 across at least two short intervals.
- `UPTREND_PULLBACK_ENTRY`: controlled pullback buy anchored near MA18, VWAP, or fair value.
- `UPTREND_HOLD_EXPANSION`: hold existing inventory during confirmed expansion and avoid early take profit.
- `UPTREND_EXHAUSTION_TAKE_PROFIT`: pause chase buys and allow partial reduction when MA18 catches MA6 and MA6 flattens.
- `DOWNTREND_DEFENSIVE`: only deep-discount, small defensive buy proposals.
- `PANIC_RISK_REDUCTION`: risk exits and protection locks only.
- `RECOVERY_AFTER_DROP`: limited recovery entry after MA6 recrosses MA18 with volume recovery.
- `LOW_VOL_OBSERVE`: no new orders, keep existing GTC orders managed.

Default scenario settings:

```env
SCENARIO_ENGINE_ENABLED=true
TREND_PROBE_ENTRY_FRACTION=0.25
RECOVERY_ENTRY_FRACTION=0.20
UPTREND_EXPANSION_MIN_PERIODS=2
UPTREND_EXHAUSTION_GAP_PCT=0.0015
DOWNTREND_BUY_DISCOUNT_MULTIPLIER=1.8
LOW_VOL_ATR_PCT=0.0008
ORDER_TIER_MERGE_ENABLED=true
ORDER_TIER_MERGE_MIN_NOTIONAL=5000
```

Runtime reports and `/api/dashboard` expose `scenario_decision` and `scenario_decisions`. The realtime dashboard includes a `场景判断` card with the main scenario, MA expansion count, ATR, volume ratio, allowed actions, blocked actions, and Chinese explanation.

The active order path is:

```text
ScenarioEngine -> ExternalMarketSignalEngine -> PolicyEngine -> InventorySkewOrderProposalEngine -> OrderProposalFilter -> OrderExecutor
```

## External market signal voting

`P21` adds a read-only external consensus layer between the scenario layer and policy proposals. The first version reads public futures data from Binance Futures, OKX, and Bybit, then maps `XRP/JPY` to high-liquidity XRP perpetual markets.

Default mapping:

- Binance Futures: `XRPUSDT`
- OKX: `XRP-USDT-SWAP`
- Bybit: `XRPUSDT`

The external layer votes on `BULLISH`, `BEARISH`, `NEUTRAL`, or `RISK_OFF`. Local P20 scenario weight remains `60%`; external consensus uses `40%`. Missing sources are marked stale and the available sources are reweighted. External consensus can adjust scenario size and risk posture, but it cannot bypass protection locks, net-edge filters, or GTC limit-order lifecycle.

Default external signal settings:

```env
EXTERNAL_SIGNAL_ENABLED=true
EXTERNAL_SIGNAL_REFRESH_SECONDS=60
EXTERNAL_SIGNAL_STALE_SECONDS=180
EXTERNAL_SIGNAL_LOCAL_WEIGHT=0.60
EXTERNAL_SIGNAL_EXTERNAL_WEIGHT=0.40
EXTERNAL_SIGNAL_SOURCES=binance_futures,okx,bybit
EXTERNAL_SYMBOL_BINANCE_FUTURES_XRPJPY=XRPUSDT
EXTERNAL_SYMBOL_OKX_XRPJPY=XRP-USDT-SWAP
EXTERNAL_SYMBOL_BYBIT_XRPJPY=XRPUSDT
EXTERNAL_SIGNAL_MIN_SOURCES=2
EXTERNAL_SIGNAL_CAN_CHANGE_DIRECTION=true
EXTERNAL_SIGNAL_CAN_TRIGGER_RISK_OFF=true
```

Runtime reports and `/api/dashboard` expose `external_signal_snapshots`, `external_signal_votes`, `external_consensus`, `external_signal_health`, and `blended_scenario_decisions`. The realtime dashboard includes an `外部共识` card.

When `POLICY_ENGINE_ENABLED=true` and `LEGACY_DIRECT_ORDER_FALLBACK=false`, old direct entry and rebalance branches remain diagnostic only and cannot bypass policy proposals.

Stop the monitor and dashboard cleanly:

```bash
./stop_visual_dashboard.sh
```

## Pair limit

Initial requirement is enforced by `MAX_ACTIVE_SYMBOLS=3`.

- `3`: up to 3 trading pairs
- `0`: unlimited

## Decision scheduling

The runtime persists decision cadence state in `decision_state.json` under each output directory.

- `DECISION`: at least one symbol entered a real decision pass
- `REFRESH`: price and dashboard updated, but no symbol had a new closed candle or threshold event
- `MIXED`: some symbols entered decision, others remained refresh-only

## Live order safety

This project assumes:

- trading permission enabled
- withdrawal permission disabled
- API key IP whitelist configured

Do not switch to live trading until:

- dry-run decisions are reviewed
- per-symbol filters are verified
- you have accepted the risk logic and position sizing
