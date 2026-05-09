from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import time
from dataclasses import asdict
from pathlib import Path
from typing import Dict, Iterable, List

from binance_ai.config import load_settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import PortfolioSnapshot, PositionSnapshot


SIMULATED_RUNTIME_FILES = (
    "cycle_reports.jsonl",
    "latest_report.json",
    "decision_state.json",
    "paper_state.json",
    "heartbeat_cycle.log",
    "monitor.log",
)


def base_asset_for_symbol(symbol: str, quote_asset: str) -> str:
    symbol = symbol.upper()
    quote_asset = quote_asset.upper()
    if symbol.endswith(quote_asset):
        return symbol[: -len(quote_asset)]
    return symbol


def build_paper_snapshot_from_balances(
    *,
    balances: Dict[str, float],
    symbols: Iterable[str],
    quote_asset: str,
    prices: Dict[str, float],
    timestamp_ms: int,
) -> PortfolioSnapshot:
    positions: Dict[str, PositionSnapshot] = {}
    quote_balance = float(balances.get(quote_asset, 0.0))

    for symbol in symbols:
        normalized_symbol = symbol.upper()
        base_asset = base_asset_for_symbol(normalized_symbol, quote_asset)
        quantity = float(balances.get(base_asset, 0.0))
        price = float(prices.get(normalized_symbol, 0.0))
        if quantity <= 0 or price <= 0:
            continue
        positions[normalized_symbol] = PositionSnapshot(
            quantity=quantity,
            average_entry_price=price,
            opened_at_ms=timestamp_ms,
            entry_candle_close_time=timestamp_ms,
            highest_price=price,
        )

    initial_quote_balance = quote_balance + sum(
        position.quantity * position.average_entry_price
        for position in positions.values()
    )
    return PortfolioSnapshot(
        quote_asset=quote_asset,
        quote_balance=quote_balance,
        initial_quote_balance=initial_quote_balance,
        positions=positions,
        realized_pnl=0.0,
        activation_state={},
    )


def stop_monitor_if_running(output_dir: Path) -> int | None:
    pid_path = output_dir / "monitor.pid"
    if not pid_path.exists():
        return None
    raw_pid = pid_path.read_text(encoding="utf-8").strip()
    if not raw_pid:
        pid_path.unlink(missing_ok=True)
        return None
    pid = int(raw_pid)
    try:
        os.kill(pid, 0)
    except OSError:
        pid_path.unlink(missing_ok=True)
        return None
    os.kill(pid, signal.SIGTERM)
    time.sleep(0.5)
    pid_path.unlink(missing_ok=True)
    return pid


def clear_simulated_runtime(output_dir: Path, archive_root: Path | None) -> List[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    archived_or_removed: List[str] = []
    archive_dir: Path | None = None
    if archive_root is not None:
        archive_dir = archive_root / time.strftime("%Y%m%d-%H%M%S") / output_dir.name
        archive_dir.mkdir(parents=True, exist_ok=True)

    for name in SIMULATED_RUNTIME_FILES:
        path = output_dir / name
        if not path.exists():
            continue
        if archive_dir is not None:
            shutil.copy2(path, archive_dir / name)
        path.unlink()
        archived_or_removed.append(name)
    return archived_or_removed


def write_seed_manifest(
    *,
    output_dir: Path,
    snapshot: PortfolioSnapshot,
    balances: Dict[str, float],
    prices: Dict[str, float],
    stopped_monitor_pid: int | None,
    cleared_files: List[str],
) -> None:
    payload = {
        "seeded_at_ms": int(time.time() * 1000),
        "source": "binance_account_balances",
        "mode": "paper_state_seed_from_real_account",
        "quote_asset": snapshot.quote_asset,
        "symbols": sorted(snapshot.positions),
        "balances": balances,
        "prices": prices,
        "stopped_monitor_pid": stopped_monitor_pid,
        "cleared_simulated_files": cleared_files,
        "note": "Paper entry prices are seeded at current market prices; historical real cost basis is not inferred.",
    }
    (output_dir / "account_seed_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed paper state from the current Binance account.")
    parser.add_argument("--output-dir", default="runtime_visual", help="Runtime directory to reset and seed.")
    parser.add_argument(
        "--archive-root",
        default="runtime_resets",
        help="Directory used to archive old simulated runtime files before removal. Use empty string to disable.",
    )
    parser.add_argument("--no-stop-monitor", action="store_true", help="Do not stop an existing monitor.pid process.")
    parser.add_argument("--symbols", default="", help="Comma-separated symbols. Defaults to TRADING_SYMBOLS.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    archive_root = Path(args.archive_root) if args.archive_root else None

    settings = load_settings()
    symbols = [
        item.strip().upper()
        for item in (args.symbols or ",".join(settings.trading_symbols)).split(",")
        if item.strip()
    ]
    if not settings.dry_run:
        raise RuntimeError("Refusing to seed paper state while DRY_RUN=false.")
    if not symbols:
        raise RuntimeError("No trading symbols configured.")

    stopped_monitor_pid = None
    if not args.no_stop_monitor:
        stopped_monitor_pid = stop_monitor_if_running(output_dir)

    client = BinanceSpotClient(settings)
    try:
        balances = client.get_account_balances(include_locked=True)
        prices = {symbol: client.get_symbol_price(symbol) for symbol in symbols}
    finally:
        client.close()

    timestamp_ms = int(time.time() * 1000)
    snapshot = build_paper_snapshot_from_balances(
        balances=balances,
        symbols=symbols,
        quote_asset=settings.quote_asset,
        prices=prices,
        timestamp_ms=timestamp_ms,
    )
    cleared_files = clear_simulated_runtime(output_dir, archive_root)
    (output_dir / "paper_state.json").write_text(
        json.dumps(asdict(snapshot), ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    write_seed_manifest(
        output_dir=output_dir,
        snapshot=snapshot,
        balances=balances,
        prices=prices,
        stopped_monitor_pid=stopped_monitor_pid,
        cleared_files=cleared_files,
    )

    summary = {
        "output_dir": str(output_dir),
        "stopped_monitor_pid": stopped_monitor_pid,
        "cleared_files": cleared_files,
        "quote_asset": snapshot.quote_asset,
        "quote_balance": snapshot.quote_balance,
        "initial_quote_balance": snapshot.initial_quote_balance,
        "positions": {
            symbol: {
                "quantity": position.quantity,
                "average_entry_price": position.average_entry_price,
            }
            for symbol, position in snapshot.positions.items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
