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
chmod +x run_visual_dashboard.sh stop_visual_dashboard.sh
./run_visual_dashboard.sh
```

The script prints the final dashboard URL. It prefers `8765` and automatically switches to the next free port if that port is already occupied.

Typical URL:

```text
http://127.0.0.1:8765
```

This script starts:

- a background paper monitor loop writing to `runtime_visual/`
- a local dashboard server reading from the same directory

The dashboard is a five-tab local trading workstation:

- `实时交易`: main-interval candlesticks, volume bars, paper fills, exit lines, AI veto markers, live position state
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

- fast layer: market scan every `10` seconds
- slow layer: news and announcement refresh every `120` seconds
- decision layer: only executes trading decisions on a new closed candle or a configured price-threshold event

You can override the slow layer interval in `.env` with `NEWS_REFRESH_SECONDS=60` or another value.
You can override the decision threshold with `DECISION_PRICE_MOVE_THRESHOLD_PCT=0.005`.

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
- `backtest_summary`
- `backtest_segments`
- `backtest_equity_curve`
- `backtest_trades`
- `backtest_manifest`

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
