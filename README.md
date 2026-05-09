# Binance AI Trader

API-first Binance spot trading skeleton. It fetches market data, evaluates a deterministic strategy, applies risk controls, and can submit spot market orders through Binance REST APIs.

## Current scope

- Spot trading only
- Default limit: up to 3 configured trading pairs
- Historical kline pull via REST
- Deterministic momentum strategy
- Risk-based sizing
- Dry-run by default

## Why this shape

The trading core should stay deterministic. LLM or AI modules can be added later for:

- market regime classification
- parameter proposals
- anomaly explanation
- research summarization

They should not directly bypass risk checks and submit raw orders.

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

Two-layer monitoring defaults:

- fast layer: market scan every `10` seconds
- slow layer: news and announcement refresh every `120` seconds

You can override the slow layer interval in `.env` with `NEWS_REFRESH_SECONDS=60` or another value.

Stop the monitor and dashboard cleanly:

```bash
./stop_visual_dashboard.sh
```

## Pair limit

Initial requirement is enforced by `MAX_ACTIVE_SYMBOLS=3`.

- `3`: up to 3 trading pairs
- `0`: unlimited

## Live order safety

This project assumes:

- trading permission enabled
- withdrawal permission disabled
- API key IP whitelist configured

Do not switch to live trading until:

- dry-run decisions are reviewed
- per-symbol filters are verified
- you have accepted the risk logic and position sizing
