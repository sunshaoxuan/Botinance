#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

SLEEP_SECONDS="${SLEEP_SECONDS:-300}"
OUTPUT_DIR="${OUTPUT_DIR:-runtime}"

echo "Starting paper monitor"
echo "Workspace: $ROOT_DIR"
echo "Output dir: $OUTPUT_DIR"
echo "Sleep seconds: $SLEEP_SECONDS"
echo "Config source: $ROOT_DIR/.env + $ROOT_DIR/.secrets.enc + macOS Keychain"
echo

PYTHONPATH=src python3 -m binance_ai.main \
  --loop \
  --sleep-seconds "$SLEEP_SECONDS" \
  --output-dir "$OUTPUT_DIR"
