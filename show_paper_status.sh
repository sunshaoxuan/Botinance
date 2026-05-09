#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

OUTPUT_DIR="${OUTPUT_DIR:-runtime}"
LATEST_REPORT="$OUTPUT_DIR/latest_report.json"
PAPER_STATE="$OUTPUT_DIR/paper_state.json"

if [[ ! -f "$LATEST_REPORT" ]]; then
  echo "No report found at $LATEST_REPORT"
  exit 1
fi

python3 - "$LATEST_REPORT" "$PAPER_STATE" <<'PY'
import json
import sys
from pathlib import Path

latest_report = Path(sys.argv[1])
paper_state = Path(sys.argv[2])

report = json.loads(latest_report.read_text(encoding="utf-8"))
state = json.loads(paper_state.read_text(encoding="utf-8")) if paper_state.exists() else {}

print("Latest cycle")
print(json.dumps(report, ensure_ascii=True, indent=2))
print()
print("Paper state")
print(json.dumps(state, ensure_ascii=True, indent=2))
PY

