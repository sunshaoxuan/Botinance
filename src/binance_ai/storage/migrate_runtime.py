from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict

from binance_ai.config import load_settings
from binance_ai.storage.runtime import PostgresRuntimeStore, StorageUnavailable


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import Botinance JSON runtime data into PostgreSQL.")
    parser.add_argument("--runtime-dir", default="runtime_visual")
    parser.add_argument("--database-url", default="")
    parser.add_argument("--from-jsonl", default="cycle_reports.jsonl")
    parser.add_argument("--limit", type=int, default=0)
    return parser.parse_args()


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def main() -> None:
    args = parse_args()
    runtime_dir = Path(args.runtime_dir)
    settings = load_settings()
    started = time.monotonic()
    try:
        store = PostgresRuntimeStore(settings=settings, database_url=args.database_url or None)
    except StorageUnavailable as exc:
        raise SystemExit(f"PostgreSQL unavailable: {exc}") from exc

    imported_cycles = 0
    skipped_cycles = 0
    jsonl_path = runtime_dir / args.from_jsonl
    if jsonl_path.exists():
        with jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if args.limit and imported_cycles >= args.limit:
                    break
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    skipped_cycles += 1
                    continue
                try:
                    store.write_cycle_report(payload)
                    imported_cycles += 1
                except Exception:
                    skipped_cycles += 1

    imported_snapshots = 0
    paper_state = _load_json(runtime_dir / "paper_state.json")
    if paper_state:
        store.write_portfolio_snapshot(paper_state)
        imported_snapshots = 1

    result = {
        "status": "ok",
        "runtime_dir": str(runtime_dir),
        "imported_cycles": imported_cycles,
        "skipped_cycles": skipped_cycles,
        "imported_snapshots": imported_snapshots,
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
