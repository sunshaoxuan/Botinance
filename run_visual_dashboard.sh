#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

OUTPUT_DIR="${OUTPUT_DIR:-runtime_visual}"
SLEEP_SECONDS="${SLEEP_SECONDS:-3}"
DASHBOARD_HOST="${DASHBOARD_HOST:-127.0.0.1}"
DASHBOARD_PORT="${DASHBOARD_PORT:-8765}"

mkdir -p "$OUTPUT_DIR"

if [[ -f "$OUTPUT_DIR/monitor.pid" ]]; then
  OLD_MONITOR_PID="$(cat "$OUTPUT_DIR/monitor.pid" 2>/dev/null || true)"
  if [[ -n "${OLD_MONITOR_PID:-}" ]] && kill -0 "$OLD_MONITOR_PID" >/dev/null 2>&1; then
    echo "Stopping previous monitor process: $OLD_MONITOR_PID"
    kill "$OLD_MONITOR_PID" >/dev/null 2>&1 || true
    sleep 1
  fi
fi

RESOLVED_PORT="$(python3 - <<'PY' "$DASHBOARD_HOST" "$DASHBOARD_PORT"
import socket
import sys

host = sys.argv[1]
start_port = int(sys.argv[2])

for port in range(start_port, start_port + 50):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            continue
        print(port)
        break
else:
    raise SystemExit(f"No available port found in range {start_port}-{start_port + 49}")
PY
)"

echo "Starting local paper monitor in background"
echo "Output dir: $OUTPUT_DIR"
echo "Monitor interval: ${SLEEP_SECONDS}s"
echo "Config source: $ROOT_DIR/.env + $ROOT_DIR/.secrets.enc + macOS Keychain"
echo "Dashboard: http://${DASHBOARD_HOST}:${RESOLVED_PORT}"
echo

PYTHONPATH=src python3 -m binance_ai.main \
  --loop \
  --sleep-seconds "$SLEEP_SECONDS" \
  --output-dir "$OUTPUT_DIR" \
  > "$OUTPUT_DIR/monitor.log" 2>&1 &

MONITOR_PID=$!
echo "$MONITOR_PID" > "$OUTPUT_DIR/monitor.pid"

cleanup() {
  if kill -0 "$MONITOR_PID" >/dev/null 2>&1; then
    kill "$MONITOR_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

PYTHONPATH=src python3 -m binance_ai.dashboard_server \
  --host "$DASHBOARD_HOST" \
  --port "$RESOLVED_PORT" \
  --output-dir "$OUTPUT_DIR"
