#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

OUTPUT_DIR="${OUTPUT_DIR:-runtime_visual}"

if [[ -f "$OUTPUT_DIR/monitor.pid" ]]; then
  MONITOR_PID="$(cat "$OUTPUT_DIR/monitor.pid" 2>/dev/null || true)"
  if [[ -n "${MONITOR_PID:-}" ]] && kill -0 "$MONITOR_PID" >/dev/null 2>&1; then
    echo "Stopping monitor process: $MONITOR_PID"
    kill "$MONITOR_PID" >/dev/null 2>&1 || true
  fi
  rm -f "$OUTPUT_DIR/monitor.pid"
fi

DASHBOARD_PIDS="$(pgrep -f "binance_ai.dashboard_server .*--output-dir ${OUTPUT_DIR}" || true)"
if [[ -n "${DASHBOARD_PIDS:-}" ]]; then
  for pid in $DASHBOARD_PIDS; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      echo "Stopping dashboard process: $pid"
      kill "$pid" >/dev/null 2>&1 || true
    fi
  done
fi
