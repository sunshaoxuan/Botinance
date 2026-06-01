from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import time
import threading
from dataclasses import asdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List
from urllib.parse import parse_qs, urlparse

from binance_ai.config import load_settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import Candle
from binance_ai.storage.runtime import build_runtime_store
from binance_ai.tools.sync_paper_from_account import (
    base_asset_for_symbol,
    build_cash_baseline_snapshot_from_balances,
    build_paper_snapshot_from_balances,
    calculate_cash_baseline_equity,
    clear_simulated_runtime,
    infer_remaining_cost_basis_from_trades,
    parse_asset_minimums,
    validate_account_sync_requirements,
    write_seed_manifest,
)


INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "10m": 600_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "7d": 604_800_000,
    "10d": 864_000_000,
    "30d": 2_592_000_000,
    "90d": 7_776_000_000,
    "180d": 15_552_000_000,
    "1y": 31_536_000_000,
}

NATIVE_BINANCE_INTERVALS = {
    "1m",
    "3m",
    "5m",
    "15m",
    "30m",
    "1h",
    "2h",
    "4h",
    "6h",
    "8h",
    "12h",
    "1d",
    "3d",
    "1w",
    "1M",
}

CHART_INTERVAL_OPTIONS: List[Dict[str, str]] = [
    {"value": "1m", "label": "1分", "source": "binance"},
    {"value": "3m", "label": "3分", "source": "binance"},
    {"value": "5m", "label": "5分", "source": "binance"},
    {"value": "10m", "label": "10分", "source": "aggregate:5m"},
    {"value": "30m", "label": "30分", "source": "binance"},
    {"value": "1h", "label": "1小时", "source": "binance"},
    {"value": "4h", "label": "4小时", "source": "binance"},
    {"value": "8h", "label": "8小时", "source": "binance"},
    {"value": "1d", "label": "日线", "source": "binance"},
    {"value": "7d", "label": "7日线", "source": "aggregate:1d"},
    {"value": "10d", "label": "10日线", "source": "aggregate:1d"},
    {"value": "30d", "label": "30日线", "source": "aggregate:1d"},
    {"value": "90d", "label": "90日线", "source": "aggregate:1d"},
    {"value": "180d", "label": "180日线", "source": "aggregate:1d"},
    {"value": "1y", "label": "1年线", "source": "aggregate:1d"},
]

CHART_INTERVAL_VALUES = {item["value"] for item in CHART_INTERVAL_OPTIONS}
ORDER_RECORD_CACHE_TTL_SECONDS = 1.0
ORDER_RECORD_SCAN_CACHE_TTL_SECONDS = 2.0
CYCLE_SCAN_MAX_ON_NONE = 20000
DASHBOARD_CHART_MARKER_DEFAULT_SCAN_LINES = 2400
_build_dashboard_chart_payload_cache_ttl = 2.0

_recent_cycle_cache: Dict[str, Dict[str, Any]] = {}
_recent_cycle_cache_lock = threading.Lock()

_order_records_cache: Dict[str, Dict[str, Any]] = {}
_order_records_cache_lock = threading.Lock()


def _get_runtime_cache_key(path: Path, scan_lines: int | None) -> str:
    return f"{path}:{scan_lines or 0}"


def _cycle_cache_entry(path: Path, scan_lines: int | None) -> Dict[str, Any] | None:
    key = _get_runtime_cache_key(path, scan_lines)
    now = time.time()
    with _recent_cycle_cache_lock:
        entry = _recent_cycle_cache.get(key)
        if entry is None:
            return None
        if now - float(entry.get("cached_at", 0.0)) > float(entry.get("ttl", ORDER_RECORD_SCAN_CACHE_TTL_SECONDS)):
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        if entry.get("mtime_ns") != stat.st_mtime_ns or entry.get("size") != stat.st_size:
            return None
        return entry


def _record_cycle_scan_cache(path: Path, scan_lines: int | None, cycles: List[Dict[str, Any]], ttl: float) -> None:
    if not path.exists():
        return
    try:
        stat = path.stat()
    except OSError:
        return
    key = _get_runtime_cache_key(path, scan_lines)
    with _recent_cycle_cache_lock:
        _recent_cycle_cache[key] = {
            "cached_at": time.time(),
            "ttl": ttl,
            "mtime_ns": stat.st_mtime_ns,
            "size": stat.st_size,
            "scan_lines": scan_lines,
            "cycles": cycles,
        }


def _load_recent_cycles_cached(path: Path, scan_lines: int | None, *, ttl: float = ORDER_RECORD_SCAN_CACHE_TTL_SECONDS) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    normalized_scan_lines = scan_lines
    if normalized_scan_lines is None:
        normalized_scan_lines = CYCLE_SCAN_MAX_ON_NONE
    if normalized_scan_lines <= 0:
        return []
    cache_key = _get_runtime_cache_key(path, normalized_scan_lines)
    entry = _cycle_cache_entry(path, normalized_scan_lines)
    if entry is not None:
        cycles = entry.get("cycles")
        if isinstance(cycles, list):
            return cycles

    if scan_lines is None:
        scan_lines = normalized_scan_lines
    lines = _read_recent_lines(path, scan_lines)
    cycles: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            cycle = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(cycle, dict):
            cycles.append(cycle)
    _record_cycle_scan_cache(path, normalized_scan_lines, cycles, ttl)
    return cycles


INDEX_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Botinance</title>
  <style>
    :root {
      --bg: #f7f9fc;
      --bg-grid: rgba(36, 53, 76, 0.035);
      --surface: #ffffff;
      --surface-soft: #f6f8fb;
      --surface-glass: rgba(255, 255, 255, 0.88);
      --line: #d7e0ea;
      --line-strong: #b7c4d2;
      --ink: #111b2b;
      --ink-soft: #334155;
      --muted: #66758a;
      --muted-2: #9aa8b8;
      --blue: #1f3f6d;
      --blue-2: #426b9f;
      --blue-soft: rgba(31, 63, 109, 0.08);
      --green: #15803d;
      --green-soft: rgba(21, 128, 61, 0.10);
      --red: #b4232a;
      --red-soft: rgba(180, 35, 42, 0.08);
      --coral: #c96a21;
      --coral-soft: rgba(201, 106, 33, 0.09);
      --shadow: 0 12px 28px rgba(17, 27, 43, 0.045);
      --shadow-soft: 0 4px 14px rgba(17, 27, 43, 0.035);
      --radius: 7px;
      --radius-sm: 5px;
      --font: "Hiragino Sans", "Hiragino Kaku Gothic ProN", "Yu Gothic", "Avenir Next", "PingFang SC", sans-serif;
      --mono: "SFMono-Regular", "Menlo", "Consolas", monospace;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: var(--font);
      background:
        linear-gradient(120deg, rgba(31, 63, 109, 0.035), transparent 34%),
        radial-gradient(circle at 92% 8%, rgba(66, 107, 159, 0.09), transparent 28%),
        linear-gradient(var(--bg-grid) 1px, transparent 1px),
        linear-gradient(90deg, var(--bg-grid) 1px, transparent 1px),
        var(--bg);
      background-size: auto, auto, 28px 28px, 28px 28px, auto;
      overflow: hidden;
    }

    button {
      font: inherit;
      color: inherit;
    }

    .app-shell {
      display: block;
      height: 100vh;
    }

    .workspace {
      min-width: 0;
      height: 100vh;
      overflow: auto;
      padding: 14px 16px 20px;
    }

    .top-bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      min-height: 58px;
      padding: 9px 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface-glass);
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow-soft);
      position: sticky;
      top: 14px;
      z-index: 3;
    }

    .top-title {
      display: flex;
      align-items: center;
      gap: 12px;
      min-width: 260px;
    }

    .top-kicker {
      font-size: 10px;
      color: var(--muted);
      letter-spacing: 0.16em;
      text-transform: uppercase;
      font-weight: 720;
    }

    .top-name {
      margin-top: 2px;
      font-size: 19px;
      font-weight: 760;
      letter-spacing: 0.02em;
    }

    .market-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 9px;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--ink-soft);
      font-weight: 740;
      font-family: var(--mono);
      box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.9);
    }

    .market-dot {
      width: 8px;
      height: 8px;
      border-radius: 99px;
      background: var(--green);
      box-shadow: 0 0 0 4px var(--green-soft);
    }

    .top-metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(104px, 1fr));
      gap: 7px;
      flex: 1;
      max-width: 860px;
    }

    .top-metric {
      padding: 6px 9px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: rgba(255, 255, 255, 0.72);
    }

    .top-metric span {
      display: block;
      font-size: 10px;
      color: var(--muted);
      font-weight: 680;
    }

    .top-metric strong {
      display: block;
      margin-top: 2px;
      font-size: 13px;
      font-weight: 760;
      font-family: var(--mono);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .tab-bar {
      margin: 10px 0 14px;
      display: flex;
      gap: 4px;
      padding: 0 8px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(255, 255, 255, 0.72);
      backdrop-filter: blur(10px);
      box-shadow: none;
      overflow-x: auto;
    }

    .tab-button {
      border: 0;
      border-bottom: 2px solid transparent;
      border-radius: 0;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      padding: 11px 16px 10px;
      font-size: 13px;
      font-weight: 720;
      white-space: nowrap;
      transition: all 150ms ease;
    }

    .tab-button:hover {
      color: var(--blue);
      background: transparent;
    }

    .tab-button.active {
      color: var(--blue);
      background: linear-gradient(180deg, transparent, rgba(31, 63, 109, 0.045));
      border-bottom-color: var(--blue);
      box-shadow: none;
    }

    .tab-panel { display: none; }
    .tab-panel.active { display: block; }

    .trade-workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 320px;
      gap: 12px;
      align-items: start;
    }

    .trade-main-column {
      display: grid;
      gap: 12px;
      min-width: 0;
    }

    .right-command-stack,
    .stack {
      display: grid;
      gap: 10px;
    }

    .panel,
    .card {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: rgba(255, 255, 255, 0.92);
      box-shadow: var(--shadow-soft);
    }

    .panel-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      padding: 11px 13px 9px;
      border-bottom: 1px solid var(--line);
    }

    .panel-title {
      font-size: 14px;
      font-weight: 760;
      letter-spacing: 0.02em;
    }

    .panel-subtitle {
      margin-top: 3px;
      font-size: 11px;
      color: var(--muted);
    }

    .panel-body {
      padding: 12px;
    }

    .chart-card {
      min-height: 560px;
      overflow: hidden;
    }

    .chart-frame {
      position: relative;
      height: 506px;
      padding: 10px;
      background: #fff;
    }

    .profit-strip {
      height: 112px;
      border-top: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(248, 251, 255, 0.72), #fff);
    }

    .profit-strip-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      height: 28px;
      padding: 8px 12px 0;
      color: var(--muted);
      font-size: 11px;
      font-weight: 620;
    }

    .profit-strip-header strong {
      color: var(--ink);
      font-family: var(--mono);
      font-size: 11px;
      font-weight: 560;
    }

    .profit-strip canvas {
      display: block;
      width: 100%;
      height: 84px;
    }

    .chart-loading {
      position: absolute;
      inset: 10px;
      z-index: 2;
      display: none;
      align-items: center;
      justify-content: center;
      gap: 10px;
      border-radius: var(--radius-sm);
      background: rgba(255, 255, 255, 0.72);
      color: var(--ink-soft);
      font-size: 12px;
      font-weight: 720;
      backdrop-filter: blur(2px);
      pointer-events: none;
    }

    .chart-loading.active {
      display: flex;
    }

    .chart-spinner {
      width: 14px;
      height: 14px;
      border: 2px solid rgba(31, 63, 109, 0.16);
      border-top-color: var(--blue);
      border-radius: 999px;
      animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    canvas {
      display: block;
      width: 100%;
      height: 100%;
      border-radius: var(--radius-sm);
    }

    .tool-row {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
    }

    .tool-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--surface-soft);
      color: var(--ink-soft);
      padding: 4px 8px;
      font-size: 11px;
      font-weight: 720;
      font-family: var(--mono);
      white-space: nowrap;
    }

    .tool-pill.blue { color: var(--blue); background: var(--blue-soft); border-color: rgba(37, 99, 235, 0.18); }
    .tool-pill.green { color: var(--green); background: var(--green-soft); border-color: rgba(22, 163, 74, 0.16); }
    .tool-pill.red { color: var(--red); background: var(--red-soft); border-color: rgba(220, 38, 38, 0.16); }
    .tool-pill.coral { color: var(--coral); background: var(--coral-soft); border-color: rgba(249, 115, 22, 0.18); }

    .card {
      padding: 12px 13px;
      min-width: 0;
    }

    .card-label {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
      font-weight: 720;
    }

    .card-value {
      margin-top: 7px;
      font-size: 22px;
      line-height: 1.1;
      font-weight: 780;
      letter-spacing: 0.01em;
      font-family: var(--mono);
    }

    .card-note {
      margin-top: 8px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }

    .card-note.prose {
      max-width: 100%;
    }

    .card-note.detail {
      padding: 8px 9px;
      border: 1px solid rgba(133, 155, 184, 0.24);
      border-radius: 8px;
      background: rgba(248, 251, 255, 0.72);
      color: var(--ink-soft);
      overflow-wrap: anywhere;
    }

    .mini-kv {
      display: grid;
      gap: 5px;
      margin-top: 9px;
    }

    .mini-kv-row {
      display: grid;
      grid-template-columns: minmax(86px, 0.9fr) minmax(0, 1fr);
      gap: 8px;
      align-items: baseline;
      min-width: 0;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.35;
    }

    .mini-kv-row span {
      color: var(--muted);
      font-weight: 520;
      white-space: normal;
    }

    .mini-kv-row strong {
      min-width: 0;
      color: var(--ink-soft);
      font-weight: 540;
      text-align: right;
      overflow-wrap: anywhere;
    }

    .metric-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .metric-tile {
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-soft);
      padding: 12px;
      min-width: 0;
    }

    .metric-tile span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      font-weight: 680;
    }

    .metric-tile strong {
      display: block;
      margin-top: 7px;
      font-size: 20px;
      font-weight: 760;
      font-family: var(--mono);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .trade-fill-panel { min-width: 0; }

    .trade-fill-panel .panel-body {
      padding: 10px 12px 12px;
    }

    .trade-fill-panel .scroll-table {
      max-height: none;
      overflow: auto;
    }

    .fill-toolbar {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 11px;
      font-family: var(--mono);
    }

    .fill-toolbar select {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      padding: 4px 8px;
      font-size: 11px;
      font-weight: 720;
      outline: none;
    }

    .pager-button {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink-soft);
      padding: 4px 9px;
      font-size: 11px;
      cursor: pointer;
    }

    .pager-button:disabled {
      opacity: 0.38;
      cursor: default;
    }

    .utility-button {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink-soft);
      padding: 5px 10px;
      font-size: 11px;
      font-weight: 740;
      cursor: pointer;
      transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
    }

    .utility-button:hover {
      border-color: rgba(31, 63, 109, 0.38);
      color: var(--navy);
      background: var(--blue-soft);
    }

    .chart-select {
      border: 1px solid var(--line);
      border-radius: 999px;
      background: #fff;
      color: var(--ink);
      padding: 5px 26px 5px 10px;
      font-size: 11px;
      font-weight: 760;
      font-family: var(--mono);
      outline: none;
      cursor: pointer;
    }

    .drawer-backdrop {
      position: fixed;
      inset: 0;
      z-index: 40;
      background: rgba(15, 23, 42, 0.18);
      opacity: 0;
      pointer-events: none;
      transition: opacity 0.18s ease;
    }

    .drawer-backdrop.active {
      opacity: 1;
      pointer-events: auto;
    }

    .insight-drawer {
      position: fixed;
      z-index: 41;
      top: 0;
      right: 0;
      width: min(520px, 92vw);
      height: 100vh;
      border-left: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.98);
      box-shadow: -18px 0 40px rgba(31, 63, 109, 0.14);
      transform: translateX(102%);
      transition: transform 0.22s ease;
      display: flex;
      flex-direction: column;
    }

    .insight-drawer.active {
      transform: translateX(0);
    }

    .drawer-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 14px;
      padding: 18px 18px 14px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, #fff 0%, #f8fbff 100%);
    }

    .drawer-title {
      font-size: 15px;
      font-weight: 780;
      letter-spacing: 0.02em;
    }

    .drawer-subtitle {
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }

    .drawer-close {
      border: 1px solid var(--line);
      border-radius: 999px;
      width: 30px;
      height: 30px;
      background: #fff;
      color: var(--muted);
      cursor: pointer;
      font-size: 18px;
      line-height: 1;
    }

    .drawer-body {
      padding: 14px 16px 22px;
      overflow: auto;
      flex: 1;
    }

    .drawer-section + .drawer-section {
      margin-top: 16px;
    }

    .drawer-section-title {
      margin-bottom: 8px;
      color: var(--ink);
      font-size: 12px;
      font-weight: 760;
    }

    .page-grid-2 {
      display: grid;
      grid-template-columns: 1.1fr 0.9fr;
      gap: 14px;
      align-items: start;
    }

    .page-grid-3 {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      align-items: start;
    }

    .backtest-grid {
      display: grid;
      grid-template-columns: 1.2fr 0.8fr;
      gap: 14px;
      align-items: start;
    }

    .mini-chart {
      height: 260px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 11px;
    }

    th,
    td {
      padding: 7px 8px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
    }

    th {
      color: var(--muted);
      font-size: 10px;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      font-weight: 720;
      background: var(--surface-soft);
    }

    tr:last-child td { border-bottom: 0; }

    .table-wrap {
      overflow-x: auto;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: #fff;
    }

    .kv-grid {
      display: grid;
      gap: 6px;
    }

    .kv-row {
      display: grid;
      grid-template-columns: 116px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
      padding: 7px 0;
      border-bottom: 1px solid rgba(215, 224, 234, 0.75);
      font-size: 12px;
    }

    .kv-row:last-child { border-bottom: 0; }
    .kv-row span { color: var(--muted); font-weight: 680; }
    .kv-row strong { color: var(--ink); font-weight: 720; word-break: break-word; }

    .timeline {
      display: grid;
      gap: 8px;
    }

    .timeline-item {
      position: relative;
      padding: 9px 10px 9px 30px;
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      background: var(--surface-soft);
    }

    .timeline-item::before {
      content: "";
      position: absolute;
      left: 12px;
      top: 14px;
      width: 6px;
      height: 6px;
      border-radius: 99px;
      background: var(--blue);
      box-shadow: 0 0 0 4px var(--blue-soft);
    }

    .timeline-time {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10px;
      font-weight: 700;
    }

    .timeline-title {
      margin-top: 4px;
      font-size: 12px;
      font-weight: 740;
    }

    .timeline-body {
      margin-top: 4px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.5;
    }

    .empty {
      border: 1px dashed var(--line-strong);
      border-radius: var(--radius-sm);
      background: var(--surface-soft);
      color: var(--muted);
      padding: 16px;
      text-align: center;
      font-size: 12px;
      font-weight: 700;
    }

    .positive { color: var(--green); }
    .negative { color: var(--red); }
    .neutral { color: var(--muted); }

    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      border-radius: 999px;
      padding: 4px 7px;
      font-size: 11px;
      font-weight: 720;
      border: 1px solid var(--line);
      background: #fff;
    }

    .status-chip::before {
      content: "";
      width: 6px;
      height: 6px;
      border-radius: 99px;
      background: var(--muted-2);
    }

    .status-chip.buy { color: var(--green); background: var(--green-soft); border-color: rgba(22, 163, 74, 0.16); }
    .status-chip.buy::before { background: var(--green); }
    .status-chip.sell { color: var(--red); background: var(--red-soft); border-color: rgba(220, 38, 38, 0.16); }
    .status-chip.sell::before { background: var(--red); }
    .status-chip.wait { color: var(--blue); background: var(--blue-soft); border-color: rgba(37, 99, 235, 0.18); }
    .status-chip.wait::before { background: var(--blue); }
    .status-chip.block { color: var(--coral); background: var(--coral-soft); border-color: rgba(249, 115, 22, 0.18); }
    .status-chip.block::before { background: var(--coral); }

    .scroll-table {
      max-height: 360px;
      overflow: auto;
    }

    .drawer-body .scroll-table {
      max-height: none;
    }

    .drawer-body table {
      min-width: 760px;
    }

    .drawer-body th,
    .drawer-body td {
      white-space: nowrap;
    }

    .drawer-body .muted {
      white-space: normal;
      display: inline-block;
      max-width: 140px;
      line-height: 1.35;
    }

    .drawer-count {
      color: var(--muted);
      font-size: 11px;
      font-weight: 560;
      margin-left: 6px;
    }

    .code {
      font-family: var(--mono);
      font-size: 12px;
      color: var(--ink-soft);
    }

    .nowrap { white-space: nowrap; }

    .muted {
      color: var(--muted);
      font-size: 10px;
    }

    @media (max-width: 1180px) {
      .trade-workspace,
      .backtest-grid,
      .page-grid-2 {
        grid-template-columns: 1fr;
      }

      .right-command-stack {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .page-grid-3 {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 900px) {
      body { overflow: auto; }

      .app-shell { height: auto; }

      .workspace {
        height: auto;
        overflow: visible;
        padding: 12px;
      }

      .top-bar {
        position: static;
        display: grid;
      }

      .top-metrics,
      .metric-grid,
      .right-command-stack {
        grid-template-columns: 1fr;
      }

      .chart-frame {
        height: 430px;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <main class="workspace">
      <header class="top-bar">
        <div class="top-title">
          <div>
            <div class="top-kicker">XRP/JPY PAPER TRADING</div>
            <div class="top-name">Botinance</div>
          </div>
          <div class="market-pill"><span class="market-dot"></span><span id="topSymbol">XRP/JPY</span></div>
        </div>
        <div class="top-metrics">
          <div class="top-metric"><span>运行状态</span><strong id="topMode">读取中</strong></div>
          <div class="top-metric"><span>最新刷新</span><strong id="topUpdated">--</strong></div>
          <div class="top-metric"><span>当前价格</span><strong id="topPrice">--</strong></div>
          <div class="top-metric"><span>可用现金</span><strong id="topCash">--</strong></div>
          <div class="top-metric"><span>交割成本总盈亏</span><strong id="topEquity">--</strong></div>
        </div>
      </header>

      <nav class="tab-bar" aria-label="功能分页">
        <button class="tab-button active" data-tab="trading">实时交易</button>
        <button class="tab-button" data-tab="ai">AI 决策</button>
        <button class="tab-button" data-tab="backtest">回测分析</button>
        <button class="tab-button" data-tab="risk">风险控制</button>
        <button class="tab-button" data-tab="system">系统日志</button>
      </nav>

      <section class="tab-panel active" id="tab-trading">
        <div class="trade-workspace">
          <div class="trade-main-column">
            <article class="panel chart-card">
              <div class="panel-header">
                <div>
                  <div class="panel-title">主周期行情</div>
                  <div class="panel-subtitle" id="chartSubtitle">K 线、成交量、成交点、AI 否决点、退出线</div>
                </div>
                <div class="tool-row">
                  <select class="chart-select" id="chartIntervalSelect" aria-label="K线周期"></select>
                  <span class="tool-pill blue" id="chartInterval">主周期 --</span>
                  <span class="tool-pill green">PAPER</span>
                  <span class="tool-pill coral" id="chartPointCount">0 bars</span>
                  <button class="utility-button" data-drawer="evidence" type="button">证据来源</button>
                  <button class="utility-button" data-drawer="decision" type="button">决策链路</button>
                </div>
              </div>
              <div class="chart-frame">
                <div class="chart-loading" id="chartLoading"><span class="chart-spinner"></span><span>图表后台加载</span></div>
                <canvas id="tradeChart"></canvas>
              </div>
              <div class="profit-strip">
                <div class="profit-strip-header">
                  <span>Boti已实现操作利润线</span>
                  <strong id="profitCurveLabel">--</strong>
                </div>
                <canvas id="profitCurveChart"></canvas>
              </div>
            </article>

            <article class="panel trade-fill-panel">
              <div class="panel-header">
                <div>
                  <div class="panel-title">订单与成交记录</div>
                  <div class="panel-subtitle">挂单、成交、撤单、冻结资产和手续费统一展示</div>
                </div>
                <div class="fill-toolbar">
                  <select id="fillFilter" aria-label="订单成交记录筛选">
                    <option value="all">全部订单</option>
                    <option value="filled">已成交</option>
                    <option value="open">挂单中</option>
                    <option value="closed">撤单/过期</option>
                    <option value="buy">买入</option>
                    <option value="sell">卖出</option>
                  </select>
                  <span id="fillPageInfo">--</span>
                  <select id="fillPageSize" aria-label="成交记录每页行数">
                    <option value="50">50行</option>
                    <option value="100">100行</option>
                  </select>
                  <button class="pager-button" id="fillPrev" type="button">上一页</button>
                  <button class="pager-button" id="fillNext" type="button">下一页</button>
                </div>
              </div>
              <div class="panel-body" id="tradeFillsTable"></div>
            </article>
          </div>

          <aside class="right-command-stack">
            <div class="card" id="positionCard"></div>
            <div class="card" id="pnlCard"></div>
            <div class="card" id="sellDecisionCard"></div>
            <div class="card" id="riskGateCard"></div>
            <div class="card" id="openOrderCard"></div>
            <div class="card" id="scenarioCard"></div>
            <div class="card" id="externalSignalCard"></div>
            <div class="card" id="entryPlanCard"></div>
            <div class="card" id="executionCard"></div>
          </aside>
        </div>
      </section>

      <section class="tab-panel" id="tab-ai">
        <div class="page-grid-2">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">大模型市场判断</div><div class="panel-subtitle">分析结论、市场状态与风险提示</div></div></div>
            <div class="panel-body" id="aiSummaryCard"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">规则信号 vs AI 裁决</div><div class="panel-subtitle">AI 只负责否决或降风险，不创建新买点</div></div></div>
            <div class="panel-body" id="ruleVsAiCard"></div>
          </article>
        </div>
        <div class="page-grid-2" style="margin-top:14px;">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">完整证据列表</div><div class="panel-subtitle">新闻、市场数据与链路来源</div></div></div>
            <div class="panel-body" id="evidenceFull"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">AI 风险闸门</div><div class="panel-subtitle">入场许可、缩仓建议与风险解释</div></div></div>
            <div class="panel-body" id="aiRiskFull"></div>
          </article>
        </div>
      </section>

      <section class="tab-panel" id="tab-backtest">
        <div class="metric-grid">
          <div class="metric-tile"><span>总收益率</span><strong id="btTotalReturn">--</strong></div>
          <div class="metric-tile"><span>最大回撤</span><strong id="btMaxDrawdown">--</strong></div>
          <div class="metric-tile"><span>胜率</span><strong id="btWinRate">--</strong></div>
          <div class="metric-tile"><span>Profit Factor</span><strong id="btProfitFactor">--</strong></div>
          <div class="metric-tile"><span>单笔期望</span><strong id="btExpectancy">--</strong></div>
          <div class="metric-tile"><span>交易数</span><strong id="btTradeCount">--</strong></div>
        </div>

        <div class="backtest-grid" style="margin-top:14px;">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">Walk-forward 权益曲线</div><div class="panel-subtitle" id="btSourceLabel">等待回测文件</div></div></div>
            <div class="panel-body">
              <div class="mini-chart"><canvas id="btEquityChart"></canvas></div>
            </div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">回撤曲线</div><div class="panel-subtitle">按 P6 标准结果文件读取</div></div></div>
            <div class="panel-body">
              <div class="mini-chart"><canvas id="btDrawdownChart"></canvas></div>
            </div>
          </article>
        </div>

        <div class="page-grid-2" style="margin-top:14px;">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">Segment 对比</div><div class="panel-subtitle">固定滚动窗稳定性检验</div></div></div>
            <div class="panel-body" id="btSegments"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">交易明细</div><div class="panel-subtitle">开平仓、收益、MFE / MAE</div></div></div>
            <div class="panel-body" id="btTrades"></div>
          </article>
        </div>
        <article class="panel" style="margin-top:14px;">
          <div class="panel-header"><div><div class="panel-title">Run Manifest</div><div class="panel-subtitle">配置快照与数据缓存状态</div></div></div>
          <div class="panel-body" id="btManifest"></div>
        </article>
      </section>

      <section class="tab-panel" id="tab-risk">
        <div class="page-grid-3">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">买入决策链路</div><div class="panel-subtitle">预算、最小成交额、数量取整</div></div></div>
            <div class="panel-body" id="buyDecisionFull"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">建仓 / 补仓计划</div><div class="panel-subtitle">目标仓位、现金保留和预估买入梯队</div></div></div>
            <div class="panel-body" id="entryPlanFull"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">退出风险线</div><div class="panel-subtitle">止损、止盈、跟踪止损、超时退出</div></div></div>
            <div class="panel-body" id="exitRiskCard"></div>
          </article>
        </div>
        <div class="page-grid-3" style="margin-top:14px;">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">仓位激活</div><div class="panel-subtitle">主动网格、待回补和当日次数</div></div></div>
            <div class="panel-body" id="riskParametersCard"></div>
          </article>
        </div>
      </section>

      <section class="tab-panel" id="tab-system">
        <div class="page-grid-3">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">刷新轮 / 决策轮</div><div class="panel-subtitle">runtime 周期状态</div></div></div>
            <div class="panel-body" id="systemStateCard"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">调度状态</div><div class="panel-subtitle">新闻刷新与下一轮执行</div></div></div>
            <div class="panel-body" id="schedulingFull"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">数据源状态</div><div class="panel-subtitle">行情、回测、成交、payload 健康度</div></div></div>
            <div class="panel-body" id="payloadHealthCard"></div>
          </article>
        </div>
        <article class="panel" style="margin-top:14px;">
          <div class="panel-header"><div><div class="panel-title">最近 Runtime 周期</div><div class="panel-subtitle">cycle_reports.jsonl 摘要</div></div></div>
          <div class="panel-body" id="cycleLedger"></div>
        </article>
      </section>
    </main>
  </div>

  <div class="drawer-backdrop" id="insightDrawerBackdrop"></div>
  <aside class="insight-drawer" id="insightDrawer" aria-hidden="true">
    <div class="drawer-header">
      <div>
        <div class="drawer-title" id="insightDrawerTitle">详情</div>
        <div class="drawer-subtitle" id="insightDrawerSubtitle">按需查看，不占用主交易画面</div>
      </div>
      <button class="drawer-close" id="insightDrawerClose" type="button" aria-label="关闭">×</button>
    </div>
    <div class="drawer-body" id="insightDrawerBody"></div>
  </aside>

  <script>
    const refreshMs = 2000;
    let activeTab = "trading";
    let lastPayloadSnapshot = null;
    let chartHover = { active: false, x: 0, y: 0 };
    let activeDrawerKind = null;
    let selectedChartInterval = window.localStorage.getItem("boti.chartInterval") || "1m";
    let fillPageSize = 50;
    let fillPage = 0;
    let fillFilter = window.localStorage.getItem("boti.fillFilter") || "all";
    let dashboardRequestSeq = 0;
    let chartRenderSeq = 0;
    let drawerRequestSeq = 0;
    let orderRequestSeq = 0;
    let lastOrderRecordsLoadedAt = 0;
    let tickInFlight = false;
    let orderRecordsInFlight = false;
    let chartRenderedInterval = "";
    let chartLastPayloadMs = 0;
    const chartBarsCache = {};
    const SNAPSHOT_CACHE_KEY = "boti.lastDashboardSnapshot.v2";

    const els = {};
    const ids = [
      "topSymbol", "topMode", "topUpdated", "topPrice", "topCash", "topEquity", "chartSubtitle", "chartInterval", "chartPointCount", "chartLoading",
      "profitCurveLabel",
      "chartIntervalSelect", "fillFilter", "fillPageInfo", "fillPageSize", "fillPrev", "fillNext", "positionCard", "pnlCard", "sellDecisionCard", "riskGateCard", "openOrderCard", "scenarioCard", "externalSignalCard", "entryPlanCard", "executionCard", "tradeFillsTable",
      "aiSummaryCard", "ruleVsAiCard", "evidenceFull", "aiRiskFull", "btTotalReturn", "btMaxDrawdown", "btWinRate",
      "btProfitFactor", "btExpectancy", "btTradeCount", "btSourceLabel", "btSegments", "btTrades", "btManifest",
      "buyDecisionFull", "entryPlanFull", "exitRiskCard", "riskParametersCard", "systemStateCard", "schedulingFull", "payloadHealthCard",
      "cycleLedger", "insightDrawerBackdrop", "insightDrawer", "insightDrawerTitle", "insightDrawerSubtitle", "insightDrawerBody", "insightDrawerClose"
    ];

    function cacheEls() {
      ids.forEach((id) => { els[id] = document.getElementById(id); });
    }

    function compactSnapshotForCache(payload) {
      if (!payload) return null;
      const copy = { ...payload };
      copy.history = [];
      copy.live_chart_bars = (payload.live_chart_bars || []).slice(-160);
      copy.live_main_interval_bars = (payload.live_main_interval_bars || []).slice(-160);
      copy.live_refresh_bars = (payload.live_refresh_bars || []).slice(-160);
      copy.live_profit_curve = (payload.live_profit_curve || []).slice(-600);
      copy.trade_records = (payload.trade_records || []).slice(0, 200);
      copy.trade_records_complete = payload.trade_records_complete === true;
      return copy;
    }

    function saveSnapshotCache(payload) {
      try {
        const compact = compactSnapshotForCache(payload);
        if (compact) window.localStorage.setItem(SNAPSHOT_CACHE_KEY, JSON.stringify(compact));
      } catch (_) {
        // Cache is a best-effort first-paint optimization.
      }
    }

    function loadSnapshotCache() {
      try {
        const raw = window.localStorage.getItem(SNAPSHOT_CACHE_KEY);
        return raw ? JSON.parse(raw) : null;
      } catch (_) {
        return null;
      }
    }

    function asNumber(value, fallback = 0) {
      const n = Number(value);
      return Number.isFinite(n) ? n : fallback;
    }

    function fmtNumber(value, digits = 4) {
      const n = Number(value);
      if (!Number.isFinite(n)) return "--";
      return n.toLocaleString("zh-CN", { maximumFractionDigits: digits });
    }

    function fmtCurrency(value, asset = "") {
      const n = Number(value);
      if (!Number.isFinite(n)) return "--";
      return `${n.toLocaleString("zh-CN", { maximumFractionDigits: 4 })}${asset ? " " + asset : ""}`;
    }

    function fmtSymbol(symbol, quoteAsset = "") {
      const raw = String(symbol || "").trim().toUpperCase();
      const quote = String(quoteAsset || "").trim().toUpperCase();
      if (!raw) return "--";
      if (raw.includes("/")) return raw;
      if (quote && raw.endsWith(quote) && raw.length > quote.length) {
        return `${raw.slice(0, -quote.length)}/${quote}`;
      }
      for (const candidate of ["USDT", "USDC", "BUSD", "JPY", "USD", "BTC", "ETH", "BNB"]) {
        if (raw.endsWith(candidate) && raw.length > candidate.length) {
          return `${raw.slice(0, -candidate.length)}/${candidate}`;
        }
      }
      return raw;
    }

    function fmtPercent(value, digits = 2) {
      const n = Number(value);
      if (!Number.isFinite(n)) return "--";
      return `${n.toFixed(digits)}%`;
    }

    function fmtTime(value) {
      if (!value) return "--";
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return String(value);
      return d.toLocaleString("zh-CN", { hour12: false });
    }

    function fmtShortTime(value) {
      if (!value) return "--";
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return String(value).slice(0, 16);
      return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
    }

    function fmtChartAxisTime(value, interval) {
      if (!value) return "--";
      const d = new Date(value);
      if (Number.isNaN(d.getTime())) return String(value).slice(0, 16);
      if (["1d", "7d", "10d", "30d", "90d", "180d", "1y"].includes(interval)) {
        return d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
      }
      if (["4h", "8h"].includes(interval)) {
        return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", hour12: false });
      }
      return fmtShortTime(value);
    }

    function chartMaConfig(payload) {
      const cfg = payload.runtime_config || {};
      const interval = payload.live_chart_interval || selectedChartInterval || "1m";
      if (interval === cfg.mtf_trend_interval) {
        return {
          label: `${interval} 趋势`,
          fastWindow: Number(cfg.mtf_trend_fast_window || 0),
          slowWindow: Number(cfg.mtf_trend_slow_window || 0),
        };
      }
      if (interval === cfg.mtf_entry_interval) {
        return {
          label: `${interval} 入场`,
          fastWindow: Number(cfg.mtf_entry_fast_window || 0),
          slowWindow: Number(cfg.mtf_entry_slow_window || 0),
        };
      }
      return {
        label: `${interval} 主策略`,
        fastWindow: Number(cfg.fast_window || 0),
        slowWindow: Number(cfg.slow_window || 0),
      };
    }

    function pnlClass(value) {
      const n = Number(value);
      if (!Number.isFinite(n) || n === 0) return "neutral";
      return n > 0 ? "positive" : "negative";
    }

    function signalClass(signal) {
      const s = String(signal || "").toUpperCase();
      if (s === "BUY") return "buy";
      if (s === "SELL") return "sell";
      if (s === "BLOCKED" || s === "REJECTED") return "block";
      return "wait";
    }

    function signalLabel(signal) {
      const s = String(signal || "").toUpperCase();
      if (s === "BUY") return "买入";
      if (s === "SELL") return "卖出";
      if (s === "HOLD") return "观望";
      if (s === "PAPER_FILLED") return "模拟已成交";
      if (s === "ORDER_OPEN") return "挂单中";
      if (s === "ORDER_LADDER_OPEN") return "挂单组";
      if (s === "OPEN") return "挂单中";
      if (s === "FILLED") return "已成交";
      if (s === "CANCELED") return "已撤单";
      if (s === "SUBMITTED") return "已挂单";
      if (s === "EXPIRED") return "交易所过期";
      if (s === "REJECTED") return "已拒绝";
      if (s === "UNKNOWN") return "状态待确认";
      if (s === "BLOCKED") return "已阻塞";
      if (s === "REFRESH_ONLY") return "仅刷新";
      if (s === "SKIPPED_REFRESH_ONLY") return "刷新观察";
      if (s === "NO_ACTION") return "无动作";
      return signal || "--";
    }

    function executionLabel(status) {
      const s = String(status || "").toUpperCase();
      if (s === "SKIPPED_REFRESH_ONLY") return "刷新观察";
      if (s === "PAPER_FILLED") return "模拟已成交";
      if (s === "BLOCKED") return "已阻塞";
      if (s === "NO_ACTION") return "无动作";
      if (s === "PASS") return "通过";
      if (s === "HOLD") return "持有";
      if (s === "ORDER_OPEN") return "挂单中";
      if (s === "ORDER_LADDER_OPEN") return "挂单组";
      if (s === "FILLED") return "已成交";
      if (s === "CANCELED") return "已撤单";
      if (s === "EXPIRED") return "交易所过期";
      return status || "--";
    }

    function executionDetail(status, reason) {
      const s = String(status || "").toUpperCase();
      const mappedReason = reasonLabel(reason);
      if (reason) return mappedReason || reason;
      if (s === "SKIPPED_REFRESH_ONLY") return "本轮只刷新行情、持仓和风控线，不触发下单决策。";
      if (s === "PAPER_FILLED") return "本轮已产生成交，明细见 K 线下方成交记录。";
      if (s === "ORDER_OPEN") return "本轮已提交限价挂单，等待行情触价或撤单规则处理。";
      if (s === "ORDER_LADDER_OPEN") return "本轮已提交多级限价挂单组，等待行情触价、风险反转或重定价规则处理。";
      if (s === "UNKNOWN") return "订单接口状态不确定，下一轮会先查询订单状态，不直接补单。";
      if (s === "CANCELED") return "挂单已撤销，锁定资产已释放。";
      if (s === "EXPIRED") return "交易所返回订单过期，锁定资产已释放。";
      if (s === "BLOCKED") return "本轮动作被规则、预算、最小成交额或 AI 风险闸门阻塞。";
      if (s === "NO_ACTION" || s === "HOLD") return "本轮未下单，继续等待策略或退出条件。";
      if (s === "PASS") return "检查通过，但本轮没有需要执行的交易动作。";
      return "暂无执行说明。";
    }

    function triggerLabel(trigger) {
      const raw = String(trigger || "");
      const key = raw.toLowerCase();
      const labels = {
        strategy_buy: "策略买入",
        strategy_sell: "策略卖出",
        stop_loss: "硬止损",
        emergency_stop: "极端风险退出",
        take_profit: "止盈",
        trailing_stop: "跟踪止损",
        max_hold_exit: "超时退出",
        grid_profit_sell: "网格止盈卖出",
        grid_loss_recovery_sell: "亏损修复卖出",
        strategy_release_sell: "策略释放待回补",
        take_profit_release_sell: "止盈释放待回补",
        trailing_stop_release_sell: "跟踪止损释放待回补",
        max_hold_release_sell: "超时释放待回补",
        grid_buyback: "网格回补",
        target_rebuild_buy: "目标仓位补仓/建仓",
        grid_wait_buyback: "等待回补",
        grid_buyback_blocked: "回补受阻",
        grid_sell_blocked: "释放受阻",
        buyback_cooldown_blocks_loss_recovery: "冷却保护中",
        refresh_only: "仅刷新",
      };
      return labels[key] || raw || "--";
    }

    function parseLadderTiers(raw) {
      return String(raw || "").split(",").map((item) => {
        const [offsetRaw, fractionRaw] = item.split(":");
        return {
          offset: Math.max(0, asNumber(offsetRaw, 0)),
          fraction: Math.max(0, asNumber(fractionRaw, 0)),
        };
      }).filter((tier) => tier.fraction > 0);
    }

    function buildEntryPlan(c, payload) {
      const cfg = payload.runtime_config || {};
      const activation = c.activationState || {};
      const price = asNumber(c.currentPrice, 0);
      const quoteBalance = asNumber(c.paper.quote_balance ?? c.latest.quote_asset_balance, 0);
      const reservedQuote = asNumber(c.paper.reserved_quote_balance, 0);
      const availableCash = Math.max(0, quoteBalance - reservedQuote);
      const qty = asNumber((c.position || {}).quantity || c.positionDiag.quantity, 0);
      const positionValue = price > 0 ? Math.max(0, qty * price) : 0;
      const totalEquity = Math.max(0, availableCash + positionValue);
      const targetFraction = Math.min(1, Math.max(0, asNumber(cfg.target_position_fraction, 0)));
      const cashReserveFraction = Math.min(1, Math.max(0, asNumber(cfg.min_cash_reserve_fraction, 0)));
      const aiMultiplier = Math.min(1, Math.max(0, asNumber(c.buyDiag.ai_position_multiplier, 1)));
      const targetNotional = totalEquity * targetFraction * aiMultiplier;
      const cashReserve = totalEquity * cashReserveFraction;
      const maxSpend = Math.max(0, availableCash - cashReserve);
      const plannedSpend = Math.min(maxSpend, Math.max(0, targetNotional - positionValue));
      const baseQuantity = price > 0 ? plannedSpend / price : 0;
      const tiers = parseLadderTiers(cfg.entry_ladder_tiers || "0.0015:0.25,0.0025:0.25,0.0040:0.25");
      const tierRows = tiers.map((tier, index) => {
        const tierPrice = price > 0 ? price * (1 - tier.offset) : 0;
        const tierQuantity = baseQuantity * tier.fraction;
        const tierNotional = tierPrice * tierQuantity;
        return {
          index: index + 1,
          offset: tier.offset,
          fraction: tier.fraction,
          price: tierPrice,
          quantity: tierQuantity,
          notional: tierNotional,
        };
      });
      const blockers = [];
      if (!cfg.order_ladder_enabled) blockers.push("多级挂单未启用");
      if (String(c.latest.cycle_mode || "").toUpperCase() === "REFRESH") blockers.push("当前为刷新轮，等待决策轮或关键阈值事件");
      if (String(c.signal || "").toUpperCase() === "SELL") blockers.push("当前策略为卖出信号");
      if (c.aiRisk.allow_entry === false) blockers.push("AI 风险闸门否决入场");
      if (asNumber(activation.pending_buyback_quantity, 0) > 0) blockers.push("存在待回补仓位，优先回补");
      if (asNumber(c.latest.cooldown_remaining_bars || c.executionResult.cooldown_remaining_bars, 0) > 0) blockers.push("回补冷却期内");
      if (plannedSpend <= 0) blockers.push("目标仓位已满足或现金保留线不足");
      const minNotional = asNumber(c.buyDiag.min_notional_required || c.buyDiag.min_notional, 0);
      if (minNotional > 0 && tierRows.every((row) => row.notional < minNotional)) blockers.push("所有建仓梯队低于最小成交额");
      return {
        enabled: Boolean(cfg.order_ladder_enabled),
        targetFraction,
        cashReserveFraction,
        aiMultiplier,
        availableCash,
        positionValue,
        totalEquity,
        targetNotional,
        cashReserve,
        maxSpend,
        plannedSpend,
        baseQuantity,
        tiers: tierRows,
        blockers,
        status: blockers.length ? "等待条件" : "可在决策轮评估",
      };
    }

    function reasonLabel(reason) {
      const raw = String(reason || "");
      const key = raw.toLowerCase();
      const labels = {
        refresh_only: "仅刷新行情",
        existing_open_order: "已有挂单，等待处理",
        paper_limit_order_open: "模拟限价单已挂出，尚未成交",
        paper_limit_order_filled: "模拟限价单已成交",
        paper_limit_order_canceled: "模拟限价单未成交，已撤销",
        order_timeout_canceled: "旧版超时撤单",
        order_price_deviation_exceeded: "价格偏离撤单",
        order_stale_observed: "挂单已陈旧，继续等待触价",
        order_stale_reprice_requested: "挂单已陈旧，建议重定价",
        order_reprice_deviation_requested: "价差结构偏离，建议重定价",
        open_order_waiting_for_touch: "挂单等待触价成交",
        order_status_unknown_wait: "订单状态待确认，继续查询",
        ai_risk_worsened_cancel_open_buy: "AI 风险变差，撤买单",
        ai_risk_observed_keep_open_buy: "AI 普通风险，保留买单",
        ai_extreme_risk_cancel_open_buy: "AI 极端风险，撤买单",
        signal_reversed_cancel_open_buy: "信号反转，撤买单",
        signal_reversed_cancel_open_sell: "信号反转，撤卖单",
        paper_limit_order_expired: "交易所过期",
        ai_entry_veto: "AI 风险闸门否决入场",
        net_edge_too_small: "手续费后净边际不足",
        profitability_guard_passed: "交易收益闸门通过",
        target_position_reached_or_cash_reserved: "目标仓位已满足或现金保留线不足",
        target_position_disabled: "目标仓位部署未启用",
        target_notional_below_min_notional: "目标补仓金额低于最小成交额",
        target_quantity_below_min_qty: "目标补仓数量低于最小数量",
        buyback_cooldown_blocks_loss_recovery: "回补冷却期内禁止亏损修复卖出",
        sell_order_approved: "卖出订单已通过风控",
        buy_order_approved: "买入订单已通过风控",
      };
      if (key.startsWith("position_too_small_to_sell")) return "持仓低于可卖最小数量";
      if (key.startsWith("sell_notional_below_min_notional")) return "卖出金额低于最小成交额";
      if (key.startsWith("final_notional_below_min_notional")) return "买入金额低于最小成交额";
      if (key.startsWith("quantity_below_min_qty")) return "数量低于最小下单量";
      return labels[key] || raw || "--";
    }

    function labelWithRaw(label, raw) {
      const rawText = String(raw || "");
      if (!rawText || rawText === label || label === "--") return escapeHtml(label);
      return `${escapeHtml(label)}<br><span class="muted code">${escapeHtml(rawText)}</span>`;
    }

    function stateBlock(label, raw) {
      return `<div class="card-value">${escapeHtml(label)}</div><div class="card-note code">${escapeHtml(raw || "")}</div>`;
    }

    function cycleModeLabel(mode) {
      const m = String(mode || "").toLowerCase();
      if (m === "decision") return "决策轮";
      if (m === "refresh") return "刷新轮";
      return mode || "未知";
    }

    function boolLabel(value) {
      if (value === true) return "是";
      if (value === false) return "否";
      return "--";
    }

    function emptyBox(text) {
      return `<div class="empty">${text}</div>`;
    }

    function statusChip(label, kind = "wait") {
      return `<span class="status-chip ${kind}">${label}</span>`;
    }

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function shortOrderId(value) {
      const id = String(value || "").trim();
      if (!id) return "--";
      return id.length > 18 ? `${id.slice(0, 10)}…${id.slice(-6)}` : id;
    }

    function kvRows(rows) {
      const html = rows.map(([k, v]) => `
        <div class="kv-row"><span>${escapeHtml(k)}</span><strong>${v}</strong></div>
      `).join("");
      return `<div class="kv-grid">${html}</div>`;
    }

    function miniKvRows(rows) {
      const html = rows
        .filter(([_, value]) => value !== undefined && value !== null && String(value).trim() !== "")
        .map(([label, value]) => `
          <div class="mini-kv-row"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>
        `).join("");
      return html ? `<div class="mini-kv">${html}</div>` : "";
    }

    function table(headers, rows, emptyText) {
      if (!rows || rows.length === 0) return emptyBox(emptyText || "暂无数据");
      const thead = `<thead><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead>`;
      const tbody = `<tbody>${rows.join("")}</tbody>`;
      return `<div class="table-wrap scroll-table"><table>${thead}${tbody}</table></div>`;
    }

    function context(payload) {
      const latest = payload.latest_report || {};
      const paper = { ...(payload.paper_state || {}) };
      if (!Number.isFinite(Number(paper.total_equity)) && latest.total_equity !== undefined) paper.total_equity = latest.total_equity;
      if (!Number.isFinite(Number(paper.realized_pnl)) && latest.realized_pnl !== undefined) paper.realized_pnl = latest.realized_pnl;
      if (!Number.isFinite(Number(paper.unrealized_pnl)) && latest.unrealized_pnl !== undefined) paper.unrealized_pnl = latest.unrealized_pnl;
      const symbols = latest.symbols || [];
      const primary = symbols[0] || {};
      const symbol = payload.live_chart_symbol || primary.symbol || "XRPJPY";
      const positions = paper.positions || {};
      const position = positions[symbol] || null;
      const quoteAsset = primary.quote_asset || paper.quote_asset || "JPY";
      const requestedInterval = payload.requested_chart_interval || selectedChartInterval || payload.live_chart_interval || "1m";
      if (payload.live_chart_bars?.length) {
        chartBarsCache[requestedInterval] = payload.live_chart_bars;
      }
      const chartBars = payload.live_chart_bars?.length
        ? payload.live_chart_bars
        : chartBarsCache[requestedInterval]?.length
          ? chartBarsCache[requestedInterval]
          : requestedInterval === "1m" && payload.live_refresh_bars?.length
            ? payload.live_refresh_bars
            : requestedInterval === payload.live_main_interval && payload.live_main_interval_bars?.length
              ? payload.live_main_interval_bars
              : [];
      const currentPrice = asNumber(primary.current_price || primary.mark_price || (latest.market_prices || {})[symbol] || chartBars.slice(-1)[0]?.close, 0);
      const decision = (latest.decisions || [])[0] || {};
      const strategy = decision.strategy_decision || {};
      const rawSignal = strategy.signal || decision.action || decision.signal?.action || decision.signal || latest.signal?.action || latest.signal || "HOLD";
      const signal = typeof rawSignal === "object" ? (rawSignal.action || rawSignal.signal || "HOLD") : rawSignal;
      const execution = decision.execution_result || latest.execution_result || {};
      const executionResult = execution && typeof execution === "object" ? execution : {};
      const executionStatus = typeof execution === "object" ? (execution.status || execution.result || JSON.stringify(execution)) : execution;
      const executionReason = typeof execution === "object" ? (execution.reason || execution.message || "") : "";
      const llm = (latest.llm_assessments || [])[0] || latest.llm_analysis || {};
      const aiRisk = (latest.ai_risk_assessments || [])[0] || {};
      const buyDiag = (latest.buy_diagnostics || [])[0] || {};
      const sellDiag = (latest.sell_diagnostics || payload.sell_diagnostics || [])[0] || {};
      const positionDiag = (latest.position_diagnostics || [])[0] || {};
      const activationState = (payload.position_activation_state || paper.activation_state || {})[symbol] || {};
      const schedule = (latest.scheduling_diagnostics || [])[0] || {};
      const fills = payload.recent_fills || [];
      const tradeRecords = payload.trade_records || fills;
      const realCostBasis = payload.real_cost_basis_summary || {};
      const bars = chartBars;
      const markers = payload.live_trade_markers || [];
      const vetoes = payload.live_ai_veto_markers || [];
      const openOrders = payload.open_orders || latest.open_orders || [];
      const openOrderGroups = payload.open_order_groups || payload.order_ladder_summary || {};
      const targetInventory = (payload.target_inventory_summary || {})[symbol] || executionResult.target_inventory_summary || {};
      const directionDecision = (payload.direction_decision || executionResult.direction_decision || (latest.direction_decisions || [])[0] || {});
      const scenarioDecision = (payload.scenario_decision || executionResult.scenario_decision || (latest.scenario_decisions || [])[0] || {});
      const blendedScenarioDecision = (payload.blended_scenario_decision || executionResult.blended_scenario_decision || (latest.blended_scenario_decisions || [])[0] || scenarioDecision || {});
      const externalConsensus = (payload.external_consensus || executionResult.external_consensus || latest.external_consensus || [])[0] || {};
      const externalVotes = payload.external_signal_votes || latest.external_signal_votes || [];
      const effectiveOrderGuard = (payload.effective_order_guard || {})[symbol] || {};
      const dailyRiskBudget = (payload.daily_risk_budget || {})[symbol] || {};
      const orderEvents = payload.order_lifecycle_events || latest.order_lifecycle_events || [];
      const orderMarkers = payload.order_markers || [];
      const profitCurve = payload.live_profit_curve || [];
      return { latest, paper, primary, symbol, position, quoteAsset, currentPrice, decision, strategy, signal, executionResult, executionStatus, executionReason, llm, aiRisk, buyDiag, sellDiag, positionDiag, activationState, schedule, fills, tradeRecords, realCostBasis, bars, markers, vetoes, openOrders, openOrderGroups, targetInventory, directionDecision, scenarioDecision, blendedScenarioDecision, externalConsensus, externalVotes, effectiveOrderGuard, dailyRiskBudget, orderEvents, orderMarkers, profitCurve };
    }

    function syncChartIntervalOptions(payload) {
      const options = payload.chart_interval_options || [];
      if (!els.chartIntervalSelect || !options.length) return;
      const currentValues = Array.from(els.chartIntervalSelect.options).map((item) => item.value).join(",");
      const nextValues = options.map((item) => item.value).join(",");
      if (currentValues !== nextValues) {
        els.chartIntervalSelect.innerHTML = options.map((item) => `
          <option value="${escapeHtml(item.value)}">${escapeHtml(item.label)}</option>
        `).join("");
      }
      const requestedInterval = payload.requested_chart_interval || selectedChartInterval || payload.live_chart_interval || "1m";
      if (!options.some((item) => item.value === selectedChartInterval)) {
        selectedChartInterval = options.some((item) => item.value === requestedInterval) ? requestedInterval : options[0].value;
      }
      window.localStorage.setItem("boti.chartInterval", selectedChartInterval);
      els.chartIntervalSelect.value = selectedChartInterval;
    }

    function setChartLoading(active, text) {
      if (!els.chartLoading) return;
      els.chartLoading.classList.toggle("active", false);
      const label = els.chartLoading.querySelector("span:last-child");
      if (label && text) label.textContent = text;
    }

    function activateTab(tabName) {
      activeTab = tabName;
      document.querySelectorAll(".tab-button").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
      document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tabName}`));
      scheduleChartRender(lastPayloadSnapshot, { showLoading: false });
    }

    function updateTopBar(payload) {
      const c = context(payload);
      const availableCash = Math.max(0, asNumber(c.paper.quote_balance ?? c.latest.quote_asset_balance, 0) - asNumber(c.paper.reserved_quote_balance, 0));
      els.topSymbol.textContent = fmtSymbol(c.symbol, c.quoteAsset);
      els.topMode.textContent = cycleModeLabel(c.latest.cycle_mode);
      els.topUpdated.textContent = fmtTime(c.latest.generated_at || c.latest.timestamp || c.latest.timestamp_ms || payload.generated_at);
      els.topPrice.textContent = c.currentPrice ? fmtCurrency(c.currentPrice, c.quoteAsset) : "--";
      els.topCash.textContent = fmtCurrency(availableCash, c.quoteAsset);
      const totalPnlFromOriginal = asNumber(c.realCostBasis.total_pnl, NaN);
      els.topEquity.textContent = Number.isFinite(totalPnlFromOriginal)
        ? fmtCurrency(totalPnlFromOriginal, c.quoteAsset)
        : fmtCurrency(c.paper.net_pnl, c.quoteAsset);
    }

    function updateTradingTab(payload) {
      const c = context(payload);
      const pos = c.position || {};
      const qty = asNumber(pos.quantity || c.positionDiag.quantity, 0);
      const avg = asNumber(pos.average_entry_price || pos.entry_price || c.positionDiag.average_entry_price, 0);
      const realAvg = asNumber(c.activationState.real_average_entry_price, 0);
      const highest = asNumber(pos.highest_price || c.positionDiag.highest_price, 0);
      const holdBars = asNumber(pos.hold_bars || pos.bars_held || c.positionDiag.bars_held, 0);
      const botiUnrealized = asNumber(pos.unrealized_pnl ?? c.positionDiag.unrealized_pnl ?? c.paper.unrealized_pnl, 0);
      const realUnrealized = asNumber(c.realCostBasis.unrealized_pnl, qty > 0 && realAvg > 0 && c.currentPrice > 0 ? qty * (c.currentPrice - realAvg) : botiUnrealized);
      const originalTotalPnl = asNumber(c.realCostBasis.total_pnl, realUnrealized);
      const realizedOriginalPnl = asNumber(c.realCostBasis.realized_pnl, 0);
      const realized = asNumber(c.paper.realized_pnl, 0);
      const botiOperationPnl = asNumber(
        c.realCostBasis.boti_operation_pnl,
        asNumber(payload.boti_pnl_breakdown?.operation_pnl, realized)
      );
      const botiMarkToMarketPnl = asNumber(
        c.realCostBasis.boti_mark_to_market_pnl,
        asNumber(c.paper.net_pnl, realized + botiUnrealized)
      );
      const botiInitialEquity = asNumber(c.realCostBasis.boti_initial_equity, NaN);
      const realTotalEquity = asNumber(c.realCostBasis.current_total_equity, realAvg > 0 && qty > 0 && c.currentPrice > 0
        ? asNumber(c.paper.quote_balance, 0) + qty * c.currentPrice
        : c.paper.total_equity);
      const availableCash = Math.max(0, asNumber(c.paper.quote_balance ?? c.latest.quote_asset_balance, 0) - asNumber(c.paper.reserved_quote_balance, 0));
      const riskLines = activeRiskLines(c);
      const allowEntry = c.aiRisk.allow_entry;
      const executionResult = c.executionResult || {};
      const executionStatus = c.executionStatus || executionResult.status || "无执行";

      els.chartSubtitle.textContent = `${fmtSymbol(c.symbol, c.quoteAsset)} 实时观察 K 线、成交量、模拟成交点、AI 否决点、退出线`;
      const chartSource = payload.live_chart_source || "runtime";
      els.chartInterval.textContent = `图表 ${payload.live_chart_interval_label || payload.live_chart_interval || "1m"} / 策略 ${payload.live_main_interval || "--"} / ${chartSource}`;
      els.chartPointCount.textContent = `${c.bars.length} bars`;
      const lastProfitPoint = c.profitCurve.slice(-1)[0] || {};
      const latestRealizedPnl = asNumber(lastProfitPoint.realized_pnl, 0);
      if (els.profitCurveLabel) {
        els.profitCurveLabel.textContent = c.profitCurve.length
          ? `${fmtCurrency(latestRealizedPnl, c.quoteAsset)} / ${c.profitCurve.length} 点`
          : "暂无利润曲线";
      }

      els.positionCard.innerHTML = `
        <div class="card-label"><span>当前持仓</span>${qty > 0 ? statusChip("持仓中", "buy") : statusChip("空仓", "wait")}</div>
        <div class="card-value">${qty > 0 ? `${fmtNumber(qty, 6)} XRP` : "0 XRP"}</div>
        ${miniKvRows([
          ["真实成本", realAvg ? fmtCurrency(realAvg, c.quoteAsset) : "--"],
          ["Boti接管价", avg ? fmtCurrency(avg, c.quoteAsset) : "--"],
          ["最高价", fmtCurrency(highest, c.quoteAsset)],
          ["持仓K线", fmtNumber(holdBars, 0)],
          ["目标仓位", c.targetInventory.target_fraction !== undefined ? fmtPercent(c.targetInventory.target_fraction) : "--"],
          ["当前仓位", c.targetInventory.current_fraction !== undefined ? fmtPercent(c.targetInventory.current_fraction) : "--"],
          ["可买预算", c.targetInventory.available_buy_notional !== undefined ? fmtCurrency(c.targetInventory.available_buy_notional, c.quoteAsset) : "--"],
          ["可卖数量", c.targetInventory.allowed_sell_quantity !== undefined ? `${fmtNumber(c.targetInventory.allowed_sell_quantity, 6)} XRP` : "--"],
        ])}
      `;

      els.pnlCard.innerHTML = `
        <div class="card-label"><span>交割成本总盈亏</span><span class="${pnlClass(originalTotalPnl)}">原始成本</span></div>
        <div class="card-value ${pnlClass(originalTotalPnl)}">${fmtCurrency(originalTotalPnl, c.quoteAsset)}</div>
        ${miniKvRows([
          ["交割成本", fmtCurrency(c.realCostBasis.original_initial_equity, c.quoteAsset)],
          ["当前权益", fmtCurrency(realTotalEquity, c.quoteAsset)],
          ["原始已实现", fmtCurrency(realizedOriginalPnl, c.quoteAsset)],
          ["原始未实现", fmtCurrency(realUnrealized, c.quoteAsset)],
          ["Boti接手后操作盈亏", fmtCurrency(botiOperationPnl, c.quoteAsset)],
          ["接手后持仓浮动", fmtCurrency(botiMarkToMarketPnl, c.quoteAsset)],
          ["接手基线", Number.isFinite(botiInitialEquity) ? fmtCurrency(botiInitialEquity, c.quoteAsset) : "--"],
        ])}
      `;

      els.sellDecisionCard.innerHTML = `
        <div class="card-label"><span>卖出判断</span>${c.sellDiag.eligible_to_sell ? statusChip("可卖", "sell") : statusChip("持有", "wait")}</div>
        <div class="card-value">${c.sellDiag.eligible_to_sell ? fmtNumber(c.sellDiag.recommended_sell_quantity, 8) : "HOLD"}</div>
        ${miniKvRows([
          ["判断原因", escapeHtml(c.sellDiag.blocker || "暂无卖出诊断")],
          ["网格状态", escapeHtml(triggerLabel(c.sellDiag.activation_trigger || c.activationState.last_trigger || "--"))],
          ["建议数量", c.sellDiag.eligible_to_sell ? fmtNumber(c.sellDiag.recommended_sell_quantity, 8) : "--"],
          ["浮盈比例", fmtPercent(c.sellDiag.unrealized_pnl_pct)],
        ])}
      `;

      els.riskGateCard.innerHTML = `
        <div class="card-label"><span>AI 风险闸门</span>${allowEntry === false ? statusChip("否决", "block") : statusChip("允许/未触发", "wait")}</div>
        ${stateBlock(allowEntry === false ? "否决" : "通过", allowEntry === false ? "BLOCK" : "PASS")}
        <div class="card-note prose">${escapeHtml(c.aiRisk.reason || c.aiRisk.veto_reason || c.aiRisk.summary || "暂无 AI 风险闸门输出")}</div>
      `;

      const orderSummary = c.openOrderGroups || {};
      const nearestBuy = asNumber(orderSummary.nearest_buy_price, 0);
      const nearestSell = asNumber(orderSummary.nearest_sell_price, 0);
      const entryPlan = buildEntryPlan(c, payload);
      els.openOrderCard.innerHTML = asNumber(orderSummary.total_count, c.openOrders.length) > 0 ? `
        <div class="card-label"><span>当前挂单组</span>${statusChip(`${asNumber(orderSummary.total_count, c.openOrders.length)} 单`, "wait")}</div>
        <div class="card-value">${asNumber(orderSummary.buy_count, 0)} 买 / ${asNumber(orderSummary.sell_count, 0)} 卖</div>
        ${miniKvRows([
          ["最近买价", nearestBuy > 0 ? fmtCurrency(nearestBuy, c.quoteAsset) : "--"],
          ["最近卖价", nearestSell > 0 ? fmtCurrency(nearestSell, c.quoteAsset) : "--"],
          ["冻结现金", fmtCurrency(orderSummary.reserved_quote, c.quoteAsset)],
          ["冻结币数", orderSummary.reserved_base ? `${fmtNumber(orderSummary.reserved_base, 8)} XRP` : "--"],
          ["有效下限", c.effectiveOrderGuard.min_effective_order_notional ? fmtCurrency(c.effectiveOrderGuard.min_effective_order_notional, c.quoteAsset) : "--"],
          ["收益闸门", c.effectiveOrderGuard.guard_result ? escapeHtml(triggerLabel(c.effectiveOrderGuard.guard_result)) : "--"],
        ])}
      ` : `
        <div class="card-label"><span>当前挂单组</span>${statusChip("无挂单", "wait")}</div>
        <div class="card-value">--</div>
        <div class="card-note prose">当前没有等待成交的限价单。</div>
      `;

      const scenario = c.scenarioDecision || {};
      const scenarioIndicators = scenario.indicators || {};
      els.scenarioCard.innerHTML = `
        <div class="card-label"><span>场景判断</span>${statusChip(escapeHtml(scenario.scenario_state || "未知"), "wait")}</div>
        <div class="card-value small">${escapeHtml(scenario.scenario_state || "--")}</div>
        ${miniKvRows([
          ["MA扩散", scenarioIndicators.ma_expanding_periods !== undefined ? `${scenarioIndicators.ma_expanding_periods} 个周期` : "--"],
          ["ATR", scenarioIndicators.atr_pct !== undefined ? fmtPercent(scenarioIndicators.atr_pct) : "--"],
          ["量能", scenarioIndicators.volume_ratio !== undefined ? `${fmtNumber(scenarioIndicators.volume_ratio, 2)}x` : "--"],
          ["允许动作", (scenario.allowed_actions || []).join(" / ") || "--"],
          ["禁止动作", (scenario.blocked_actions || []).join(" / ") || "--"],
        ])}
        <div class="card-note detail">${escapeHtml(scenario.reason_cn || "暂无场景说明")}</div>
      `;

      const externalConsensus = c.externalConsensus || {};
      const externalVotes = c.externalVotes || [];
      const externalHealth = externalConsensus.health || {};
      const sourceText = externalVotes.length
        ? externalVotes.map((vote) => `${vote.source || "--"}:${vote.direction_vote || "--"} ${fmtPercent(vote.confidence || 0)}`).join(" / ")
        : "暂无外部合约数据";
      els.externalSignalCard.innerHTML = `
        <div class="card-label"><span>外部共识</span>${statusChip(escapeHtml(externalConsensus.direction_vote || "未启用"), externalConsensus.direction_vote === "BULLISH" ? "buy" : externalConsensus.direction_vote === "BEARISH" || externalConsensus.direction_vote === "RISK_OFF" ? "sell" : "wait")}</div>
        <div class="card-value small">${escapeHtml(externalConsensus.direction_vote || "--")}</div>
        ${miniKvRows([
          ["置信度", externalConsensus.confidence !== undefined ? fmtPercent(externalConsensus.confidence) : "--"],
          ["本地/外部", `${fmtPercent(externalConsensus.local_weight || 0.6)} / ${fmtPercent(externalConsensus.external_weight || 0.4)}`],
          ["可用源", externalConsensus.available_sources !== undefined ? `${externalConsensus.available_sources}/${externalConsensus.required_sources || 0}` : "--"],
          ["风险分", externalConsensus.risk_score !== undefined ? fmtPercent(externalConsensus.risk_score) : "--"],
          ["延迟", externalHealth.sources ? externalHealth.sources.map((item) => `${item.source}:${item.latency_ms || 0}ms`).join(" / ") : "--"],
        ])}
        <div class="card-note detail">${escapeHtml(externalConsensus.reason_cn || sourceText)}</div>
      `;

      const firstEntryTier = entryPlan.tiers[0] || {};
      const directionReason = c.directionDecision.reason_cn || c.targetInventory.reason || entryPlan.status;
      const policyDecision = c.executionResult.policy_decision || {};
      const policyProposalCount = (policyDecision.order_proposals || []).length;
      const policyMergedProposalCount = (policyDecision.merged_order_proposals || []).length;
      els.entryPlanCard.innerHTML = `
        <div class="card-label"><span>方向允许性 / 最终下单</span>${statusChip(policyProposalCount > 0 ? "已生成提案" : "未下单", policyProposalCount > 0 ? "buy" : "wait")}</div>
        <div class="card-value">${escapeHtml(c.directionDecision.recommended_action || "HOLD")}</div>
        ${miniKvRows([
          ["公平价", c.directionDecision.fair_value ? fmtCurrency(c.directionDecision.fair_value, c.quoteAsset) : "--"],
          ["买入区", c.directionDecision.buy_zone_price ? `<= ${fmtCurrency(c.directionDecision.buy_zone_price, c.quoteAsset)}` : "--"],
          ["卖出区", c.directionDecision.sell_zone_price ? `>= ${fmtCurrency(c.directionDecision.sell_zone_price, c.quoteAsset)}` : "--"],
          ["价格区间", escapeHtml(c.directionDecision.price_zone || "--")],
          ["候选提案", `${policyMergedProposalCount} 个`],
          ["通过提案", `${policyProposalCount} 个`],
          ["净边际", c.directionDecision.expected_net_edge_pct !== undefined ? fmtPercent(c.directionDecision.expected_net_edge_pct) : "--"],
          ["目标仓位", c.targetInventory.target_fraction !== undefined ? fmtPercent(c.targetInventory.target_fraction) : fmtPercent(entryPlan.targetFraction)],
          ["当前仓位", c.targetInventory.current_fraction !== undefined ? fmtPercent(c.targetInventory.current_fraction) : "--"],
          ["计划投入", c.targetInventory.available_buy_notional !== undefined ? fmtCurrency(c.targetInventory.available_buy_notional, c.quoteAsset) : fmtCurrency(entryPlan.plannedSpend, c.quoteAsset)],
        ])}
        <div class="card-note detail">${escapeHtml(directionReason || "暂无方向说明")}</div>
      `;

      const cfg = lastPayloadSnapshot?.runtime_config || payload?.runtime_config || {};
      const targetFraction = asNumber(executionResult.target_position_fraction, asNumber(cfg.target_position_fraction, 0));
      const cashReserveFraction = asNumber(executionResult.min_cash_reserve_fraction, asNumber(cfg.min_cash_reserve_fraction, 0));
      const capitalDeployment = Boolean(executionResult.capital_deployment || executionResult.trigger === "target_rebuild_buy");
      const submittedCount = asNumber(executionResult.submitted_count, executionStatus === "ORDER_OPEN" ? 1 : 0);
      const operationAction = executionResult.trigger
        ? triggerLabel(executionResult.trigger)
        : executionStatus === "SKIPPED_REFRESH_ONLY"
          ? "仅刷新"
          : executionStatus === "NO_ACTION" || executionStatus === "HOLD"
            ? "无交易动作"
            : executionLabel(executionStatus);
      const operationRows = capitalDeployment
        ? [
            ["动作类型", escapeHtml(triggerLabel(executionResult.trigger || "target_rebuild_buy"))],
            ["目标仓位", targetFraction ? fmtPercent(targetFraction) : "--"],
            ["现金保留", cashReserveFraction ? fmtPercent(cashReserveFraction) : "--"],
            ["挂单层数", submittedCount ? `${fmtNumber(submittedCount, 0)} 层` : "--"],
          ]
        : [
            ["动作类型", escapeHtml(operationAction)],
            ["止损", riskLines.stopLoss || "--"],
            ["止盈", riskLines.takeProfit || "--"],
            ["跟踪", riskLines.trailingStop || "--"],
          ];
      els.executionCard.innerHTML = `
        <div class="card-label"><span>本轮操作</span>${statusChip(signalLabel(c.signal), signalClass(c.signal))}</div>
        <div class="card-value">${escapeHtml(executionLabel(executionStatus))}</div>
        <div class="card-note prose">${escapeHtml(executionDetail(executionStatus, c.executionReason))}</div>
        ${miniKvRows(operationRows)}
      `;

      renderFills(c.tradeRecords, c.quoteAsset);
    }

    function updateAiTab(payload) {
      const c = context(payload);
      const actionBias = c.llm.action_bias_cn || c.llm.action_bias || c.llm.recommendation || c.llm.action || "未输出";
      const marketState = c.llm.regime_cn || c.llm.market_state || c.llm.market_regime || c.llm.summary_cn || c.llm.summary || "暂无市场状态";
      const riskNote = c.llm.risk_note_cn || c.llm.risk_note || c.llm.risk_summary || c.aiRisk.reason || c.aiRisk.veto_reason || "暂无风险提示";
      const ruleSignal = c.signal;
      const aiVerdict = c.aiRisk.allow_entry === false ? "AI 否决入场" : "AI 未否决";

      els.aiSummaryCard.innerHTML = kvRows([
        ["模型", escapeHtml(c.llm.model || c.latest.llm_model || "模型未命中，显示兼容端点")],
        ["市场状态", escapeHtml(marketState)],
        ["行动偏向", escapeHtml(actionBias)],
        ["风险提示", escapeHtml(riskNote)],
        ["更新时间", escapeHtml(fmtTime(c.llm.generated_at || c.latest.generated_at))]
      ]);

      els.ruleVsAiCard.innerHTML = `
        <div class="metric-grid">
          <div class="metric-tile"><span>规则信号</span><strong>${signalLabel(ruleSignal)}</strong></div>
          <div class="metric-tile"><span>AI 裁决</span><strong>${escapeHtml(aiVerdict)}</strong></div>
          <div class="metric-tile"><span>允许入场</span><strong>${boolLabel(c.aiRisk.allow_entry)}</strong></div>
        </div>
        <div class="card-note">关系：策略负责产生买卖信号；AI 风险闸门只做否决、缩仓或风险解释，不新增主动买点。</div>
      `;

      els.evidenceFull.innerHTML = renderEvidence(c.latest, 16);
      els.aiRiskFull.innerHTML = renderAiRisk(c);
    }

    function updateBacktestTab(payload) {
      const summary = payload.backtest_summary || {};
      const metrics = summary.metrics || summary || {};
      const source = payload.backtest_source || "未找到回测目录";
      const available = payload.backtest_available === true;
      const missingReason = payload.backtest_missing_reason || "";

      els.btTotalReturn.textContent = fmtPercent(metrics.total_return_pct);
      els.btMaxDrawdown.textContent = fmtPercent(metrics.max_drawdown_pct);
      els.btWinRate.textContent = fmtPercent(metrics.win_rate);
      els.btProfitFactor.textContent = fmtNumber(metrics.profit_factor, 3);
      els.btExpectancy.textContent = fmtNumber(metrics.expectancy_per_trade, 4);
      els.btTradeCount.textContent = `${fmtNumber(metrics.trade_count, 0)} / ${fmtNumber(metrics.completed_trade_count, 0)}`;
      els.btSourceLabel.textContent = available ? `数据源 ${source}` : (missingReason || "缺失 runtime_backtest_walk / runtime_backtest_check");

      const segments = payload.backtest_segments || [];
      const segmentRows = segments.map((s) => {
        const m = s.metrics || s.summary || s;
        return `<tr>
          <td>${escapeHtml(s.segment_index ?? s.index ?? "--")}</td>
          <td class="nowrap">${escapeHtml((s.train_from || "").slice(0, 10))}<br>${escapeHtml((s.train_to || "").slice(0, 10))}</td>
          <td class="nowrap">${escapeHtml((s.test_from || "").slice(0, 10))}<br>${escapeHtml((s.test_to || "").slice(0, 10))}</td>
          <td class="${pnlClass(m.total_return_pct)}">${fmtPercent(m.total_return_pct)}</td>
          <td>${fmtPercent(m.max_drawdown_pct)}</td>
          <td>${fmtPercent(m.win_rate)}</td>
          <td>${boolLabel(s.beats_baseline)}</td>
        </tr>`;
      });
      els.btSegments.innerHTML = table(["段", "训练窗", "测试窗", "收益", "回撤", "胜率", "优于基线"], segmentRows, missingReason || "暂无 walk-forward segment 文件");

      const trades = (payload.backtest_trades || []).slice(-80).reverse();
      const tradeRows = trades.map((t) => `<tr>
        <td class="nowrap">${escapeHtml((t.entry_time || "").slice(0, 16))}<br>${escapeHtml((t.exit_time || "").slice(0, 16))}</td>
        <td>${fmtNumber(t.entry_price, 4)}<br>${fmtNumber(t.exit_price, 4)}</td>
        <td class="${pnlClass(t.realized_pnl)}">${fmtNumber(t.realized_pnl, 4)}</td>
        <td class="${pnlClass(t.return_pct)}">${fmtPercent(t.return_pct)}</td>
        <td>${fmtNumber(t.hold_bars, 0)} / ${fmtNumber(t.hold_hours, 1)}h</td>
        <td>${fmtPercent(t.mfe_pct)} / ${fmtPercent(t.mae_pct)}</td>
        <td>${escapeHtml(t.exit_reason || "--")}</td>
      </tr>`);
      els.btTrades.innerHTML = table(["开平仓", "价格", "盈亏", "收益率", "持仓", "MFE/MAE", "退出"], tradeRows, missingReason || "暂无回测交易明细");

      const manifest = payload.backtest_manifest || {};
      els.btManifest.innerHTML = Object.keys(manifest).length ? kvRows(Object.entries(flattenObject(manifest)).slice(0, 20).map(([k, v]) => [k, escapeHtml(String(v))])) : emptyBox(missingReason || "暂无 run_manifest.json");
    }

    function updateRiskTab(payload) {
      const c = context(payload);
      const buy = c.buyDiag || {};
      const sell = c.sellDiag || {};
      const activation = c.activationState || {};
      const risk = activeRiskLines(c);
      const buybackStep = asNumber((payload.runtime_config || {}).grid_buyback_step_pct, 0);
      const lastReleasePrice = asNumber(activation.last_grid_sell_price, 0);
      const buybackTriggerPrice = asNumber(
        activation.buyback_trigger_price,
        lastReleasePrice > 0 && buybackStep > 0 ? lastReleasePrice * (1 - buybackStep) : 0
      );
      const pendingBuyback = asNumber(activation.pending_buyback_quantity, 0);

      els.buyDecisionFull.innerHTML = kvRows([
        ["规则信号", statusChip(signalLabel(c.signal), signalClass(c.signal))],
        ["Quote 预算", escapeHtml(fmtCurrency(buy.quote_budget || buy.budget_quote, c.quoteAsset))],
        ["最小成交额", escapeHtml(fmtCurrency(buy.min_notional || buy.min_notional_required, c.quoteAsset))],
        ["原始数量", escapeHtml(fmtNumber(buy.raw_quantity, 8))],
        ["取整数量", escapeHtml(fmtNumber(buy.rounded_quantity || buy.order_quantity || buy.adjusted_quantity, 8))],
        ["是否可下单", escapeHtml(boolLabel(buy.can_place_order ?? buy.eligible_to_buy))]
      ]);

      const entryPlan = buildEntryPlan(c, payload);
      const entryRows = entryPlan.tiers.map((tier) => `<tr>
        <td>${escapeHtml(String(tier.index))}</td>
        <td>${escapeHtml(fmtPercent(tier.offset))}</td>
        <td>${escapeHtml(fmtCurrency(tier.price, c.quoteAsset))}</td>
        <td>${escapeHtml(fmtNumber(tier.quantity, 8))}</td>
        <td>${escapeHtml(fmtCurrency(tier.notional, c.quoteAsset))}</td>
      </tr>`);
      els.entryPlanFull.innerHTML = `
        ${kvRows([
          ["当前状态", entryPlan.blockers.length ? statusChip("等待条件", "wait") : statusChip("可在决策轮评估", "buy")],
          ["目标仓位", escapeHtml(fmtPercent(entryPlan.targetFraction))],
          ["现金保留", escapeHtml(fmtPercent(entryPlan.cashReserveFraction))],
          ["可用现金", escapeHtml(fmtCurrency(entryPlan.availableCash, c.quoteAsset))],
          ["目标持仓额", escapeHtml(fmtCurrency(entryPlan.targetNotional, c.quoteAsset))],
          ["计划投入", escapeHtml(fmtCurrency(entryPlan.plannedSpend, c.quoteAsset))],
          ["预估总数量", escapeHtml(`${fmtNumber(entryPlan.baseQuantity, 8)} XRP`)],
          ["阻塞/等待原因", escapeHtml(entryPlan.blockers.join("；") || "无，下一决策轮可评估建仓/补仓")]
        ])}
        <div style="height:10px"></div>
        ${table(["档", "回落", "预估挂买价", "数量", "金额"], entryRows, "暂无建仓梯队")}
      `;

      els.exitRiskCard.innerHTML = kvRows([
        ["是否适合卖出", sell.eligible_to_sell ? statusChip("可卖", "sell") : statusChip("持有", "wait")],
        ["建议卖出数量", escapeHtml(fmtNumber(sell.recommended_sell_quantity, 8))],
        ["卖出原因", escapeHtml(sell.blocker || "--")],
        ["止损线", risk.stopLoss || "--"],
        ["止盈线", risk.takeProfit || "--"],
        ["跟踪止损", risk.trailingStop || "--"],
        ["动态退出", escapeHtml((sell.blocker_details || []).find((item) => String(item).startsWith("动态退出")) || "未输出")],
        ["持仓 K 线数", escapeHtml(fmtNumber(sell.bars_held ?? (c.position || {}).hold_bars, 0))],
        ["触发状态", escapeHtml(c.positionDiag.exit_trigger || c.positionDiag.exit_reason || "未触发")]
      ]);

      els.riskParametersCard.innerHTML = kvRows([
        ["最近触发", escapeHtml(triggerLabel(activation.last_trigger || sell.activation_trigger || "--"))],
        ["待回补数量", escapeHtml(fmtNumber(pendingBuyback, 8))],
        ["最近释放卖价", escapeHtml(fmtCurrency(lastReleasePrice, c.quoteAsset))],
        ["回补触发价", escapeHtml(buybackTriggerPrice ? fmtCurrency(buybackTriggerPrice, c.quoteAsset) : "--")],
        ["回补层级", escapeHtml(`${fmtNumber(asNumber(activation.buyback_tier_index, 0) + 1, 0)} / ${fmtPercent(asNumber(activation.buyback_tier_net_edge_pct, 0))}`)],
        ["回补状态", pendingBuyback > 0 ? (c.currentPrice <= buybackTriggerPrice ? statusChip("已到回补线", "buy") : statusChip("等待回落", "wait")) : statusChip("无待回补", "wait")],
        ["同步参考价", escapeHtml(fmtCurrency(activation.seed_price, c.quoteAsset))],
        ["真实成本", escapeHtml(fmtCurrency(activation.real_average_entry_price, c.quoteAsset))],
        ["成本来源", escapeHtml(activation.cost_basis_source || "--")],
        ["当日次数", escapeHtml(`${activation.daily_trade_count ?? 0} / 8`)],
        ["最近原因", escapeHtml(activation.last_reason || "--")]
      ]);
    }

    function updateSystemTab(payload) {
      const c = context(payload);
      const history = payload.history || [];
      const historyCount = payload.history_count ?? history.length;
      const ledger = payload.decision_ledger || [];
      const schedule = c.schedule || {};

      els.systemStateCard.innerHTML = kvRows([
        ["Cycle Mode", escapeHtml(cycleModeLabel(c.latest.cycle_mode))],
        ["本轮时间", escapeHtml(fmtTime(c.latest.generated_at || c.latest.timestamp || c.latest.timestamp_ms))],
        ["最近周期", escapeHtml(String(historyCount))],
        ["最近成交", escapeHtml(String(c.fills.length))],
        ["主周期 bars", escapeHtml(String(c.bars.length))]
      ]);

      els.schedulingFull.innerHTML = kvRows([
        ["新闻刷新", escapeHtml(schedule.news_refresh_status || schedule.news_status || c.latest.news_refresh_status || "--")],
        ["下次新闻", escapeHtml(fmtTime(schedule.next_news_refresh_at || c.latest.news_next_refresh_ms))],
        ["监控间隔", escapeHtml(schedule.monitor_interval_seconds ? `${schedule.monitor_interval_seconds}s` : "--")],
        ["决策间隔", escapeHtml(schedule.decision_interval_seconds ? `${schedule.decision_interval_seconds}s` : "--")],
        ["最近原因", escapeHtml(schedule.reason || schedule.decision_reason || c.latest.cycle_reason || c.latest.reason || "--")]
      ]);

      els.payloadHealthCard.innerHTML = kvRows([
        ["行情数据", c.bars.length ? statusChip("可用", "buy") : statusChip("缺失", "block")],
        ["回测数据", payload.backtest_available ? statusChip("可用", "buy") : statusChip("空状态", "wait")],
        ["回测来源", escapeHtml(payload.backtest_source || "--")],
        ["AI 否决点", escapeHtml(String(c.vetoes.length))],
        ["成交标记", escapeHtml(String(c.markers.length))]
      ]);

      const rows = ledger.slice(0, 80).map((r) => `<tr>
        <td>${escapeHtml(fmtTime(r.timestamp_ms))}</td>
        <td>${escapeHtml(cycleModeLabel(r.cycle_mode))}</td>
        <td>${escapeHtml(fmtSymbol(r.symbol, c.quoteAsset))}</td>
        <td>${escapeHtml(fmtCurrency(r.price, c.quoteAsset))}</td>
        <td>${escapeHtml(r.position_quantity ? fmtNumber(r.position_quantity, 8) : "0")}</td>
        <td>${escapeHtml(r.buy_blocker || "--")}</td>
        <td>${escapeHtml(r.sell_blocker || "--")}</td>
        <td>${escapeHtml(r.final_action || "--")} / ${escapeHtml(r.execution_status || "--")}</td>
      </tr>`);
      els.cycleLedger.innerHTML = table(["时间", "模式", "交易对", "价格", "持仓", "买入判断", "卖出判断", "最终动作"], rows, "暂无历史决策账本");
    }

    function renderEvidence(latest, limit) {
      const evidence = latest.evidence_items || latest.news_evidence || latest.news_items || [];
      if (!evidence.length) return emptyBox("本轮没有可展示的新闻或证据来源");
      const rows = evidence.slice(0, limit).map((item) => {
        const title = item.title || item.headline || item.source || "未命名来源";
        const source = item.source || item.publisher || item.url || "--";
        const ts = item.published_at || item.published_at_ms || item.timestamp || item.timestamp_ms || item.time || "";
        const score = item.score ?? item.sentiment_score ?? item.relevance ?? "--";
        return `<tr>
          <td>${escapeHtml(title)}</td>
          <td>${escapeHtml(source)}</td>
          <td class="nowrap">${escapeHtml(fmtTime(ts))}</td>
          <td>${escapeHtml(String(score))}</td>
        </tr>`;
      });
      return table(["标题", "来源", "时间", "分数"], rows, "暂无证据来源");
    }

    function renderAiTimeline(c) {
      const items = [
        ["规则信号", signalLabel(c.signal), c.strategy.reason || c.decision.reason || "策略规则输出"],
        ["AI 市场判断", c.llm.action_bias_cn || c.llm.action_bias || c.llm.recommendation || "未输出", c.llm.summary_cn || c.llm.summary || c.llm.risk_note_cn || c.llm.risk_note || "暂无说明"],
        ["风险闸门", c.aiRisk.allow_entry === false ? "否决入场" : "未否决", c.aiRisk.reason || c.aiRisk.veto_reason || "暂无风险解释"],
        ["执行结果", c.executionStatus || "无执行", c.executionReason || c.decision.execution_reason || ""]
      ].filter((item) => String(item[1] || item[2] || "").trim());
      return `<div class="timeline">${items.map((item) => `
        <div class="timeline-item">
          <div class="timeline-time">${escapeHtml(item[0])}</div>
          <div class="timeline-title">${escapeHtml(item[1])}</div>
          <div class="timeline-body">${escapeHtml(item[2])}</div>
        </div>
      `).join("")}</div>`;
    }

    function renderDecisionDrawer(payload) {
      const c = context(payload);
      const ledger = payload.decision_ledger || [];
      const orderEvents = c.orderEvents || [];
      const ledgerShown = Math.min(ledger.length, 100);
      const eventsShown = Math.min(orderEvents.length, 100);
      const meta = payload.decision_drawer_meta || {};
      const ledgerRows = ledger.slice(0, 100).map((r) => `<tr>
        <td class="nowrap">${escapeHtml(fmtTime(r.timestamp_ms || r.time))}</td>
        <td>${labelWithRaw(cycleModeLabel(r.cycle_mode), r.cycle_mode)}</td>
        <td>${escapeHtml(fmtSymbol(r.symbol || c.symbol, c.quoteAsset))}</td>
        <td>${escapeHtml(fmtCurrency(r.price, c.quoteAsset))}</td>
        <td>${labelWithRaw(signalLabel(r.buy_signal), r.buy_signal)}<br><span class="muted">${escapeHtml(reasonLabel(r.buy_blocker))}</span></td>
        <td>${labelWithRaw(signalLabel(r.sell_signal), r.sell_signal)}<br><span class="muted">${escapeHtml(reasonLabel(r.sell_blocker))}</span></td>
        <td>${labelWithRaw(signalLabel(r.final_action), r.final_action)}<br><span class="muted">${escapeHtml(executionLabel(r.execution_status))}</span></td>
      </tr>`);
      const eventRows = orderEvents.slice(0, 100).map((e) => `<tr>
        <td class="nowrap">${escapeHtml(fmtTime(e.timestamp_ms || e.time))}</td>
        <td class="code" title="${escapeHtml(e.client_order_id || "")}">${escapeHtml(shortOrderId(e.client_order_id))}</td>
        <td>${statusChip(signalLabel(e.status || e.event_type || "--"), signalClass(e.status || e.side))}<br><span class="muted code">${escapeHtml(e.status || e.event_type || "--")}</span></td>
        <td>${labelWithRaw(signalLabel(e.side), e.side)}</td>
        <td>${fmtCurrency(e.fill_price || e.limit_price, c.quoteAsset)}</td>
        <td>${fmtNumber(e.filled_quantity || e.quantity, 8)}</td>
        <td>${escapeHtml(reasonLabel(e.reason || e.trigger))}<br><span class="muted">${escapeHtml(triggerLabel(e.trigger))}</span></td>
      </tr>`);
      const externalConsensus = c.externalConsensus || {};
      const blendedScenario = c.blendedScenarioDecision || {};
      const externalVoteRows = (c.externalVotes || []).map((vote) => `<tr>
        <td>${escapeHtml(vote.source || "--")}</td>
        <td>${labelWithRaw(signalLabel(vote.direction_vote), vote.direction_vote)}</td>
        <td>${escapeHtml(fmtPercent(vote.confidence || 0))}</td>
        <td>${escapeHtml((vote.risk_flags || []).join(" / ") || "--")}</td>
        <td>${escapeHtml(vote.source_latency_ms !== undefined ? `${vote.source_latency_ms}ms` : "--")}</td>
        <td>${escapeHtml(vote.reason_cn || "--")}</td>
      </tr>`);
      return `
        <div class="drawer-section">
          <div class="drawer-section-title">本地 60 / 外部 40 融合明细</div>
          ${kvRows([
            ["本地场景", labelWithRaw(blendedScenario.scenario_state || c.scenarioDecision.scenario_state || "--", c.scenarioDecision.scenario_state || "")],
            ["外部共识", labelWithRaw(signalLabel(externalConsensus.direction_vote), externalConsensus.direction_vote || "--")],
            ["外部置信度", escapeHtml(externalConsensus.confidence !== undefined ? fmtPercent(externalConsensus.confidence) : "--")],
            ["可用来源", escapeHtml(externalConsensus.available_sources !== undefined ? `${externalConsensus.available_sources}/${externalConsensus.required_sources || 0}` : "--")],
            ["融合后说明", escapeHtml(blendedScenario.reason_cn || externalConsensus.reason_cn || "暂无融合说明")],
          ])}
          ${table(["来源", "方向", "置信度", "风险标记", "延迟", "原因"], externalVoteRows, "暂无外部票选明细")}
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">历史决策账本 <span class="drawer-count">已加载 ${ledger.length} 条，显示 ${ledgerShown} 条${meta.scan_lines ? `，扫描 ${meta.scan_lines} 行` : ""}</span></div>
          ${table(["时间", "轮次", "交易对", "价格", "买入判断", "卖出判断", "最终动作"], ledgerRows, "暂无历史决策账本")}
        </div>
        <div class="drawer-section">
          <div class="drawer-section-title">订单生命周期事件 <span class="drawer-count">已加载 ${orderEvents.length} 条，显示 ${eventsShown} 条</span></div>
          ${table(["时间", "单据ID", "状态", "方向", "价格", "数量", "原因"], eventRows, "暂无订单生命周期事件")}
        </div>
      `;
    }

    function openInsightDrawer(kind) {
      const payload = lastPayloadSnapshot;
      if (!payload) return;
      const c = context(payload);
      activeDrawerKind = kind;
      if (kind === "evidence") {
        els.insightDrawerTitle.textContent = "证据来源";
        els.insightDrawerSubtitle.textContent = "本轮决策引用的新闻、市场信息与来源";
        els.insightDrawerBody.innerHTML = renderEvidence(c.latest, 30);
      } else {
        els.insightDrawerTitle.textContent = "决策链路";
        els.insightDrawerSubtitle.textContent = "按真实 cycle_reports 与订单事件生成，不使用固定假数据";
        els.insightDrawerBody.innerHTML = renderDecisionDrawer(payload);
        refreshDecisionDrawer();
      }
      els.insightDrawer.classList.add("active");
      els.insightDrawerBackdrop.classList.add("active");
      els.insightDrawer.setAttribute("aria-hidden", "false");
    }

    function closeInsightDrawer() {
      activeDrawerKind = null;
      els.insightDrawer.classList.remove("active");
      els.insightDrawerBackdrop.classList.remove("active");
      els.insightDrawer.setAttribute("aria-hidden", "true");
    }

    function renderAiRisk(c) {
      return kvRows([
        ["允许入场", statusChip(boolLabel(c.aiRisk.allow_entry), c.aiRisk.allow_entry === false ? "block" : "wait")],
        ["缩仓比例", escapeHtml(c.aiRisk.position_scale_pct !== undefined ? fmtPercent(c.aiRisk.position_scale_pct) : fmtNumber(c.aiRisk.position_multiplier, 3))],
        ["风险等级", escapeHtml(c.aiRisk.risk_level || c.aiRisk.level || "--")],
        ["裁决原因", escapeHtml(c.aiRisk.reason || c.aiRisk.veto_reason || c.aiRisk.summary || "--")],
        ["证据数量", escapeHtml(String((c.latest.evidence_items || c.latest.news_evidence || []).length))]
      ]);
    }

    function renderFills(records, quoteAsset) {
      const allRecords = records || [];
      const filteredRecords = allRecords.filter((record) => {
        const status = String(record.status || record.event_type || "").toUpperCase();
        const side = String(record.side || record.action || "").toUpperCase();
        if (fillFilter === "filled") return status === "FILLED" || status === "PAPER_FILLED";
        if (fillFilter === "open") return status === "OPEN" || status === "NEW" || status === "PARTIALLY_FILLED" || status === "UNKNOWN";
        if (fillFilter === "closed") return status === "CANCELED" || status === "EXPIRED" || status === "REJECTED";
        if (fillFilter === "buy") return side === "BUY";
        if (fillFilter === "sell") return side === "SELL";
        return true;
      });
      const total = filteredRecords.length;
      const pageCount = Math.max(1, Math.ceil(total / fillPageSize));
      fillPage = Math.max(0, Math.min(fillPage, pageCount - 1));
      const start = fillPage * fillPageSize;
      const pageRecords = filteredRecords.slice(start, start + fillPageSize);
      const rows = pageRecords.map((f) => {
        const side = String(f.side || f.action || "").toUpperCase();
        const status = String(f.status || "").toUpperCase();
        const statusKind = status === "FILLED" || status === "PAPER_FILLED"
          ? "buy"
          : status === "CANCELED" || status === "EXPIRED" || status === "REJECTED"
            ? "block"
            : "wait";
        const reservedQuote = asNumber(f.reserved_quote, 0);
        const reservedBase = asNumber(f.reserved_base, 0);
        const reservedAsset = f.reserved_asset || (reservedBase > 0 ? (f.base_asset || "BASE") : quoteAsset);
        const frozenText = reservedQuote > 0
          ? fmtCurrency(reservedQuote, quoteAsset)
          : reservedBase > 0
            ? `${fmtNumber(reservedBase, 8)} ${escapeHtml(reservedAsset)}`
            : "--";
        const fee = asNumber(f.fee, 0);
        const estimatedFee = asNumber(f.estimated_fee, 0);
        const isFilled = status === "FILLED" || status === "PAPER_FILLED";
        const statusText = isFilled
          ? signalLabel(status)
          : status === "OPEN"
            ? "挂单未成交"
            : status === "CANCELED"
              ? "未成交已撤单"
              : signalLabel(status || f.event_type || "--");
        const feeText = fee > 0 ? fmtCurrency(fee, quoteAsset) : isFilled && estimatedFee > 0 ? `预计 ${fmtCurrency(estimatedFee, quoteAsset)}` : "--";
        const realizedText = isFilled ? fmtCurrency(f.realized_pnl, quoteAsset) : "--";
        const reasonText = reasonLabel(f.reason || f.event_type || status);
        const targetSpread = asNumber(f.target_spread_pct, 0);
        const currentSpread = asNumber(f.current_spread_pct, 0);
        const tolerance = asNumber(f.reprice_tolerance_pct, 0);
        const spreadText = targetSpread || currentSpread || tolerance
          ? `目标 ${fmtPercent(targetSpread * 100, 2)}<br><span class="muted">当前 ${fmtPercent(currentSpread * 100, 2)} / 容差 ${fmtPercent(tolerance * 100, 2)}</span>`
          : "--";
        return `<tr>
          <td class="code" title="${escapeHtml(f.client_order_id || "")}">${escapeHtml(shortOrderId(f.client_order_id))}</td>
          <td>${statusChip(statusText, statusKind)}<br><span class="muted">${escapeHtml(reasonText)}</span></td>
          <td>${statusChip(side || "--", side === "BUY" ? "buy" : side === "SELL" ? "sell" : "wait")}</td>
          <td>${fmtNumber(f.quantity, 8)}</td>
          <td>${fmtCurrency(f.price || f.fill_price || f.limit_price, quoteAsset)}</td>
          <td>${spreadText}</td>
          <td>${escapeHtml(frozenText)}</td>
          <td>${escapeHtml(feeText)}</td>
          <td class="${pnlClass(isFilled ? f.realized_pnl : 0)}">${realizedText}</td>
          <td class="nowrap">${escapeHtml(fmtTime(f.timestamp || f.timestamp_ms || f.time))}</td>
        </tr>`;
      });
      if (els.fillPageInfo) {
        const end = Math.min(total, start + pageRecords.length);
        els.fillPageInfo.textContent = total ? `${start + 1}-${end} / ${total}（全量 ${allRecords.length}）` : `0 / 0（全量 ${allRecords.length}）`;
      }
      if (els.fillFilter) els.fillFilter.value = fillFilter;
      if (els.fillPrev) els.fillPrev.disabled = fillPage <= 0;
      if (els.fillNext) els.fillNext.disabled = fillPage >= pageCount - 1;
      if (els.fillPageSize) els.fillPageSize.value = String(fillPageSize);
      els.tradeFillsTable.innerHTML = table(["单据ID", "订单状态", "方向", "数量", "挂单/成交价", "价差", "冻结", "手续费", "已实现", "时间"], rows, "暂无订单或成交记录");
    }

    function activeRiskLines(c) {
      const pos = c.position || {};
      const diag = c.positionDiag || {};
      const stopLoss = pos.stop_loss_price || diag.stop_loss_price;
      const takeProfit = pos.take_profit_price || diag.take_profit_price;
      const trailingStop = pos.trailing_stop_price || diag.trailing_stop_price;
      return {
        stopLoss: stopLoss ? escapeHtml(fmtCurrency(stopLoss, c.quoteAsset)) : "",
        takeProfit: takeProfit ? escapeHtml(fmtCurrency(takeProfit, c.quoteAsset)) : "",
        trailingStop: trailingStop ? escapeHtml(fmtCurrency(trailingStop, c.quoteAsset)) : "",
        numeric: {
          stop_loss_price: asNumber(stopLoss, NaN),
          take_profit_price: asNumber(takeProfit, NaN),
          trailing_stop_price: asNumber(trailingStop, NaN)
        }
      };
    }

    function flattenObject(obj, prefix = "") {
      const out = {};
      Object.entries(obj || {}).forEach(([key, value]) => {
        const path = prefix ? `${prefix}.${key}` : key;
        if (value && typeof value === "object" && !Array.isArray(value)) {
          Object.assign(out, flattenObject(value, path));
        } else {
          out[path] = Array.isArray(value) ? JSON.stringify(value) : value;
        }
      });
      return out;
    }

    function updateDom(payload, options = {}) {
      lastPayloadSnapshot = payload;
      saveSnapshotCache(payload);
      syncChartIntervalOptions(payload);
      updateTopBar(payload);
      updateTradingTab(payload);
      updateAiTab(payload);
      updateBacktestTab(payload);
      updateRiskTab(payload);
      updateSystemTab(payload);
      if (activeDrawerKind) openInsightDrawer(activeDrawerKind);
      if (options.renderChart !== false) scheduleChartRender(payload, { showLoading: options.showChartLoading === true });
    }

    function preserveChartPayload(payload, previousPayload, chartInterval, cachedBars) {
      const previous = previousPayload || {};
      const previousFillCount = previous.recent_fills?.length || 0;
      const payloadFillCount = payload.recent_fills?.length || 0;
      const incomingHasIncompleteOrders = payload.trade_records_complete === false;
      const keepCompleteTradeRecords = previous.trade_records_complete === true && incomingHasIncompleteOrders;
      const incomingHasNewFill = payloadFillCount > previousFillCount;
      const orderRecordsNeedRefresh = incomingHasIncompleteOrders && (
        (previous.trade_records_complete !== true && !(previous.trade_records || []).length) ||
        incomingHasNewFill
      );
      const bars = cachedBars?.length
        ? cachedBars
        : payload.live_chart_bars?.length
          ? payload.live_chart_bars
          : previous.live_chart_bars || [];
      return {
        ...previous,
        ...payload,
        live_chart_interval: chartInterval || payload.live_chart_interval || previous.live_chart_interval,
        live_chart_interval_label: payload.live_chart_interval_label || previous.live_chart_interval_label,
        live_chart_source: bars.length ? (previous.live_chart_source || payload.live_chart_source) : payload.live_chart_source,
        live_chart_cache: previous.live_chart_cache || payload.live_chart_cache,
        live_chart_bars: bars,
        live_trade_markers: payload.live_trade_markers?.length ? payload.live_trade_markers : previous.live_trade_markers || [],
        order_markers: payload.order_markers?.length ? payload.order_markers : previous.order_markers || [],
        position_activation_markers: payload.position_activation_markers?.length ? payload.position_activation_markers : previous.position_activation_markers || [],
        live_ai_veto_markers: payload.live_ai_veto_markers?.length ? payload.live_ai_veto_markers : previous.live_ai_veto_markers || [],
        trade_records: keepCompleteTradeRecords ? previous.trade_records || [] : payload.trade_records || previous.trade_records || [],
        recent_fills: keepCompleteTradeRecords ? previous.recent_fills || [] : payload.recent_fills || previous.recent_fills || [],
        order_lifecycle_events: keepCompleteTradeRecords ? previous.order_lifecycle_events || [] : payload.order_lifecycle_events || previous.order_lifecycle_events || [],
        trade_records_complete: keepCompleteTradeRecords ? true : payload.trade_records_complete === true,
        order_records_refresh_needed: orderRecordsNeedRefresh,
        order_records_meta: keepCompleteTradeRecords ? previous.order_records_meta || {} : payload.order_records_meta || previous.order_records_meta || {},
      };
    }

    function scheduleChartRender(payload, options = {}) {
      if (!payload) return;
      const renderSeq = ++chartRenderSeq;
      if (options.showLoading) setChartLoading(false, options.loadingText || "正在绘制 K 线");
      window.setTimeout(() => {
        window.requestAnimationFrame(() => {
          if (renderSeq !== chartRenderSeq) return;
          try {
            redrawCharts(payload);
            if (activeTab === "trading") setChartLoading(false);
          } catch (err) {
            console.error(err);
            if (activeTab === "trading") setChartLoading(false, "图表渲染失败，其他数据不受影响");
          }
        });
      }, 0);
    }

    function redrawCharts(payload) {
      if (!payload) return;
      if (activeTab === "trading") {
        const c = context(payload);
        drawCandlestickChart(document.getElementById("tradeChart"), c.bars, {
          markers: c.markers,
          vetoes: c.vetoes,
          orderMarkers: c.orderMarkers,
          openOrders: c.openOrders,
          riskLines: activeRiskLines(c).numeric,
          quoteAsset: c.quoteAsset,
          interval: payload.live_chart_interval || selectedChartInterval,
          maConfig: chartMaConfig(payload),
          maxVisibleBars: 80,
          hover: chartHover
        });
        drawProfitCurve(document.getElementById("profitCurveChart"), c.profitCurve, c.quoteAsset);
      }
      if (activeTab === "backtest") {
        const equity = payload.backtest_equity_curve || [];
        drawLineChart(document.getElementById("btEquityChart"), equity.map((p) => ({ time: p.time || p.timestamp, value: p.equity || p.total_equity || p.net_value })), "#1f3f6d", "rgba(31, 63, 109, 0.08)", "权益");
        drawLineChart(document.getElementById("btDrawdownChart"), equity.map((p) => ({ time: p.time || p.timestamp, value: p.drawdown_pct || p.drawdown || 0 })), "#b4232a", "rgba(180, 35, 42, 0.06)", "回撤");
      }
    }

    function setupCanvas(canvas) {
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      const width = Math.max(1, Math.floor(rect.width));
      const height = Math.max(1, Math.floor(rect.height));
      if (width <= 1 || height <= 1) return null;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, width, height);
      return { ctx, width, height };
    }

    function drawEmptyChart(canvas, text) {
      const setup = setupCanvas(canvas);
      if (!setup) return;
      const { ctx, width, height } = setup;
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      ctx.fillStyle = "#66758a";
      ctx.font = "700 12px Hiragino Sans, PingFang SC, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(text, width / 2, height / 2);
    }

    function drawCandlestickChart(canvas, bars, options) {
      const setup = setupCanvas(canvas);
      if (!setup) return;
      const { ctx, width, height } = setup;
      const maxVisibleBars = Number(options.maxVisibleBars || 80);
      const data = (bars || []).filter((b) => Number.isFinite(Number(b.close))).slice(-maxVisibleBars);
      if (!data.length) {
        drawEmptyChart(canvas, "等待主周期 K 线数据");
        return;
      }
      const fastMa = movingAverageSeries(data, options.maConfig?.fastWindow || 0);
      const slowMa = movingAverageSeries(data, options.maConfig?.slowWindow || 0);

      const pad = { left: 52, right: 76, top: 22, bottom: 30 };
      const volumeHeight = Math.max(50, Math.floor(height * 0.16));
      const volumeTop = height - pad.bottom - volumeHeight;
      const priceBottom = volumeTop - 16;
      const plotW = width - pad.left - pad.right;
      const priceH = priceBottom - pad.top;
      const values = [];
      data.forEach((b) => {
        values.push(asNumber(b.high || b.close), asNumber(b.low || b.close), asNumber(b.open || b.close), asNumber(b.close));
      });
      fastMa.forEach((v) => { if (Number.isFinite(v)) values.push(v); });
      slowMa.forEach((v) => { if (Number.isFinite(v)) values.push(v); });
      Object.values((options && options.riskLines) || {}).forEach((v) => { if (Number.isFinite(v)) values.push(v); });
      let min = Math.min(...values);
      let max = Math.max(...values);
      if (min === max) { min *= 0.995; max *= 1.005; }
      const span = max - min || 1;
      min -= span * 0.08;
      max += span * 0.08;
      const y = (value) => pad.top + (max - value) / (max - min) * priceH;
      const x = (idx) => pad.left + (idx + 0.5) / data.length * plotW;
      const barSlot = plotW / data.length;
      const candleW = Math.max(5, Math.min(15, barSlot * 0.72));
      const maxVol = Math.max(...data.map((b) => barVolumeValue(b)), 1);

      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = "#e8eef5";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const gy = pad.top + (priceH / 4) * i;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(width - pad.right, gy);
        ctx.stroke();
      }
      for (let i = 0; i <= 4; i += 1) {
        const gx = pad.left + (plotW / 4) * i;
        ctx.beginPath();
        ctx.moveTo(gx, pad.top);
        ctx.lineTo(gx, height - pad.bottom);
        ctx.stroke();
      }
      ctx.strokeStyle = "#cbd6e2";
      ctx.beginPath();
      ctx.moveTo(pad.left, pad.top);
      ctx.lineTo(pad.left, height - pad.bottom);
      ctx.lineTo(width - pad.right, height - pad.bottom);
      ctx.stroke();

      ctx.fillStyle = "#f7f9fc";
      ctx.fillRect(pad.left, volumeTop, plotW, volumeHeight);
      data.forEach((b, idx) => {
        const open = asNumber(b.open || b.close);
        const close = asNumber(b.close);
        const high = asNumber(b.high || close);
        const low = asNumber(b.low || close);
        const cx = x(idx);
        const up = close >= open;
        const color = up ? "#15803d" : "#b4232a";
        const vol = barVolumeValue(b);
        const volH = Math.max(2, vol / maxVol * (volumeHeight - 12));
        ctx.fillStyle = up ? "rgba(21, 128, 61, 0.16)" : "rgba(180, 35, 42, 0.14)";
        ctx.fillRect(cx - candleW / 2, volumeTop + volumeHeight - volH, candleW, volH);
        ctx.strokeStyle = color;
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(cx, y(high));
        ctx.lineTo(cx, y(low));
        ctx.stroke();
        const bodyY = Math.min(y(open), y(close));
        const bodyH = Math.max(1.5, Math.abs(y(close) - y(open)));
        ctx.fillStyle = up ? "rgba(21, 128, 61, 0.72)" : "rgba(180, 35, 42, 0.72)";
        ctx.fillRect(cx - candleW / 2, bodyY, candleW, bodyH);
      });

      drawMaLine(ctx, fastMa, x, y, "#2563eb");
      drawMaLine(ctx, slowMa, x, y, "#f59e0b");
      drawMaLegend(ctx, {
        x: pad.left + 8,
        y: pad.top + 14,
        label: options.maConfig?.label || "均线",
        fastWindow: options.maConfig?.fastWindow || 0,
        slowWindow: options.maConfig?.slowWindow || 0,
        fastValue: fastMa[fastMa.length - 1],
        slowValue: slowMa[slowMa.length - 1],
        maxWidth: Math.max(180, (width - pad.left - pad.right) * 0.42),
      });

      drawRiskLine(ctx, options.riskLines?.stop_loss_price, y, pad.left, width - pad.right, "#b4232a", "止损");
      drawRiskLine(ctx, options.riskLines?.take_profit_price, y, pad.left, width - pad.right, "#15803d", "止盈");
      drawRiskLine(ctx, options.riskLines?.trailing_stop_price, y, pad.left, width - pad.right, "#c96a21", "跟踪");
      (options.openOrders || []).forEach((order) => {
        const side = String(order.side || "").toUpperCase();
        drawRiskLine(
          ctx,
          asNumber(order.limit_price, NaN),
          y,
          pad.left,
          width - pad.right,
          side === "BUY" ? "#1f6fbf" : "#7c3aed",
          side === "BUY" ? "挂买" : "挂卖"
        );
      });

      const indexByTime = buildTimeIndex(data);
      (options.markers || []).forEach((m) => drawTradeMarker(ctx, m, indexByTime, data.length, x, y));
      (options.orderMarkers || []).forEach((m) => drawOrderMarker(ctx, m, indexByTime, data.length, x, y));
      (options.vetoes || []).forEach((m) => drawVetoMarker(ctx, m, indexByTime, data.length, x, y));

      ctx.fillStyle = "#66758a";
      ctx.font = "11px SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "right";
      for (let i = 0; i <= 4; i += 1) {
        const value = max - (max - min) / 4 * i;
        ctx.fillText(fmtNumber(value, 4), width - 12, pad.top + priceH / 4 * i + 4);
      }
      ctx.textAlign = "center";
      const labelCount = Math.min(5, data.length);
      for (let i = 0; i < labelCount; i += 1) {
        const idx = Math.floor(i * (data.length - 1) / Math.max(1, labelCount - 1));
        ctx.fillText(fmtChartAxisTime(data[idx].time || data[idx].open_time, options.interval || "1m"), x(idx), height - 12);
      }
      const last = data[data.length - 1];
      const lastY = y(asNumber(last.close));
      ctx.strokeStyle = "rgba(31, 63, 109, 0.45)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(pad.left, lastY);
      ctx.lineTo(width - pad.right + 4, lastY);
      ctx.stroke();
      ctx.fillStyle = "#1f3f6d";
      ctx.beginPath();
      ctx.roundRect(width - pad.right + 8, lastY - 10, 50, 20, 4);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.textAlign = "center";
      ctx.fillText(fmtNumber(last.close, 3), width - pad.right + 33, lastY + 4);

      drawChartHover(ctx, {
        hover: options.hover,
        data,
        x,
        y,
        pad,
        plotW,
        priceH,
        volumeTop,
        volumeHeight,
        width,
        height,
        quoteAsset: options.quoteAsset || "",
        fastMa,
        slowMa,
        maConfig: options.maConfig || {}
      });
    }

    function movingAverageSeries(data, windowSize) {
      const size = Number(windowSize);
      if (!Number.isFinite(size) || size <= 0) return data.map(() => NaN);
      const result = [];
      let sum = 0;
      data.forEach((bar, index) => {
        sum += asNumber(bar.close, 0);
        if (index >= size) sum -= asNumber(data[index - size].close, 0);
        result.push(index >= size - 1 ? sum / size : NaN);
      });
      return result;
    }

    function drawMaLine(ctx, series, x, y, color) {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      let started = false;
      series.forEach((value, index) => {
        if (!Number.isFinite(value)) return;
        const px = x(index);
        const py = y(value);
        if (!started) {
          ctx.moveTo(px, py);
          started = true;
        } else {
          ctx.lineTo(px, py);
        }
      });
      if (started) ctx.stroke();
      ctx.restore();
    }

    function drawMaLegend(ctx, item) {
      if (!item.fastWindow || !item.slowWindow) return;
      const fastText = `MA${item.fastWindow} ${fmtNumber(item.fastValue, 4)}`;
      const slowText = `MA${item.slowWindow} ${fmtNumber(item.slowValue, 4)}`;
      const state = Number.isFinite(item.fastValue) && Number.isFinite(item.slowValue)
        ? (item.fastValue >= item.slowValue ? "快线在上" : "快线在下")
        : "均线计算中";
      const segments = [
        { text: item.label, color: "#536176" },
        { text: fastText, color: "#2563eb" },
        { text: slowText, color: "#f59e0b" },
        { text: state, color: state === "快线在上" ? "#15803d" : state === "快线在下" ? "#b4232a" : "#66758a" },
      ];
      ctx.save();
      ctx.font = "560 9.5px Hiragino Sans, PingFang SC, sans-serif";
      const paddingX = 8;
      const rowH = 14;
      const measured = segments.map((segment) => ({
        ...segment,
        width: Math.ceil(ctx.measureText(segment.text).width),
      }));
      const contentW = Math.max(...measured.map((segment) => segment.width));
      const boxW = contentW + paddingX * 2;
      const boxH = measured.length * rowH + 8;
      ctx.fillStyle = "rgba(255, 255, 255, 0.88)";
      ctx.strokeStyle = "rgba(150, 164, 184, 0.48)";
      ctx.beginPath();
      ctx.roundRect(item.x - paddingX, item.y - 11, boxW, boxH, 6);
      ctx.fill();
      ctx.stroke();
      ctx.textAlign = "left";
      measured.forEach((segment, index) => {
        ctx.fillStyle = segment.color;
        ctx.fillText(segment.text, item.x, item.y + 3 + index * rowH);
      });
      ctx.restore();
    }

    function barVolumeValue(bar) {
      const volume = asNumber(bar.volume, NaN);
      if (Number.isFinite(volume) && volume > 0) return volume;
      return asNumber(bar.sample_count, 1);
    }

    function drawChartHover(ctx, options) {
      const hover = options.hover || {};
      if (!hover.active || !options.data.length) return;
      const chartRight = options.width - options.pad.right;
      const chartBottom = options.height - options.pad.bottom;
      if (hover.x < options.pad.left || hover.x > chartRight || hover.y < options.pad.top || hover.y > chartBottom) return;

      const slot = options.plotW / options.data.length;
      const idx = Math.max(0, Math.min(options.data.length - 1, Math.floor((hover.x - options.pad.left) / slot)));
      const bar = options.data[idx];
      const cx = options.x(idx);
      const close = asNumber(bar.close);
      const cy = hover.y <= options.volumeTop ? options.y(close) : hover.y;
      const volume = asNumber(bar.volume, NaN);
      const samples = asNumber(bar.sample_count, NaN);
      const volumeText = Number.isFinite(volume) && volume > 0
        ? `量 ${fmtNumber(volume, 2)}`
        : `样本 ${Number.isFinite(samples) ? fmtNumber(samples, 0) : "--"}`;
      const fastValue = options.fastMa ? options.fastMa[idx] : NaN;
      const slowValue = options.slowMa ? options.slowMa[idx] : NaN;
      const maLabel = options.maConfig?.label || "均线";
      const lines = [
        fmtTime(bar.time || bar.open_time || bar.close_time),
        `开 ${fmtNumber(bar.open, 4)}  高 ${fmtNumber(bar.high, 4)}`,
        `低 ${fmtNumber(bar.low, 4)}  收 ${fmtNumber(bar.close, 4)}`,
        volumeText,
        `${maLabel}`,
        `快 ${fmtNumber(fastValue, 4)}  慢 ${fmtNumber(slowValue, 4)}`
      ];

      ctx.save();
      ctx.strokeStyle = "rgba(31, 63, 109, 0.42)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(cx, options.pad.top);
      ctx.lineTo(cx, chartBottom);
      ctx.moveTo(options.pad.left, cy);
      ctx.lineTo(chartRight, cy);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.fillStyle = "#1f3f6d";
      ctx.beginPath();
      ctx.roundRect(chartRight + 8, cy - 10, 50, 20, 4);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "700 10px SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "center";
      ctx.fillText(fmtNumber(close, 3), chartRight + 33, cy + 4);

      ctx.font = "700 11px SFMono-Regular, Menlo, monospace";
      const textW = Math.max(...lines.map((line) => ctx.measureText(line).width));
      const boxW = Math.min(Math.max(196, Math.ceil(textW + 24)), Math.max(196, chartRight - options.pad.left - 12));
      const boxH = 122;
      const boxX = cx + boxW + 16 > chartRight ? cx - boxW - 12 : cx + 12;
      const boxY = Math.max(options.pad.top + 6, Math.min(chartBottom - boxH - 6, hover.y - boxH / 2));
      ctx.fillStyle = "rgba(255, 255, 255, 0.96)";
      ctx.strokeStyle = "rgba(150, 164, 184, 0.65)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(boxX, boxY, boxW, boxH, 7);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#172033";
      ctx.textAlign = "left";
      lines.forEach((line, i) => {
        ctx.fillStyle = i === 0 ? "#536176" : "#172033";
        ctx.fillText(line, boxX + 10, boxY + 18 + i * 17);
      });
      ctx.restore();
    }

    function buildTimeIndex(data) {
      const index = new Map();
      data.forEach((b, i) => {
        const time = new Date(b.time || b.open_time || b.close_time || "").getTime();
        if (Number.isFinite(time)) index.set(time, i);
      });
      return { index, times: Array.from(index.keys()).sort((a, b) => a - b) };
    }

    function nearestIndex(timeIndex, timeValue, fallbackLength) {
      const t = new Date(timeValue || "").getTime();
      if (!Number.isFinite(t) || !timeIndex.times.length) return fallbackLength - 1;
      let best = 0;
      let bestDiff = Math.abs(timeIndex.times[0] - t);
      for (let i = 1; i < timeIndex.times.length; i += 1) {
        const diff = Math.abs(timeIndex.times[i] - t);
        if (diff < bestDiff) { best = i; bestDiff = diff; }
      }
      return timeIndex.index.get(timeIndex.times[best]) ?? fallbackLength - 1;
    }

    function drawTradeMarker(ctx, marker, timeIndex, length, x, y) {
      const side = String(marker.side || marker.action || "").toUpperCase();
      const trigger = String(marker.trigger || "");
      const price = asNumber(marker.price, NaN);
      if (!Number.isFinite(price)) return;
      const idx = nearestIndex(timeIndex, marker.time || marker.timestamp || marker.timestamp_ms, length);
      const cx = x(idx);
      const cy = y(price);
      const isBuy = side === "BUY";
      ctx.fillStyle = trigger.startsWith("grid_") ? "#c96a21" : isBuy ? "#15803d" : "#b4232a";
      ctx.beginPath();
      if (isBuy) {
        ctx.moveTo(cx, cy - 8);
        ctx.lineTo(cx - 5, cy + 4);
        ctx.lineTo(cx + 5, cy + 4);
      } else {
        ctx.moveTo(cx, cy + 8);
        ctx.lineTo(cx - 5, cy - 4);
        ctx.lineTo(cx + 5, cy - 4);
      }
      ctx.closePath();
      ctx.fill();
      if (trigger.startsWith("grid_")) {
        ctx.fillStyle = "#c96a21";
        ctx.font = "700 9px Hiragino Sans, PingFang SC, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText(trigger === "grid_buyback" ? "回补" : "网格", cx, cy + (isBuy ? 15 : -11));
      }
    }

    function drawVetoMarker(ctx, marker, timeIndex, length, x, y) {
      const price = asNumber(marker.price, NaN);
      const idx = nearestIndex(timeIndex, marker.time || marker.timestamp || marker.timestamp_ms, length);
      const cx = x(idx);
      const cy = Number.isFinite(price) ? y(price) : 42;
      ctx.strokeStyle = "#c96a21";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, 6, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(cx - 3, cy - 3);
      ctx.lineTo(cx + 3, cy + 3);
      ctx.moveTo(cx + 3, cy - 3);
      ctx.lineTo(cx - 3, cy + 3);
      ctx.stroke();
    }

    function drawOrderMarker(ctx, marker, timeIndex, length, x, y) {
      const status = String(marker.status || "").toUpperCase();
      const price = asNumber(marker.price, NaN);
      if (!Number.isFinite(price)) return;
      const idx = nearestIndex(timeIndex, marker.time || marker.timestamp || marker.timestamp_ms, length);
      const cx = x(idx);
      const cy = y(price);
      const side = String(marker.side || "").toUpperCase();
      const color = status === "FILLED"
        ? (side === "BUY" ? "#15803d" : "#b4232a")
        : status === "OPEN"
          ? "#1f6fbf"
          : "#7c8798";
      ctx.save();
      ctx.strokeStyle = color;
      ctx.fillStyle = status === "FILLED" ? color : "#fff";
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      if (status === "CANCELED" || status === "EXPIRED" || status === "REJECTED") {
        ctx.rect(cx - 4, cy - 4, 8, 8);
      } else {
        ctx.arc(cx, cy, 4.5, 0, Math.PI * 2);
      }
      ctx.fill();
      ctx.stroke();
      ctx.restore();
    }

    function drawRiskLine(ctx, value, y, x1, x2, color, label) {
      if (!Number.isFinite(value)) return;
      const yy = y(value);
      ctx.save();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.beginPath();
      ctx.moveTo(x1, yy);
      ctx.lineTo(x2, yy);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.beginPath();
      ctx.roundRect(x2 + 6, yy - 9, 30, 18, 4);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "700 10px Hiragino Sans, PingFang SC, sans-serif";
      ctx.textAlign = "center";
      ctx.fillText(label, x2 + 21, yy + 4);
      ctx.restore();
    }

    function drawLineChart(canvas, points, color, fillColor, label) {
      const setup = setupCanvas(canvas);
      if (!setup) return;
      const { ctx, width, height } = setup;
      const data = (points || []).filter((p) => Number.isFinite(Number(p.value)));
      if (!data.length) {
        drawEmptyChart(canvas, `暂无${label || "图表"}数据`);
        return;
      }
      const pad = { left: 52, right: 22, top: 20, bottom: 30 };
      const plotW = width - pad.left - pad.right;
      const plotH = height - pad.top - pad.bottom;
      let min = Math.min(...data.map((p) => Number(p.value)));
      let max = Math.max(...data.map((p) => Number(p.value)));
      if (min === max) { min -= 1; max += 1; }
      const y = (v) => pad.top + (max - v) / (max - min) * plotH;
      const x = (i) => pad.left + (i / Math.max(1, data.length - 1)) * plotW;

      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, width, height);
      ctx.strokeStyle = "#e5edf6";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 4; i += 1) {
        const gy = pad.top + plotH / 4 * i;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(width - pad.right, gy);
        ctx.stroke();
      }

      ctx.beginPath();
      data.forEach((p, i) => {
        const px = x(i);
        const py = y(Number(p.value));
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.lineTo(x(data.length - 1), height - pad.bottom);
      ctx.lineTo(x(0), height - pad.bottom);
      ctx.closePath();
      ctx.fillStyle = fillColor;
      ctx.fill();

      ctx.beginPath();
      data.forEach((p, i) => {
        const px = x(i);
        const py = y(Number(p.value));
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.stroke();

      ctx.fillStyle = "#64748b";
      ctx.font = "11px SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "right";
      for (let i = 0; i <= 4; i += 1) {
        const value = max - (max - min) / 4 * i;
        ctx.fillText(fmtNumber(value, 2), pad.left - 8, pad.top + plotH / 4 * i + 4);
      }
    }

    function drawProfitCurve(canvas, points, quoteAsset) {
      const setup = setupCanvas(canvas);
      if (!setup) return;
      const { ctx, width, height } = setup;
      const data = (points || []).map((p) => ({
        ...p,
        realized_pnl: asNumber(p.realized_pnl, 0),
      }));
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, width, height);
      if (!data.length) {
        ctx.fillStyle = "#6b7a90";
        ctx.font = "11px Hiragino Sans, PingFang SC, sans-serif";
        ctx.textAlign = "center";
        ctx.fillText("暂无利润曲线数据", width / 2, height / 2);
        return;
      }
      const pad = { left: 54, right: 16, top: 10, bottom: 18 };
      const plotW = Math.max(1, width - pad.left - pad.right);
      const plotH = Math.max(1, height - pad.top - pad.bottom);
      let min = Math.min(0, ...data.map((p) => Number(p.realized_pnl)));
      let max = Math.max(0, ...data.map((p) => Number(p.realized_pnl)));
      if (min === max) { min -= 1; max += 1; }
      const x = (i) => pad.left + (i / Math.max(1, data.length - 1)) * plotW;
      const y = (v) => pad.top + (max - v) / (max - min) * plotH;
      const zeroY = y(0);

      ctx.strokeStyle = "#edf2f8";
      ctx.lineWidth = 1;
      for (let i = 0; i <= 2; i += 1) {
        const gy = pad.top + (plotH / 2) * i;
        ctx.beginPath();
        ctx.moveTo(pad.left, gy);
        ctx.lineTo(width - pad.right, gy);
        ctx.stroke();
      }
      ctx.strokeStyle = "rgba(31, 63, 109, 0.24)";
      ctx.beginPath();
      ctx.moveTo(pad.left, zeroY);
      ctx.lineTo(width - pad.right, zeroY);
      ctx.stroke();

      const lastValue = Number(data[data.length - 1].realized_pnl);
      const positive = lastValue >= 0;
      const lineColor = positive ? "#14854f" : "#b4232a";
      const fill = positive ? "rgba(20, 133, 79, 0.08)" : "rgba(180, 35, 42, 0.07)";
      ctx.beginPath();
      data.forEach((p, i) => {
        const px = x(i);
        const py = y(Number(p.realized_pnl));
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.lineTo(x(data.length - 1), zeroY);
      ctx.lineTo(x(0), zeroY);
      ctx.closePath();
      ctx.fillStyle = fill;
      ctx.fill();

      ctx.beginPath();
      data.forEach((p, i) => {
        const px = x(i);
        const py = y(Number(p.realized_pnl));
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 1.7;
      ctx.stroke();

      ctx.fillStyle = "#617089";
      ctx.font = "10px SFMono-Regular, Menlo, monospace";
      ctx.textAlign = "right";
      ctx.fillText(fmtNumber(max, 2), pad.left - 8, pad.top + 4);
      ctx.fillText(fmtNumber(0, 2), pad.left - 8, zeroY + 4);
      ctx.fillText(fmtNumber(min, 2), pad.left - 8, height - pad.bottom);
      ctx.textAlign = "left";
      ctx.fillText(`已实现 ${fmtCurrency(lastValue, quoteAsset || "")}`, pad.left, 12);
    }

    async function loadData(chartInterval, requestSeq, includeChart = true) {
      const params = new URLSearchParams();
      if (chartInterval) params.set("chart_interval", chartInterval);
      if (!includeChart) params.set("include_chart", "false");
      const response = await fetch(`/api/dashboard?${params.toString()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      payload.requested_chart_interval = chartInterval;
      payload.request_seq = requestSeq;
      return payload;
    }

    async function loadChartData(chartInterval, requestSeq) {
      const params = new URLSearchParams();
      if (chartInterval) params.set("chart_interval", chartInterval);
      const response = await fetch(`/api/dashboard/chart?${params.toString()}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      payload.requested_chart_interval = chartInterval;
      payload.request_seq = requestSeq;
      return payload;
    }

    async function loadOrderRecords(requestSeq) {
      const status = (fillFilter || "all");
      const params = new URLSearchParams();
      if (status && status !== "all") params.set("status", status);
      params.set("limit", "1000");
      const query = params.toString();
      const response = await fetch(`/api/dashboard/orders${query ? `?${query}` : ""}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      payload.request_seq = requestSeq;
      return payload;
    }

    async function refreshDecisionDrawer() {
      if (activeDrawerKind !== "decision") return;
      const requestSeq = ++drawerRequestSeq;
      try {
        const response = await fetch("/api/dashboard/decision-drawer", { cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const drawerPayload = await response.json();
        if (requestSeq !== drawerRequestSeq || activeDrawerKind !== "decision" || !lastPayloadSnapshot) return;
        const mergedPayload = { ...lastPayloadSnapshot, ...drawerPayload };
        lastPayloadSnapshot = mergedPayload;
        els.insightDrawerBody.innerHTML = renderDecisionDrawer(mergedPayload);
      } catch (err) {
        console.error(err);
      }
    }

    async function refreshOrderRecords(force = false) {
      if (!lastPayloadSnapshot) return;
      if (orderRecordsInFlight) return;
      const now = Date.now();
      if (!force && now - lastOrderRecordsLoadedAt < 15000) return;
      const requestSeq = ++orderRequestSeq;
      orderRecordsInFlight = true;
      try {
        const orderPayload = await loadOrderRecords(requestSeq);
        if (requestSeq !== orderRequestSeq || !lastPayloadSnapshot) return;
        lastOrderRecordsLoadedAt = Date.now();
        lastPayloadSnapshot = { ...lastPayloadSnapshot, ...orderPayload };
        saveSnapshotCache(lastPayloadSnapshot);
        const c = context(lastPayloadSnapshot);
        renderFills(c.tradeRecords, c.quoteAsset);
      } catch (err) {
        console.error(err);
      } finally {
        if (requestSeq === orderRequestSeq) orderRecordsInFlight = false;
      }
    }

    async function tick(options = {}) {
      const force = options.force === true;
      if (tickInFlight && !force) return;
      tickInFlight = true;
      const requestSeq = ++dashboardRequestSeq;
      const chartInterval = selectedChartInterval;
      const cachedBars = chartBarsCache[chartInterval] || [];
      const previousPayload = lastPayloadSnapshot;
      if (!cachedBars.length) setChartLoading(false, `后台读取 ${chartInterval} K 线`);
      window.setTimeout(() => {
        if (requestSeq === dashboardRequestSeq && els.chartLoading?.classList.contains("active")) {
          setChartLoading(false);
        }
      }, 8000);
      try {
        const payload = await loadData(chartInterval, requestSeq, false);
        if (requestSeq !== dashboardRequestSeq || chartInterval !== selectedChartInterval) return;
        const hydratedPayload = preserveChartPayload(payload, previousPayload, chartInterval, cachedBars);
        updateDom(hydratedPayload, { renderChart: false });
        if (hydratedPayload.order_records_refresh_needed) {
          refreshOrderRecords();
        }
        const needsChartLoad = !cachedBars.length || chartRenderedInterval !== chartInterval || force || (Date.now() - chartLastPayloadMs) > 60_000;
        if (cachedBars.length && !needsChartLoad) {
          scheduleChartRender(hydratedPayload, { showLoading: false });
        }
        if (needsChartLoad) {
          try {
            const chartPayload = await loadChartData(chartInterval, requestSeq);
            if (requestSeq !== dashboardRequestSeq || chartInterval !== selectedChartInterval) return;
            const mergedPayload = { ...lastPayloadSnapshot, ...chartPayload };
            chartRenderedInterval = chartInterval;
            chartLastPayloadMs = Date.now();
            updateDom(mergedPayload, { renderChart: true, showChartLoading: false });
          } catch (chartErr) {
            if (requestSeq !== dashboardRequestSeq) return;
            console.error(chartErr);
            setChartLoading(false, "图表读取失败，文字数据已更新");
          }
        } else {
          if (requestSeq === dashboardRequestSeq && chartInterval === selectedChartInterval) {
            setChartLoading(false, "");
          }
        }
      } catch (err) {
        if (requestSeq !== dashboardRequestSeq) return;
        console.error(err);
        els.topMode.textContent = "数据读取失败";
        setChartLoading(false, "图表读取失败，其他数据保留");
      } finally {
        if (requestSeq === dashboardRequestSeq) tickInFlight = false;
      }
    }

    function wireTabs() {
      document.querySelectorAll("[data-tab]").forEach((btn) => {
        btn.addEventListener("click", () => activateTab(btn.dataset.tab));
      });
      document.querySelectorAll("[data-drawer]").forEach((btn) => {
        btn.addEventListener("click", () => openInsightDrawer(btn.dataset.drawer));
      });
      els.chartIntervalSelect.addEventListener("change", () => {
        selectedChartInterval = els.chartIntervalSelect.value || "1m";
        window.localStorage.setItem("boti.chartInterval", selectedChartInterval);
        dashboardRequestSeq += 1;
        if (chartBarsCache[selectedChartInterval]?.length && lastPayloadSnapshot) {
          scheduleChartRender({
            ...lastPayloadSnapshot,
            requested_chart_interval: selectedChartInterval,
            live_chart_interval: selectedChartInterval,
            live_chart_bars: chartBarsCache[selectedChartInterval],
          }, { showLoading: false });
        }
        tick({ force: true });
      });
      els.fillPageSize.addEventListener("change", () => {
        fillPageSize = Number(els.fillPageSize.value) || 50;
        fillPage = 0;
        const c = context(lastPayloadSnapshot || {});
        renderFills(c.tradeRecords, c.quoteAsset);
      });
      els.fillFilter.addEventListener("change", () => {
        fillFilter = els.fillFilter.value || "all";
        window.localStorage.setItem("boti.fillFilter", fillFilter);
        fillPage = 0;
        const c = context(lastPayloadSnapshot || {});
        renderFills(c.tradeRecords, c.quoteAsset);
        refreshOrderRecords(true);
      });
      els.fillPrev.addEventListener("click", () => {
        fillPage = Math.max(0, fillPage - 1);
        const c = context(lastPayloadSnapshot || {});
        renderFills(c.tradeRecords, c.quoteAsset);
      });
      els.fillNext.addEventListener("click", () => {
        fillPage += 1;
        const c = context(lastPayloadSnapshot || {});
        renderFills(c.tradeRecords, c.quoteAsset);
      });
      els.insightDrawerClose.addEventListener("click", closeInsightDrawer);
      els.insightDrawerBackdrop.addEventListener("click", closeInsightDrawer);
      const tradeChart = document.getElementById("tradeChart");
      if (tradeChart) {
        tradeChart.addEventListener("mousemove", (event) => {
          const rect = tradeChart.getBoundingClientRect();
          chartHover = {
            active: true,
            x: event.clientX - rect.left,
            y: event.clientY - rect.top
          };
          scheduleChartRender(lastPayloadSnapshot, { showLoading: false });
        });
        tradeChart.addEventListener("mouseleave", () => {
          chartHover = { active: false, x: 0, y: 0 };
          scheduleChartRender(lastPayloadSnapshot, { showLoading: false });
        });
      }
      window.addEventListener("resize", () => scheduleChartRender(lastPayloadSnapshot, { showLoading: false }));
    }

    cacheEls();
    wireTabs();
    activateTab("trading");
    const cachedSnapshot = loadSnapshotCache();
    if (cachedSnapshot) {
      updateDom(cachedSnapshot, { renderChart: true, showChartLoading: false });
      els.topMode.textContent = "读取中";
    }
    if (!cachedSnapshot) {
      refreshOrderRecords(true);
    }
    tick();
    setInterval(tick, refreshMs);
  </script>
</body>
</html>
"""


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _load_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_recent_lines(path: Path, limit: int, *, chunk_size: int = 256 * 1024) -> List[str]:
    if not path.exists() or limit <= 0:
        return []
    with path.open("rb") as handle:
        handle.seek(0, 2)
        file_size = handle.tell()
        position = file_size
        chunks: List[bytes] = []
        newline_count = 0
        while position > 0 and newline_count <= limit:
            read_size = min(chunk_size, position)
            position -= read_size
            handle.seek(position)
            chunk = handle.read(read_size)
            chunks.append(chunk)
            newline_count += chunk.count(b"\n")
        data = b"".join(reversed(chunks)).decode("utf-8", errors="ignore")
    return data.splitlines()[-limit:]


def _read_history(path: Path, limit: int = 6000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    cached = _load_recent_cycles_cached(path, limit)
    return list(cached)


def _file_signature(path: Path) -> tuple[int, int]:
    if not path.exists():
        return (0, 0)
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def _order_records_cache_key(
    runtime_dir: Path,
    status: str,
    scan_lines: int,
    order_limit: int,
    order_offset: int,
    *,
    include_runtime_signature: bool = True,
) -> str:
    if not include_runtime_signature:
        return f"{runtime_dir}|{status}|scan={scan_lines}|limit={order_limit}|offset={order_offset}|no_signature"
    latest_path = runtime_dir / "latest_report.json"
    paper_path = runtime_dir / "paper_state.json"
    cycle_path = runtime_dir / "cycle_reports.jsonl"
    latest_mtime, latest_size = _file_signature(latest_path)
    paper_mtime, paper_size = _file_signature(paper_path)
    cycle_mtime, cycle_size = _file_signature(cycle_path)
    return (
        f"{runtime_dir}|{status}|scan={scan_lines}|limit={order_limit}|offset={order_offset}|latest={latest_mtime}:{latest_size}|"
        f"paper={paper_mtime}:{paper_size}|cycle={cycle_mtime}:{cycle_size}"
    )


def _load_order_records_cache(key: str) -> Dict[str, Any] | None:
    now = time.time()
    with _order_records_cache_lock:
        entry = _order_records_cache.get(key)
        if entry is None:
            return None
        if now - float(entry.get("cached_at", 0.0)) > ORDER_RECORD_CACHE_TTL_SECONDS:
            return None
        return entry.get("payload") if isinstance(entry.get("payload"), dict) else None


def _write_order_records_cache(key: str, payload: Dict[str, Any]) -> None:
    with _order_records_cache_lock:
        _order_records_cache[key] = {
            "cached_at": time.time(),
            "payload": payload,
        }


def _filter_records_by_status(records: List[Dict[str, Any]], status: str) -> List[Dict[str, Any]]:
    normalized = (status or "all").lower()
    if normalized == "all":
        return records
    if normalized == "open":
        allowed = {"OPEN", "NEW", "PARTIALLY_FILLED", "UNKNOWN"}
    elif normalized == "filled":
        allowed = {"FILLED", "PAPER_FILLED"}
    elif normalized == "canceled":
        allowed = {"CANCELED", "EXPIRED", "REJECTED"}
    else:
        allowed = {normalized.upper()}
    return [item for item in records if str(item.get("status", "")).upper() in allowed]


def _dedupe_order_lifecycle_events(events: List[Dict[str, Any]], limit: int | None = None) -> List[Dict[str, Any]]:
    terminal_statuses = {"FILLED", "PAPER_FILLED", "CANCELED", "EXPIRED", "REJECTED"}
    latest_non_terminal: Dict[str, Dict[str, Any]] = {}
    terminal_events: List[Dict[str, Any]] = []
    passthrough: List[Dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            continue
        client_order_id = str(event.get("client_order_id") or "")
        status = str(event.get("status") or "").upper()
        event_type = str(event.get("event_type") or "").upper()
        timestamp_ms = _coerce_int(event.get("timestamp_ms") or event.get("updated_at_ms") or event.get("created_at_ms"))
        if status in terminal_statuses or event_type in terminal_statuses:
            terminal_events.append(event)
            continue
        if not client_order_id:
            passthrough.append(event)
            continue
        key = client_order_id
        previous = latest_non_terminal.get(key)
        previous_ts = _coerce_int((previous or {}).get("timestamp_ms") or (previous or {}).get("updated_at_ms") or (previous or {}).get("created_at_ms"))
        if previous is None or timestamp_ms >= previous_ts:
            latest_non_terminal[key] = event
    merged = [*terminal_events, *latest_non_terminal.values(), *passthrough]
    merged = sorted(
        merged,
        key=lambda item: _coerce_int(item.get("timestamp_ms") or item.get("updated_at_ms") or item.get("created_at_ms")),
        reverse=True,
    )
    if limit is not None:
        return merged[:limit]
    return merged


def _apply_optional_limit_newest_first(items: List[Dict[str, Any]], limit: int | None) -> List[Dict[str, Any]]:
    newest_first = items[::-1]
    if limit is None:
        return newest_first
    if limit <= 0:
        return []
    return newest_first[:limit]


def _cycle_matches_markers(cycle: Dict[str, Any], markers: tuple[str, ...]) -> bool:
    if not isinstance(cycle, dict):
        return False
    for marker in markers:
        if marker == '"PAPER_FILLED"':
            for decision in cycle.get("decisions", []):
                execution = decision.get("execution_result", {}) if isinstance(decision, dict) else {}
                if isinstance(execution, dict) and str(execution.get("status")) == "PAPER_FILLED":
                    return True
            for event in cycle.get("order_lifecycle_events", []):
                if isinstance(event, dict) and str(event.get("status")) == "FILLED":
                    return True
            continue
        if marker in {"\"order_lifecycle_events\": [{", "\"order_lifecycle_events\":[{"}:
            if isinstance(cycle.get("order_lifecycle_events"), list) and cycle.get("order_lifecycle_events"):
                return True
            continue
        if marker == '\"decision_ledger\"':
            if isinstance(cycle.get("decision_ledger"), list) and cycle.get("decision_ledger"):
                return True
            continue
        # fallback: keep old fast-path behavior for any unknown marker.
        if marker:
            text = json.dumps(cycle, ensure_ascii=True)
            if marker in text:
                return True
    return False


def _cycle_has_fill_marker(cycle: Dict[str, Any]) -> bool:
    if not isinstance(cycle, dict):
        return False
    for decision in cycle.get("decisions", []):
        execution = decision.get("execution_result", {}) if isinstance(decision, dict) else {}
        if isinstance(execution, dict) and str(execution.get("status")) == "PAPER_FILLED":
            return True
    for event in cycle.get("order_lifecycle_events", []):
        if isinstance(event, dict) and str(event.get("status")) == "FILLED":
            return True
    return False


def _iter_matching_cycles_from_file(path: Path, markers: tuple[str, ...], scan_lines: int | None) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return iter(())
    cycles = _load_recent_cycles_cached(path, scan_lines)
    if not markers:
        return iter(cycles)

    def _generator() -> Iterable[Dict[str, Any]]:
        for cycle in cycles:
            if _cycle_matches_markers(cycle, markers):
                yield cycle

    return _generator()


def _extract_recent_fills(history: List[Dict[str, Any]], limit: int | None = 300) -> List[Dict[str, Any]]:
    fills: List[Dict[str, Any]] = []
    for cycle in history:
        for decision in cycle.get("decisions", []):
            execution = decision.get("execution_result", {})
            if execution.get("status") == "PAPER_FILLED":
                fills.append(execution)
        for event in cycle.get("order_lifecycle_events", []):
            if event.get("status") == "FILLED":
                fills.append(
                    {
                        "symbol": event.get("symbol"),
                        "side": event.get("side"),
                        "status": "FILLED",
                        "quantity": event.get("filled_quantity") or event.get("quantity"),
                        "fill_price": event.get("fill_price") or event.get("limit_price"),
                        "fee": event.get("fee"),
                        "timestamp_ms": event.get("timestamp_ms"),
                        "trigger": event.get("trigger"),
                        "client_order_id": event.get("client_order_id"),
                    }
                )
    return _apply_optional_limit_newest_first(fills, limit)


def _dedupe_fills(fills: List[Dict[str, Any]], limit: int | None = 300) -> List[Dict[str, Any]]:
    seen: set[str] = set()
    unique: List[Dict[str, Any]] = []
    for fill in fills:
        if not isinstance(fill, dict):
            continue
        key = "|".join(
            [
                str(fill.get("client_order_id") or ""),
                str(fill.get("timestamp_ms") or fill.get("timestamp") or ""),
                str(fill.get("side") or fill.get("action") or ""),
                str(fill.get("quantity") or fill.get("filled_quantity") or ""),
                str(fill.get("fill_price") or fill.get("price") or fill.get("limit_price") or ""),
            ]
        )
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(fill)
        normalized.setdefault("status", "PAPER_FILLED")
        if "fill_price" not in normalized and "price" in normalized:
            normalized["fill_price"] = normalized.get("price")
        unique.append(normalized)
    return _apply_optional_limit_newest_first(unique, limit)


def _extract_recent_fills_from_file(path: Path, limit: int | None = 300, scan_lines: int | None = 8000) -> List[Dict[str, Any]]:
    matching_cycles = list(_iter_matching_cycles_from_file(path, ('"PAPER_FILLED"', '"order_lifecycle_events": [{', '"order_lifecycle_events":[{'), scan_lines))
    return _extract_recent_fills(matching_cycles, limit=limit)


def _extract_order_lifecycle_events(
    history: List[Dict[str, Any]],
    latest_report: Dict[str, Any],
    limit: int = 200,
) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for cycle in history:
        for event in cycle.get("order_lifecycle_events", []):
            if isinstance(event, dict):
                events.append(event)
    if not events:
        for event in latest_report.get("order_lifecycle_events", []):
            if isinstance(event, dict):
                events.append(event)
    return events[-limit:][::-1]


def _extract_order_lifecycle_events_from_file(path: Path, limit: int | None = 200, scan_lines: int | None = 8000) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for cycle in _iter_matching_cycles_from_file(
        path,
        ('"order_lifecycle_events": [{', '"order_lifecycle_events":[{', '"decision_ledger":['),
        scan_lines,
    ):
        for event in cycle.get("order_lifecycle_events", []):
            if isinstance(event, dict):
                events.append(event)
    return _apply_optional_limit_newest_first(events, limit)


def _dashboard_fee_rate(default: float = 0.001) -> float:
    raw = os.environ.get("TRADING_FEE_RATE")
    if raw is None:
        env_path = Path(".env")
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                if key.strip() == "TRADING_FEE_RATE":
                    raw = value.strip().strip('"').strip("'")
                    break
    fee_rate = _coerce_float(raw, default)
    return max(0.0, fee_rate)


def _dashboard_runtime_config() -> Dict[str, Any]:
    try:
        settings = load_settings()
    except Exception:
        return {}
    return {
        "grid_buyback_step_pct": settings.grid_buyback_step_pct,
        "grid_buyback_tiers": settings.grid_buyback_tiers,
        "grid_max_daily_trades": settings.grid_max_daily_trades,
        "kline_interval": settings.kline_interval,
        "fast_window": settings.fast_window,
        "slow_window": settings.slow_window,
        "mtf_entry_interval": settings.mtf_entry_interval,
        "mtf_entry_fast_window": settings.mtf_entry_fast_window,
        "mtf_entry_slow_window": settings.mtf_entry_slow_window,
        "mtf_trend_interval": settings.mtf_trend_interval,
        "mtf_trend_fast_window": settings.mtf_trend_fast_window,
        "mtf_trend_slow_window": settings.mtf_trend_slow_window,
        "min_net_edge_pct": settings.min_net_edge_pct,
        "required_roundtrip_edge_pct": settings.min_net_edge_pct + settings.trading_fee_rate * 2.0,
        "buyback_cooldown_bars": settings.buyback_cooldown_bars,
        "exit_stop_loss_fraction": settings.exit_stop_loss_fraction,
        "exit_emergency_stop_fraction": settings.exit_emergency_stop_fraction,
        "ai_can_cancel_buyback": settings.ai_can_cancel_buyback,
        "order_max_open_per_symbol": settings.order_max_open_per_symbol,
        "order_max_open_per_side": settings.order_max_open_per_side,
        "order_reprice_tolerance_pct": settings.order_reprice_tolerance_pct,
        "order_reprice_min_age_seconds": settings.order_reprice_min_age_seconds,
        "order_reprice_compare_mode": settings.order_reprice_compare_mode,
        "order_ladder_enabled": settings.order_ladder_enabled,
        "pair_market_making_enabled": settings.pair_market_making_enabled,
        "order_levels_per_side": settings.order_levels_per_side,
        "pair_spread_levels": settings.pair_spread_levels,
        "target_position_fraction": settings.target_position_fraction,
        "target_inventory_enabled": settings.target_inventory_enabled,
        "target_position_strong_down": settings.target_position_strong_down,
        "target_position_weak_down": settings.target_position_weak_down,
        "target_position_range": settings.target_position_range,
        "target_position_strong_up": settings.target_position_strong_up,
        "target_position_emergency": settings.target_position_emergency,
        "target_position_band_pct": settings.target_position_band_pct,
        "min_cash_reserve_fraction": settings.min_cash_reserve_fraction,
        "entry_ladder_tiers": settings.entry_ladder_tiers,
        "exit_ladder_tiers": settings.exit_ladder_tiers,
        "min_effective_order_notional": settings.min_effective_order_notional,
        "order_target_notional": settings.order_target_notional,
        "max_daily_turnover_fraction": settings.max_daily_turnover_fraction,
        "max_daily_turnover_hard_block": settings.max_daily_turnover_hard_block,
        "max_daily_realized_loss_pct": settings.max_daily_realized_loss_pct,
        "policy_engine_enabled": settings.policy_engine_enabled,
        "pair_lock_after_risk_exit_candles": settings.pair_lock_after_risk_exit_candles,
        "pair_lock_require_trend_stable": settings.pair_lock_require_trend_stable,
        "pair_lock_require_net_edge": settings.pair_lock_require_net_edge,
        "stoploss_guard_trade_limit": settings.stoploss_guard_trade_limit,
        "max_drawdown_guard_pct": settings.max_drawdown_guard_pct,
        "inventory_skew_enabled": settings.inventory_skew_enabled,
        "inventory_target_base_pct": settings.inventory_target_base_pct,
        "inventory_range_multiplier": settings.inventory_range_multiplier,
        "order_proposal_min_net_edge_pct": settings.order_proposal_min_net_edge_pct,
        "hanging_orders_enabled": settings.hanging_orders_enabled,
        "hanging_orders_cancel_pct": settings.hanging_orders_cancel_pct,
        "maker_fee_pct": settings.maker_fee_pct,
        "pair_edge_safety_buffer_pct": settings.pair_edge_safety_buffer_pct,
        "cooldown_candles_after_pair_complete": settings.cooldown_candles_after_pair_complete,
        "low_profit_pair_lookback": settings.low_profit_pair_lookback,
        "low_profit_pair_min_avg_net_edge_pct": settings.low_profit_pair_min_avg_net_edge_pct,
        "low_profit_pair_lock_candles": settings.low_profit_pair_lock_candles,
        "direction_engine_enabled": settings.direction_engine_enabled,
        "legacy_direct_order_fallback": settings.legacy_direct_order_fallback,
        "trend_follow_enabled": settings.trend_follow_enabled,
        "fair_value_method": settings.fair_value_method,
        "fair_value_lookback_bars": settings.fair_value_lookback_bars,
        "volatility_buffer_atr_multiplier": settings.volatility_buffer_atr_multiplier,
        "buy_zone_min_discount_pct": settings.buy_zone_min_discount_pct,
        "sell_zone_min_premium_pct": settings.sell_zone_min_premium_pct,
        "min_pair_net_edge_pct": settings.min_pair_net_edge_pct,
        "allow_risk_sell_below_sell_zone": settings.allow_risk_sell_below_sell_zone,
        "sell_cost_protection_enabled": settings.sell_cost_protection_enabled,
        "sell_cost_protection_buffer_pct": settings.sell_cost_protection_buffer_pct,
        "allow_below_cost_sell_for_rebalance": settings.allow_below_cost_sell_for_rebalance,
        "post_initial_release_buyback_required": settings.post_initial_release_buyback_required,
        "pair_counter_buyback_enabled": settings.pair_counter_buyback_enabled,
        "pair_counter_buyback_min_net_edge_pct": settings.pair_counter_buyback_min_net_edge_pct,
        "scenario_engine_enabled": settings.scenario_engine_enabled,
        "trend_probe_entry_fraction": settings.trend_probe_entry_fraction,
        "recovery_entry_fraction": settings.recovery_entry_fraction,
        "uptrend_expansion_min_periods": settings.uptrend_expansion_min_periods,
        "uptrend_exhaustion_gap_pct": settings.uptrend_exhaustion_gap_pct,
        "downtrend_buy_discount_multiplier": settings.downtrend_buy_discount_multiplier,
        "low_vol_atr_pct": settings.low_vol_atr_pct,
        "order_tier_merge_enabled": settings.order_tier_merge_enabled,
        "order_tier_merge_min_notional": settings.order_tier_merge_min_notional,
        "external_signal_enabled": settings.external_signal_enabled,
        "external_signal_refresh_seconds": settings.external_signal_refresh_seconds,
        "external_signal_stale_seconds": settings.external_signal_stale_seconds,
        "external_signal_timeout_seconds": settings.external_signal_timeout_seconds,
        "external_signal_local_weight": settings.external_signal_local_weight,
        "external_signal_external_weight": settings.external_signal_external_weight,
        "external_signal_sources": settings.external_signal_sources,
        "external_signal_min_sources": settings.external_signal_min_sources,
    }


def _base_asset_from_symbol(symbol: str, quote_asset: str) -> str:
    if symbol and quote_asset and symbol.upper().endswith(quote_asset.upper()):
        return symbol[: -len(quote_asset)] or "BASE"
    return "BASE"


def _order_record_timestamp(order: Dict[str, Any]) -> int:
    return _coerce_int(
        order.get("updated_at_ms"),
        _coerce_int(order.get("timestamp_ms"), _coerce_int(order.get("created_at_ms"))),
    )


def _build_fill_trade_record(fill: Dict[str, Any], quote_asset: str, fee_rate: float) -> Dict[str, Any]:
    symbol = str(fill.get("symbol") or "")
    quantity = _coerce_float(fill.get("quantity"))
    price = _coerce_float(fill.get("fill_price"))
    if price <= 0:
        price = _coerce_float(fill.get("price"), _coerce_float(fill.get("limit_price")))
    fee = _coerce_float(fill.get("fee"))
    if fee <= 0 and quantity > 0 and price > 0:
        fee = quantity * price * fee_rate
    status = fill.get("status") or "PAPER_FILLED"
    return {
        "record_type": "fill",
        "status": status,
        "event_type": fill.get("event_type") or "FILLED",
        "symbol": symbol,
        "side": fill.get("side") or fill.get("action") or "",
        "quantity": quantity,
        "price": price,
        "fill_price": price,
        "limit_price": _coerce_float(fill.get("limit_price"), price),
        "fee": fee,
        "estimated_fee": 0.0,
        "realized_pnl": _coerce_float(fill.get("realized_pnl")),
        "reserved_quote": 0.0,
        "reserved_base": 0.0,
        "reserved_asset": "",
        "base_asset": _base_asset_from_symbol(symbol, quote_asset),
        "timestamp_ms": _coerce_int(fill.get("timestamp_ms"), _coerce_int(fill.get("timestamp"))),
        "trigger": fill.get("trigger", ""),
        "reason": fill.get("reason", ""),
        "client_order_id": fill.get("client_order_id", ""),
        "pair_id": fill.get("pair_id", ""),
        "pair_role": fill.get("pair_role", ""),
        "expected_pair_net_edge_pct": _coerce_float(fill.get("expected_pair_net_edge_pct")),
        "completed_pair_net_edge_pct": _coerce_float(fill.get("completed_pair_net_edge_pct")),
    }


def _build_order_trade_record(order: Dict[str, Any], quote_asset: str, fee_rate: float) -> Dict[str, Any]:
    symbol = str(order.get("symbol") or "")
    side = str(order.get("side") or "").upper()
    status = str(order.get("status") or "OPEN").upper()
    quantity = _coerce_float(order.get("remaining_quantity"), _coerce_float(order.get("quantity")))
    filled_quantity = _coerce_float(order.get("filled_quantity"))
    price = _coerce_float(order.get("fill_price"))
    if price <= 0:
        price = _coerce_float(order.get("limit_price"), _coerce_float(order.get("price")))
    fee = _coerce_float(order.get("fee"))
    estimated_fee = quantity * price * fee_rate if quantity > 0 and price > 0 else 0.0
    reserved_quote = _coerce_float(order.get("reserved_quote"))
    reserved_base = _coerce_float(order.get("reserved_base"))
    is_open_like = status in {"OPEN", "NEW", "PARTIALLY_FILLED", "UNKNOWN"}
    if is_open_like and reserved_quote <= 0 and side == "BUY" and quantity > 0 and price > 0:
        reserved_quote = quantity * price * (1.0 + fee_rate)
    if is_open_like and reserved_base <= 0 and side == "SELL" and quantity > 0:
        reserved_base = quantity
    if status == "FILLED" and fee <= 0:
        fee_quantity = filled_quantity or quantity
        if fee_quantity > 0 and price > 0:
            fee = fee_quantity * price * fee_rate
    if not is_open_like:
        reserved_quote = 0.0
        reserved_base = 0.0
    base_asset = _base_asset_from_symbol(symbol, quote_asset)
    return {
        "record_type": "order",
        "status": "OPEN" if status == "NEW" else status,
        "event_type": order.get("event_type") or status,
        "symbol": symbol,
        "side": side,
        "quantity": filled_quantity if status == "FILLED" and filled_quantity > 0 else quantity,
        "price": price,
        "fill_price": _coerce_float(order.get("fill_price")),
        "limit_price": _coerce_float(order.get("limit_price"), price),
        "fee": fee,
        "estimated_fee": estimated_fee if fee <= 0 else 0.0,
        "realized_pnl": _coerce_float(order.get("realized_pnl")),
        "reserved_quote": reserved_quote,
        "reserved_base": reserved_base,
        "reserved_asset": quote_asset if reserved_quote > 0 else base_asset if reserved_base > 0 else "",
        "base_asset": base_asset,
        "timestamp_ms": _order_record_timestamp(order),
        "trigger": order.get("trigger", ""),
        "reason": order.get("reason", ""),
        "client_order_id": order.get("client_order_id", ""),
        "tier_index": _coerce_int(order.get("tier_index")),
        "ladder_group": order.get("ladder_group", ""),
        "target_fraction": _coerce_float(order.get("target_fraction")),
        "target_spread_pct": _coerce_float(order.get("target_spread_pct")),
        "current_spread_pct": _coerce_float(order.get("current_spread_pct")),
        "spread_delta_pct": _coerce_float(order.get("spread_delta_pct")),
        "reprice_tolerance_pct": _coerce_float(order.get("reprice_tolerance_pct")),
        "created_reference_price": _coerce_float(order.get("created_reference_price")),
        "pair_id": order.get("pair_id", ""),
        "pair_role": order.get("pair_role", ""),
        "expected_pair_net_edge_pct": _coerce_float(order.get("expected_pair_net_edge_pct")),
        "intended_counter_price": _coerce_float(order.get("intended_counter_price")),
    }


def _build_open_order_groups(open_orders: List[Dict[str, Any]], quote_asset: str) -> Dict[str, Any]:
    groups: Dict[str, Dict[str, Any]] = {}
    buy_prices: List[float] = []
    sell_prices: List[float] = []
    total_reserved_quote = 0.0
    total_reserved_base = 0.0
    buy_count = 0
    sell_count = 0
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        symbol = str(order.get("symbol") or "")
        side = str(order.get("side") or "").upper()
        group = str(order.get("ladder_group") or order.get("trigger") or "order")
        key = f"{symbol}:{group}:{side}"
        quantity = _coerce_float(order.get("remaining_quantity"), _coerce_float(order.get("quantity")))
        limit_price = _coerce_float(order.get("limit_price"), _coerce_float(order.get("price")))
        reserved_quote = _coerce_float(order.get("reserved_quote"))
        reserved_base = _coerce_float(order.get("reserved_base"))
        total_reserved_quote += reserved_quote
        total_reserved_base += reserved_base
        if side == "BUY":
            buy_count += 1
            if limit_price > 0:
                buy_prices.append(limit_price)
        elif side == "SELL":
            sell_count += 1
            if limit_price > 0:
                sell_prices.append(limit_price)
        bucket = groups.setdefault(
            key,
            {
                "symbol": symbol,
                "side": side,
                "ladder_group": group,
                "count": 0,
                "quantity": 0.0,
                "reserved_quote": 0.0,
                "reserved_base": 0.0,
                "nearest_price": 0.0,
                "orders": [],
            },
        )
        bucket["count"] += 1
        bucket["quantity"] += quantity
        bucket["reserved_quote"] += reserved_quote
        bucket["reserved_base"] += reserved_base
        if limit_price > 0:
            current_nearest = _coerce_float(bucket.get("nearest_price"))
            if current_nearest <= 0 or (side == "BUY" and limit_price > current_nearest) or (side == "SELL" and limit_price < current_nearest):
                bucket["nearest_price"] = limit_price
        bucket["orders"].append(order)
    return {
        "groups": list(groups.values()),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "total_count": buy_count + sell_count,
        "reserved_quote": total_reserved_quote,
        "reserved_base": total_reserved_base,
        "quote_asset": quote_asset,
        "nearest_buy_price": max(buy_prices) if buy_prices else 0.0,
        "nearest_sell_price": min(sell_prices) if sell_prices else 0.0,
    }


def _decorate_open_order_spreads(
    open_orders: List[Dict[str, Any]],
    market_prices: Dict[str, Any],
    runtime_config: Dict[str, Any],
) -> List[Dict[str, Any]]:
    decorated: List[Dict[str, Any]] = []
    tolerance = _coerce_float(runtime_config.get("order_reprice_tolerance_pct"))
    for order in open_orders:
        if not isinstance(order, dict):
            continue
        item = dict(order)
        symbol = str(item.get("symbol") or "")
        reference_price = _coerce_float(market_prices.get(symbol))
        limit_price = _coerce_float(item.get("limit_price"))
        side = str(item.get("side") or "").upper()
        current_spread = 0.0
        if reference_price > 0 and limit_price > 0:
            if side == "BUY":
                current_spread = (reference_price - limit_price) / reference_price
            elif side == "SELL":
                current_spread = (limit_price - reference_price) / reference_price
        target_spread = _coerce_float(item.get("target_spread_pct"))
        if target_spread <= 0:
            created_reference = _coerce_float(item.get("created_reference_price"))
            if created_reference > 0 and limit_price > 0:
                if side == "BUY":
                    target_spread = (created_reference - limit_price) / created_reference
                elif side == "SELL":
                    target_spread = (limit_price - created_reference) / created_reference
        item["current_spread_pct"] = current_spread
        item["target_spread_pct"] = max(0.0, target_spread)
        item["spread_delta_pct"] = abs(current_spread - target_spread) if current_spread >= 0 else 0.0
        item["reprice_tolerance_pct"] = tolerance
        item["open_order_action"] = "KEEP"
        item["open_order_action_reason"] = "open_order_waiting_for_touch"
        decorated.append(item)
    return decorated


def _build_trade_records(
    open_orders: List[Dict[str, Any]],
    recent_fills: List[Dict[str, Any]],
    order_events: List[Dict[str, Any]],
    quote_asset: str,
    fee_rate: float,
    limit: int | None = 500,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    current_open_clients = {str(order.get("client_order_id") or "") for order in open_orders if order.get("client_order_id")}
    collapsed_order_events = _collapse_order_events_for_trade_records(order_events, current_open_clients)
    events_by_client: Dict[str, Dict[str, Any]] = {}
    for event in collapsed_order_events:
        if isinstance(event, dict) and event.get("client_order_id"):
            events_by_client[str(event.get("client_order_id"))] = event
    for order in open_orders:
        if isinstance(order, dict):
            client_order_id = str(order.get("client_order_id") or "")
            matching_event = events_by_client.get(client_order_id, {})
            merged_order = dict(order)
            for key in (
                "symbol",
                "side",
                "quantity",
                "limit_price",
                "trigger",
                "created_at_ms",
                "updated_at_ms",
                "expires_at_ms",
                "target_spread_pct",
                "current_spread_pct",
                "spread_delta_pct",
                "reprice_tolerance_pct",
                "created_reference_price",
            ):
                current = merged_order.get(key)
                if current in (None, "", 0, 0.0):
                    merged_order[key] = matching_event.get(key, current)
            records.append(_build_order_trade_record(merged_order, quote_asset, fee_rate))
    for event in collapsed_order_events:
        if not isinstance(event, dict):
            continue
        status = str(event.get("status") or "").upper()
        client_order_id = str(event.get("client_order_id") or "")
        if status == "FILLED":
            continue
        if status in {"OPEN", "NEW"} and client_order_id and client_order_id in current_open_clients:
            continue
        records.append(_build_order_trade_record(event, quote_asset, fee_rate))
    for fill in recent_fills:
        if isinstance(fill, dict):
            records.append(_build_fill_trade_record(fill, quote_asset, fee_rate))

    deduped: Dict[tuple, Dict[str, Any]] = {}
    for record in records:
        key = (
            record.get("record_type"),
            record.get("client_order_id"),
            record.get("status"),
            record.get("timestamp_ms"),
            record.get("side"),
            record.get("price"),
            record.get("quantity"),
        )
        deduped[key] = record
    sorted_records = sorted(deduped.values(), key=lambda item: _coerce_int(item.get("timestamp_ms")), reverse=True)
    return sorted_records if limit is None else sorted_records[:limit]


def _collapse_order_events_for_trade_records(
    order_events: List[Dict[str, Any]],
    current_open_clients: set[str],
) -> List[Dict[str, Any]]:
    """Keep one table row per historical order instead of one row per polling event."""
    selected_by_client: Dict[str, Dict[str, Any]] = {}
    anonymous: List[Dict[str, Any]] = []
    terminal_statuses = {"CANCELED", "EXPIRED", "REJECTED", "FILLED"}
    for event in order_events:
        if not isinstance(event, dict):
            continue
        client_order_id = str(event.get("client_order_id") or "")
        status = str(event.get("status") or "").upper()
        event_type = str(event.get("event_type") or "").upper()
        if client_order_id and client_order_id in current_open_clients:
            continue
        if status == "FILLED" or event_type == "FILLED":
            continue
        if not client_order_id:
            anonymous.append(event)
            continue
        existing = selected_by_client.get(client_order_id)
        if existing is None:
            selected_by_client[client_order_id] = event
            continue
        existing_status = str(existing.get("status") or "").upper()
        existing_type = str(existing.get("event_type") or "").upper()
        existing_terminal = existing_status in terminal_statuses or existing_type in terminal_statuses
        current_terminal = status in terminal_statuses or event_type in terminal_statuses
        if current_terminal and not existing_terminal:
            selected_by_client[client_order_id] = event
    return [*selected_by_client.values(), *anonymous]


def _build_real_cost_basis_summary(
    runtime_dir: Path,
    paper_state: Dict[str, Any],
    latest_report: Dict[str, Any],
) -> Dict[str, Any]:
    manifest = _load_json(runtime_dir / "account_seed_manifest.json", {})
    quote_asset = str(paper_state.get("quote_asset") or manifest.get("quote_asset") or "JPY")
    balances = manifest.get("balances", {}) if isinstance(manifest.get("balances"), dict) else {}
    cost_basis_by_symbol = (
        manifest.get("cost_basis_by_symbol", {})
        if isinstance(manifest.get("cost_basis_by_symbol"), dict)
        else {}
    )
    positions = paper_state.get("positions", {}) if isinstance(paper_state.get("positions"), dict) else {}
    market_prices = latest_report.get("market_prices", {}) if isinstance(latest_report.get("market_prices"), dict) else {}
    if not balances or not cost_basis_by_symbol:
        return {}

    original_quote_balance = _coerce_float(balances.get(quote_asset))
    current_quote_balance = _coerce_float(paper_state.get("quote_balance"))
    original_cost_basis = 0.0
    current_market_value = 0.0
    realized_pnl = current_quote_balance - original_quote_balance
    unrealized_pnl = 0.0
    symbols: Dict[str, Dict[str, Any]] = {}

    for symbol, basis in cost_basis_by_symbol.items():
        if not isinstance(basis, dict):
            continue
        average_entry_price = _coerce_float(basis.get("average_entry_price"))
        if average_entry_price <= 0:
            continue
        base_asset = _base_asset_from_symbol(str(symbol), quote_asset)
        original_quantity = _coerce_float(balances.get(base_asset))
        position = positions.get(symbol, {}) if isinstance(positions.get(symbol), dict) else {}
        current_quantity = _coerce_float(position.get("quantity"))
        current_price = _coerce_float(
            market_prices.get(symbol),
            _coerce_float(position.get("average_entry_price"), _coerce_float(position.get("highest_price"))),
        )
        sold_quantity = max(0.0, original_quantity - current_quantity)
        symbol_original_cost = original_quantity * average_entry_price
        symbol_current_value = current_quantity * current_price
        symbol_unrealized = current_quantity * (current_price - average_entry_price)

        original_cost_basis += symbol_original_cost
        current_market_value += symbol_current_value
        realized_pnl -= sold_quantity * average_entry_price
        unrealized_pnl += symbol_unrealized
        symbols[symbol] = {
            "base_asset": base_asset,
            "original_quantity": original_quantity,
            "current_quantity": current_quantity,
            "sold_quantity": sold_quantity,
            "average_entry_price": average_entry_price,
            "current_price": current_price,
            "original_cost_basis": symbol_original_cost,
            "current_market_value": symbol_current_value,
            "realized_pnl": None,
            "unrealized_pnl": symbol_unrealized,
            "source": basis.get("source", ""),
        }

    original_initial_equity = original_quote_balance + original_cost_basis
    current_total_equity = current_quote_balance + current_market_value
    total_pnl = current_total_equity - original_initial_equity
    boti_initial_equity = _coerce_float(paper_state.get("initial_total_equity"))
    if boti_initial_equity <= 0:
        boti_initial_equity = _coerce_float(paper_state.get("initial_quote_balance")) + _coerce_float(
            paper_state.get("initial_market_value")
        )
    boti_operation_pnl = _coerce_float(paper_state.get("realized_pnl"), 0.0)
    boti_mark_to_market_pnl = _coerce_float(paper_state.get("net_pnl"), current_total_equity - boti_initial_equity)
    if len(symbols) == 1:
        only_symbol = next(iter(symbols))
        symbols[only_symbol]["realized_pnl"] = realized_pnl
    return {
        "quote_asset": quote_asset,
        "original_quote_balance": original_quote_balance,
        "current_quote_balance": current_quote_balance,
        "original_cost_basis": original_cost_basis,
        "original_initial_equity": original_initial_equity,
        "current_market_value": current_market_value,
        "current_total_equity": current_total_equity,
        "realized_pnl": realized_pnl,
        "unrealized_pnl": unrealized_pnl,
        "total_pnl": total_pnl,
        "boti_initial_equity": boti_initial_equity,
        "boti_operation_pnl": boti_operation_pnl,
        "boti_mark_to_market_pnl": boti_mark_to_market_pnl,
        "boti_net_pnl": boti_operation_pnl,
        "symbols": symbols,
    }


def _extract_live_trade_markers(history: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for cycle in history:
        cycle_timestamp = _coerce_int(cycle.get("timestamp_ms"))
        market_prices = cycle.get("market_prices", {})
        for decision in cycle.get("decisions", []):
            execution = decision.get("execution_result", {})
            if execution.get("status") != "PAPER_FILLED":
                continue
            symbol = execution.get("symbol") or decision.get("symbol")
            markers.append(
                {
                    "timestamp_ms": _coerce_int(execution.get("timestamp_ms"), cycle_timestamp),
                    "symbol": symbol,
                    "side": execution.get("side", ""),
                    "price": _coerce_float(execution.get("fill_price"), _coerce_float(market_prices.get(symbol))),
                    "quantity": _coerce_float(execution.get("quantity")),
                    "reason": execution.get("reason", ""),
                    "trigger": execution.get("trigger", ""),
                }
            )
        for event in cycle.get("order_lifecycle_events", []):
            if event.get("status") != "FILLED":
                continue
            markers.append(
                {
                    "timestamp_ms": _coerce_int(event.get("timestamp_ms"), cycle_timestamp),
                    "symbol": event.get("symbol", ""),
                    "side": event.get("side", ""),
                    "price": _coerce_float(event.get("fill_price"), _coerce_float(event.get("limit_price"))),
                    "quantity": _coerce_float(event.get("filled_quantity"), _coerce_float(event.get("quantity"))),
                    "reason": event.get("reason", ""),
                    "trigger": event.get("trigger", ""),
                }
            )
    return markers[-limit:]


def _extract_order_markers(history: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for cycle in history:
        for event in cycle.get("order_lifecycle_events", []):
            if event.get("status") not in {"OPEN", "FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                continue
            markers.append(
                {
                    "timestamp_ms": _coerce_int(event.get("timestamp_ms"), _coerce_int(cycle.get("timestamp_ms"))),
                    "symbol": event.get("symbol", ""),
                    "side": event.get("side", ""),
                    "status": event.get("status", ""),
                    "price": _coerce_float(event.get("fill_price"), _coerce_float(event.get("limit_price"))),
                    "quantity": _coerce_float(event.get("filled_quantity"), _coerce_float(event.get("quantity"))),
                    "trigger": event.get("trigger", ""),
                    "reason": event.get("reason", ""),
                    "client_order_id": event.get("client_order_id", ""),
                }
            )
    return markers[-limit:]


def _extract_position_activation_markers(history: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    activation_triggers = {
        "grid_profit_sell",
        "grid_loss_recovery_sell",
        "strategy_release_sell",
        "take_profit_release_sell",
        "trailing_stop_release_sell",
        "max_hold_release_sell",
        "grid_buyback",
    }
    markers = [
        marker
        for marker in _extract_live_trade_markers(history, limit=limit * 2)
        if marker.get("trigger") in activation_triggers
    ]
    return markers[-limit:]


def _extract_trade_marker_cycles_from_file(path: Path, scan_lines: int = 8000) -> List[Dict[str, Any]]:
    cycles = _load_recent_cycles_cached(path, scan_lines)
    return [cycle for cycle in cycles if _cycle_has_fill_marker(cycle)]


def _extract_chart_trade_markers_from_file(path: Path, limit: int = 200) -> List[Dict[str, Any]]:
    return _extract_live_trade_markers(_extract_trade_marker_cycles_from_file(path), limit=limit)


def _extract_position_activation_markers_from_file(path: Path, limit: int = 200) -> List[Dict[str, Any]]:
    return _extract_position_activation_markers(_extract_trade_marker_cycles_from_file(path), limit=limit)


def _extract_live_ai_veto_markers(history: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    markers: List[Dict[str, Any]] = []
    for cycle in history:
        cycle_timestamp = _coerce_int(cycle.get("timestamp_ms"))
        market_prices = cycle.get("market_prices", {})
        assessments = {
            item.get("symbol"): item
            for item in cycle.get("ai_risk_assessments", [])
            if isinstance(item, dict)
        }
        for decision in cycle.get("decisions", []):
            signal = decision.get("signal", {})
            symbol = decision.get("symbol") or signal.get("symbol")
            assessment = assessments.get(symbol)
            if not assessment:
                continue
            if str(signal.get("action", "")).upper() != "BUY":
                continue
            if assessment.get("allow_entry", True):
                continue
            markers.append(
                {
                    "timestamp_ms": cycle_timestamp,
                    "symbol": symbol,
                    "price": _coerce_float(market_prices.get(symbol)),
                    "reason": assessment.get("veto_reason", ""),
                    "risk_score": _coerce_float(assessment.get("risk_score")),
                }
            )
    return markers[-limit:]


def _extract_decision_ledger(history: List[Dict[str, Any]], latest_report: Dict[str, Any], limit: int = 50) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for cycle in history:
        ledger = cycle.get("decision_ledger", [])
        if isinstance(ledger, list):
            entries.extend(item for item in ledger if isinstance(item, dict))
    if not entries:
        ledger = latest_report.get("decision_ledger", [])
        if isinstance(ledger, list):
            entries.extend(item for item in ledger if isinstance(item, dict))
    return entries[-limit:][::-1]


def _extract_decision_ledger_from_file(
    path: Path,
    latest_report: Dict[str, Any],
    limit: int = 200,
    scan_lines: int | None = 800,
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for cycle in _iter_matching_cycles_from_file(path, ('"decision_ledger"',), scan_lines):
        ledger = cycle.get("decision_ledger", [])
        if isinstance(ledger, list):
            entries.extend(item for item in ledger if isinstance(item, dict))
    if not entries:
        return _extract_decision_ledger([], latest_report, limit=limit)
    return entries[-limit:][::-1]


def _detect_main_interval(latest_report: Dict[str, Any], backtest_manifest: Dict[str, Any]) -> str:
    decisions = latest_report.get("decisions", [])
    for decision in decisions:
        signal = decision.get("signal", {})
        reason = str(signal.get("reason", ""))
        match = re.search(r"([0-9]+[mhd])=", reason)
        if match:
            return match.group(1)
    market_snapshots = latest_report.get("market_snapshots", [])
    if market_snapshots:
        snapshot = market_snapshots[0]
        reason = str(snapshot.get("signal_reason", ""))
        match = re.search(r"([0-9]+[mhd])=", reason)
        if match:
            return match.group(1)
    config = backtest_manifest.get("config", {}) if isinstance(backtest_manifest, dict) else {}
    return str(config.get("main_interval") or "1h")


def _normalize_chart_interval(interval: str | None) -> str:
    value = (interval or "1m").strip()
    return value if value in CHART_INTERVAL_VALUES else "1m"


def _chart_interval_label(interval: str) -> str:
    for item in CHART_INTERVAL_OPTIONS:
        if item["value"] == interval:
            return item["label"]
    return interval


def _chart_interval_source(interval: str) -> str:
    for item in CHART_INTERVAL_OPTIONS:
        if item["value"] == interval:
            return item["source"]
    return "runtime"


def _chart_cache_path(runtime_dir: Path, symbol: str, interval: str) -> Path:
    safe_symbol = re.sub(r"[^A-Z0-9_-]", "", symbol.upper()) or "UNKNOWN"
    safe_interval = re.sub(r"[^A-Za-z0-9_-]", "", interval) or "1m"
    return runtime_dir / "chart_cache" / f"{safe_symbol}_{safe_interval}.json"


def _bar_from_candle(symbol: str, candle: Candle, *, source: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "open_time": candle.open_time,
        "close_time": candle.close_time,
        "open": candle.open,
        "high": candle.high,
        "low": candle.low,
        "close": candle.close,
        "volume": candle.volume,
        "sample_count": 1,
        "source": source,
    }


def _aggregate_chart_bars(
    bars: List[Dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    limit: int,
) -> List[Dict[str, Any]]:
    interval_ms = INTERVAL_MS.get(interval)
    if not interval_ms:
        return bars[-limit:]
    buckets: Dict[int, Dict[str, Any]] = {}
    for raw in sorted(bars, key=lambda item: _coerce_int(item.get("open_time"))):
        open_time = _coerce_int(raw.get("open_time"))
        close_time = _coerce_int(raw.get("close_time"), open_time)
        open_price = _coerce_float(raw.get("open"))
        high = _coerce_float(raw.get("high"))
        low = _coerce_float(raw.get("low"))
        close = _coerce_float(raw.get("close"))
        volume = _coerce_float(raw.get("volume"))
        if open_time < 0 or close <= 0:
            continue
        bucket_open = open_time - (open_time % interval_ms)
        bucket = buckets.get(bucket_open)
        if bucket is None:
            bucket = {
                "symbol": symbol,
                "open_time": bucket_open,
                "close_time": bucket_open + interval_ms - 1,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "sample_count": 0,
                "source": f"aggregate:{interval}",
            }
            buckets[bucket_open] = bucket
        else:
            bucket["high"] = max(_coerce_float(bucket.get("high")), high)
            bucket["low"] = min(_coerce_float(bucket.get("low")), low)
            bucket["close"] = close
            bucket["close_time"] = max(_coerce_int(bucket.get("close_time")), close_time)
            bucket["volume"] = _coerce_float(bucket.get("volume")) + volume
        bucket["sample_count"] = _coerce_int(bucket.get("sample_count"), 0) + max(1, _coerce_int(raw.get("sample_count"), 1))
    return [buckets[key] for key in sorted(buckets)][-limit:]


def _fetch_chart_bars_from_binance(
    *,
    symbol: str,
    interval: str,
    limit: int,
) -> tuple[List[Dict[str, Any]], str]:
    settings = load_settings()
    client = BinanceSpotClient(settings)
    try:
        if interval in NATIVE_BINANCE_INTERVALS:
            candles = client.get_klines(symbol=symbol, interval=interval, limit=min(limit, 1000))
            return [_bar_from_candle(symbol, candle, source="binance") for candle in candles], "binance"

        source = _chart_interval_source(interval)
        base_interval = source.split(":", 1)[1] if source.startswith("aggregate:") else "1m"
        base_ms = INTERVAL_MS.get(base_interval, INTERVAL_MS["1m"])
        interval_ms = INTERVAL_MS.get(interval, base_ms)
        required = max(2, min(1000, int((limit * interval_ms) / base_ms) + 4))
        candles = client.get_klines(symbol=symbol, interval=base_interval, limit=required)
        base_bars = [_bar_from_candle(symbol, candle, source=f"binance:{base_interval}") for candle in candles]
        return _aggregate_chart_bars(base_bars, symbol=symbol, interval=interval, limit=limit), source
    finally:
        client.close()


def _read_cached_chart_bars(path: Path) -> Dict[str, Any] | None:
    payload = _load_json(path, {})
    if not payload:
        return None
    bars = payload.get("bars")
    if not isinstance(bars, list):
        return None
    return payload


def _write_chart_cache(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _load_chart_bars_from_postgres(symbol: str, interval: str, limit: int) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    settings = load_settings()
    if settings.db_read_mode != "prefer_db":
        return [], {}
    try:
        payload = build_runtime_store(settings).query_candles(symbol=symbol, interval=interval, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return [], {"postgres_error": str(exc)[:240]}
    bars = [bar for bar in payload.get("bars", []) if isinstance(bar, dict)]
    return bars, {
        "source": "postgres_candle",
        "query_ms": payload.get("query_ms", 0),
        "cache_hit": True,
    }


def _chart_cache_refresh_seconds(interval: str) -> int:
    interval_ms = INTERVAL_MS.get(interval, INTERVAL_MS["1m"])
    if interval_ms <= INTERVAL_MS["5m"]:
        return 20
    if interval_ms <= INTERVAL_MS["1h"]:
        return 60
    if interval_ms <= INTERVAL_MS["8h"]:
        return 180
    return 900


def _latest_report_timestamp_ms(latest_report: Dict[str, Any]) -> int:
    return _coerce_int(
        latest_report.get("timestamp_ms"),
        _coerce_int(latest_report.get("generated_at_ms"), _coerce_int(latest_report.get("news_last_updated_ms"))),
    )


def _chart_cache_needs_tail_refresh(cached: Dict[str, Any], interval: str, latest_report: Dict[str, Any]) -> bool:
    bars = [bar for bar in cached.get("bars", []) if isinstance(bar, dict)]
    if not bars:
        return True
    latest_timestamp_ms = _latest_report_timestamp_ms(latest_report)
    if latest_timestamp_ms <= 0:
        return False
    last_close_time = max(_coerce_int(bar.get("close_time"), _coerce_int(bar.get("open_time"))) for bar in bars)
    if latest_timestamp_ms <= last_close_time:
        return False
    fetched_at = _coerce_float(cached.get("fetched_at"))
    if fetched_at <= 0:
        return True
    return (time.time() - fetched_at) >= _chart_cache_refresh_seconds(interval)


def _chart_cache_bars_match_interval(bars: List[Dict[str, Any]], interval: str) -> bool:
    interval_ms = INTERVAL_MS.get(interval)
    if not interval_ms:
        return True
    for bar in bars:
        open_time = _coerce_int(bar.get("open_time"), -1)
        close_time = _coerce_int(bar.get("close_time"), open_time + interval_ms - 1)
        if open_time < 0:
            return False
        if open_time % interval_ms != 0:
            return False
        if close_time < open_time:
            return False
        if (close_time - open_time + 1) > interval_ms:
            return False
    return True


def _append_chart_source(existing_source: object, source: str) -> str:
    parts = [item for item in str(existing_source or "cache").split("+") if item]
    if source not in parts:
        parts.append(source)
    return "+".join(parts)


def _merge_chart_bars(base_bars: List[Dict[str, Any]], overlay_bars: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    merged = {_coerce_int(bar.get("open_time")): dict(bar) for bar in base_bars if _coerce_int(bar.get("open_time")) >= 0}
    for bar in overlay_bars:
        open_time = _coerce_int(bar.get("open_time"))
        if open_time < 0:
            continue
        existing = merged.get(open_time)
        if existing is None:
            merged[open_time] = dict(bar)
            continue
        existing["high"] = max(_coerce_float(existing.get("high")), _coerce_float(bar.get("high")))
        existing["low"] = min(_coerce_float(existing.get("low")), _coerce_float(bar.get("low")))
        existing["close"] = _coerce_float(bar.get("close"), _coerce_float(existing.get("close")))
        existing["close_time"] = max(_coerce_int(existing.get("close_time")), _coerce_int(bar.get("close_time")))
        existing["volume"] = max(_coerce_float(existing.get("volume")), _coerce_float(bar.get("volume")))
        existing["sample_count"] = max(_coerce_int(existing.get("sample_count"), 1), _coerce_int(bar.get("sample_count"), 1))
        existing["source"] = _append_chart_source(existing.get("source", "cache"), "runtime_sample")
    return [merged[key] for key in sorted(merged)][-limit:]


def _load_or_fetch_chart_bars(
    *,
    runtime_dir: Path,
    history: List[Dict[str, Any]],
    latest_report: Dict[str, Any],
    symbol: str,
    interval: str,
    limit: int = 160,
    allow_fetch: bool,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    started_at = time.perf_counter()
    interval = _normalize_chart_interval(interval)
    fallback = _build_live_main_interval_bars(
        history,
        symbol=symbol,
        interval=interval,
        latest_report=latest_report,
        limit=limit,
    )
    cache_path = _chart_cache_path(runtime_dir, symbol, interval)
    cached = _read_cached_chart_bars(cache_path)
    if cached:
        cached_bars = [bar for bar in cached.get("bars", []) if isinstance(bar, dict)]
        if cached.get("interval") != interval or not _chart_cache_bars_match_interval(cached_bars, interval):
            cached = None
    if cached:
        cache_source = cached.get("source", "cache")
        cache_refreshed = False
        refresh_error = ""
        cache_bars = list(cached.get("bars", []))
        bars = _merge_chart_bars(
            cache_bars,
            fallback,
            limit,
        )
        return bars, {
            "source": cache_source,
            "cache_path": str(cache_path),
            "cache_hit": True,
            "fetched_at": cached.get("fetched_at"),
            "cache_policy": "immutable_history",
            "cache_refreshed": cache_refreshed,
            "refresh_error": refresh_error,
            "tail_refresh_due": _chart_cache_needs_tail_refresh(cached, interval, latest_report),
            "load_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }

    db_bars, db_meta = _load_chart_bars_from_postgres(symbol, interval, limit)
    if db_bars:
        bars = _merge_chart_bars(db_bars, fallback, limit)
        payload = {
            "symbol": symbol,
            "interval": interval,
            "label": _chart_interval_label(interval),
            "source": "postgres_candle",
            "fetched_at": time.time(),
            "bars": bars,
        }
        _write_chart_cache(cache_path, payload)
        return bars, {
            "source": "postgres_candle",
            "cache_path": str(cache_path),
            "cache_hit": True,
            "fetched_at": payload["fetched_at"],
            "cache_policy": "postgres_first",
            "postgres_query_ms": db_meta.get("query_ms", 0),
            "load_ms": round((time.perf_counter() - started_at) * 1000, 2),
        }

    if not allow_fetch or not symbol:
        return fallback, {
            "source": "runtime_sample",
            "cache_path": str(cache_path),
            "cache_hit": False,
            "cache_policy": "no_fetch_without_explicit_interval",
            "load_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "error": str(db_meta.get("postgres_error") or ""),
        }

    try:
        bars, source = _fetch_chart_bars_from_binance(symbol=symbol, interval=interval, limit=limit)
        if bars:
            payload = {
                "symbol": symbol,
                "interval": interval,
                "label": _chart_interval_label(interval),
                "source": source,
                "fetched_at": time.time(),
                "bars": bars,
            }
            _write_chart_cache(cache_path, payload)
            return bars[-limit:], {
                "source": source,
                "cache_path": str(cache_path),
                "cache_hit": False,
                "fetched_at": payload["fetched_at"],
                "cache_policy": "download_once",
                "load_ms": round((time.perf_counter() - started_at) * 1000, 2),
            }
    except Exception as exc:  # noqa: BLE001 - dashboard must degrade to runtime samples instead of 500.
        return fallback, {
            "source": "runtime_sample",
            "cache_path": str(cache_path),
            "cache_hit": False,
            "cache_policy": "fallback_after_fetch_error",
            "load_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "error": str(exc),
        }

    return fallback, {
        "source": "runtime_sample",
        "cache_path": str(cache_path),
        "cache_hit": False,
        "cache_policy": "fallback_after_empty_fetch",
        "load_ms": round((time.perf_counter() - started_at) * 1000, 2),
        "error": "empty_binance_response",
    }


def _build_live_main_interval_bars(
    history: List[Dict[str, Any]],
    *,
    symbol: str,
    interval: str,
    latest_report: Dict[str, Any] | None = None,
    limit: int = 120,
) -> List[Dict[str, Any]]:
    latest_report = latest_report or {}
    snapshot_bars: List[Dict[str, Any]] = []
    for snapshot in latest_report.get("market_snapshots", []):
        if str(snapshot.get("symbol", "")).upper() != symbol.upper():
            continue
        for raw_bar in snapshot.get("main_interval_bars", []):
            if not isinstance(raw_bar, dict):
                continue
            bar = {
                "symbol": symbol,
                "open_time": _coerce_int(raw_bar.get("open_time")),
                "close_time": _coerce_int(raw_bar.get("close_time")),
                "open": _coerce_float(raw_bar.get("open")),
                "high": _coerce_float(raw_bar.get("high")),
                "low": _coerce_float(raw_bar.get("low")),
                "close": _coerce_float(raw_bar.get("close")),
                "volume": _coerce_float(raw_bar.get("volume")),
                "sample_count": _coerce_int(raw_bar.get("sample_count"), 1),
                "source": "binance_kline",
            }
            if bar["open_time"] > 0 and bar["close"] > 0:
                snapshot_bars.append(bar)
        break

    interval_ms = INTERVAL_MS.get(interval, INTERVAL_MS["1h"])
    buckets: Dict[int, Dict[str, Any]] = {}
    for cycle in history:
        timestamp_ms = _coerce_int(cycle.get("timestamp_ms"))
        price = _coerce_float((cycle.get("market_prices") or {}).get(symbol))
        if timestamp_ms <= 0 or price <= 0:
            continue
        bucket_open = timestamp_ms - (timestamp_ms % interval_ms)
        bucket = buckets.get(bucket_open)
        if bucket is None:
            bucket = {
                "symbol": symbol,
                "open_time": bucket_open,
                "close_time": bucket_open + interval_ms - 1,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "sample_count": 0,
            }
            buckets[bucket_open] = bucket
        bucket["sample_count"] += 1
        bucket["high"] = max(bucket["high"], price)
        bucket["low"] = min(bucket["low"], price)
        bucket["close"] = price

    bars = [buckets[key] for key in sorted(buckets)]
    if not snapshot_bars:
        return bars[-limit:]

    if interval != "1m":
        snapshot_bars = _aggregate_chart_bars(snapshot_bars, symbol=symbol, interval=interval, limit=limit)

    merged = {bar["open_time"]: dict(bar) for bar in snapshot_bars}
    for bar in bars:
        existing = merged.get(bar["open_time"])
        if existing:
            existing["high"] = max(_coerce_float(existing.get("high")), _coerce_float(bar.get("high")))
            existing["low"] = min(_coerce_float(existing.get("low")), _coerce_float(bar.get("low")))
            existing["close"] = _coerce_float(bar.get("close"), _coerce_float(existing.get("close")))
            existing["sample_count"] = _coerce_int(existing.get("sample_count"), 1) + _coerce_int(bar.get("sample_count"), 0)
            existing["source"] = "binance_kline+runtime_sample"
        else:
            merged[bar["open_time"]] = dict(bar, source="runtime_sample")
    return [merged[key] for key in sorted(merged)][-limit:]


def _load_backtest_payload(runtime_dir: Path) -> Dict[str, Any]:
    repo_root = runtime_dir.resolve().parent
    candidates = [
        repo_root / "runtime_backtest_walk",
        repo_root / "runtime_backtest_check",
    ]
    existing_dirs = [directory.name for directory in candidates if directory.exists()]
    for directory in candidates:
        summary = _load_json(directory / "summary.json", {})
        if not summary:
            continue
        return {
            "backtest_available": True,
            "backtest_source": directory.name,
            "backtest_summary": summary,
            "backtest_segments": _load_json(directory / "segments.json", []),
            "backtest_equity_curve": _sample_rows(_load_csv_rows(directory / "equity_curve.csv"), 160),
            "backtest_trades": _load_csv_rows(directory / "trades.csv"),
            "backtest_manifest": _load_json(directory / "run_manifest.json", {}),
            "backtest_missing_reason": None,
        }

    missing_reason = (
        f"{', '.join(existing_dirs)} 只有缓存或结果文件不完整，需要重新运行离线回测。"
        if existing_dirs
        else "未找到 runtime_backtest_walk / runtime_backtest_check。"
    )
    return {
        "backtest_available": False,
        "backtest_source": None,
        "backtest_summary": {},
        "backtest_segments": [],
        "backtest_equity_curve": [],
        "backtest_trades": [],
        "backtest_manifest": {},
        "backtest_missing_reason": missing_reason,
    }


def _sample_rows(rows: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    if limit <= 0 or len(rows) <= limit:
        return rows
    if limit == 1:
        return [rows[-1]]
    step = (len(rows) - 1) / (limit - 1)
    sampled: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for index in range(limit):
        source_index = round(index * step)
        if source_index in seen:
            continue
        sampled.append(rows[source_index])
        seen.add(source_index)
    if sampled[-1] is not rows[-1]:
        sampled[-1] = rows[-1]
    return sampled


def _build_live_profit_curve(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    baseline_equity: float | None = None
    for row in history:
        timestamp_ms = _coerce_int(row.get("timestamp_ms") or row.get("generated_at_ms") or row.get("time_ms"))
        if timestamp_ms <= 0:
            continue
        total_equity = _coerce_float(row.get("total_equity"), float("nan"))
        net_pnl = _coerce_float(row.get("net_pnl"), float("nan"))
        realized_pnl = _coerce_float(row.get("realized_pnl"), 0.0)
        unrealized_pnl = _coerce_float(row.get("unrealized_pnl"), float("nan"))
        if not (total_equity == total_equity):
            paper_state = row.get("paper_state") if isinstance(row.get("paper_state"), dict) else {}
            total_equity = _coerce_float(paper_state.get("total_equity"), float("nan"))
            if not (net_pnl == net_pnl):
                net_pnl = _coerce_float(paper_state.get("net_pnl"), float("nan"))
        if not (total_equity == total_equity) and not (net_pnl == net_pnl):
            continue
        if baseline_equity is None and total_equity == total_equity:
            baseline_equity = total_equity - net_pnl if net_pnl == net_pnl else total_equity
        if not (net_pnl == net_pnl):
            net_pnl = total_equity - (baseline_equity or total_equity)
        point = {
            "timestamp_ms": timestamp_ms,
            "net_pnl": net_pnl,
        }
        if total_equity == total_equity:
            point["total_equity"] = total_equity
        point["realized_pnl"] = realized_pnl
        if unrealized_pnl == unrealized_pnl:
            point["unrealized_pnl"] = unrealized_pnl
        points.append(point)
    return _sample_rows(points, 800)


def _dashboard_chart_symbol(latest_report: Dict[str, Any]) -> str:
    if latest_report.get("decisions"):
        return latest_report["decisions"][0].get("symbol", "")
    if latest_report.get("market_prices"):
        return next(iter(latest_report["market_prices"]))
    return ""


def _build_dashboard_chart_payload(
    runtime_dir: Path,
    *,
    latest_report: Dict[str, Any] | None = None,
    history: List[Dict[str, Any]] | None = None,
    chart_interval: str | None = None,
    backtest_manifest: Dict[str, Any] | None = None,
    marker_history_limit: int | None = None,
) -> Dict[str, Any]:
    latest_report = latest_report if latest_report is not None else _load_json(runtime_dir / "latest_report.json", {})
    history_path = runtime_dir / "cycle_reports.jsonl"
    if history is None:
        marker_history_limit = marker_history_limit or DASHBOARD_CHART_MARKER_DEFAULT_SCAN_LINES
        history = _read_history(history_path, limit=marker_history_limit)
    trade_markers = _extract_live_trade_markers(history, limit=200)
    activation_markers = _extract_position_activation_markers(history, limit=200)
    chart_symbol = _dashboard_chart_symbol(latest_report)
    selected_chart_interval = _normalize_chart_interval(chart_interval)
    main_interval = _detect_main_interval(latest_report, backtest_manifest or {})
    chart_bars, chart_meta = (
        _load_or_fetch_chart_bars(
            runtime_dir=runtime_dir,
            history=history,
            latest_report=latest_report,
            symbol=chart_symbol,
            interval=selected_chart_interval,
            allow_fetch=chart_interval is not None,
        )
        if chart_symbol
        else ([], {"source": "runtime_sample", "cache_path": "", "cache_hit": False})
    )
    return {
        "live_chart_symbol": chart_symbol,
        "live_main_interval": main_interval,
        "live_chart_interval": selected_chart_interval,
        "live_chart_interval_label": _chart_interval_label(selected_chart_interval),
        "live_chart_source": chart_meta.get("source", "runtime_sample"),
        "live_chart_cache": chart_meta,
        "live_chart_bars": chart_bars,
        "live_trade_markers": trade_markers or _extract_live_trade_markers(history),
        "order_markers": _extract_order_markers(history),
        "position_activation_markers": activation_markers or _extract_position_activation_markers(history),
        "live_ai_veto_markers": _extract_live_ai_veto_markers(history),
    }


def _build_decision_state_payload(
    paper_state: Dict[str, Any],
    latest_report: Dict[str, Any],
    runtime_config: Dict[str, Any],
) -> Dict[str, Any]:
    activation_state = paper_state.get("activation_state", {})
    if not isinstance(activation_state, dict):
        activation_state = {}
    timestamp_ms = _latest_report_timestamp_ms(latest_report)
    interval = str(runtime_config.get("kline_interval") or "1h")
    interval_ms = INTERVAL_MS.get(interval, INTERVAL_MS["1h"])
    required_edge_pct = _coerce_float(runtime_config.get("required_roundtrip_edge_pct"))
    summary: Dict[str, Any] = {}
    guard: Dict[str, Any] = {}
    protection: Dict[str, Any] = {}
    cooldowns: Dict[str, int] = {}
    for symbol, raw_state in activation_state.items():
        if not isinstance(raw_state, dict):
            continue
        cooldown_until = _coerce_int(raw_state.get("buyback_cooldown_until_candle"))
        remaining = 0
        if timestamp_ms > 0 and cooldown_until > timestamp_ms:
            remaining = int((cooldown_until - timestamp_ms + interval_ms - 1) // interval_ms)
        pending = _coerce_float(raw_state.get("pending_buyback_quantity"))
        decision_state = str(raw_state.get("decision_state") or ("RELEASED_WAIT_BUYBACK" if pending > 0 else "NORMAL"))
        if remaining > 0:
            decision_state = "BUYBACK_COOLDOWN"
        summary[str(symbol)] = {
            "decision_state": decision_state,
            "pending_buyback_quantity": pending,
            "last_grid_sell_price": _coerce_float(raw_state.get("last_grid_sell_price")),
            "last_release_price": _coerce_float(raw_state.get("last_release_price")),
            "target_buyback_price": _coerce_float(raw_state.get("target_buyback_price")),
            "post_initial_release_mode": raw_state.get("post_initial_release_mode", ""),
            "effective_cost_price": _coerce_float(raw_state.get("real_average_entry_price")),
            "cost_protection_price": _coerce_float(raw_state.get("real_average_entry_price")) * (
                1.0 + _coerce_float(runtime_config.get("sell_cost_protection_buffer_pct"), 0.0015)
            ),
            "partial_stop_count": _coerce_int(raw_state.get("partial_stop_count")),
        }
        guard[str(symbol)] = {
            "last_net_edge_pct": _coerce_float(raw_state.get("last_net_edge_pct")),
            "required_edge_pct": required_edge_pct,
            "last_release_fee_adjusted_price": _coerce_float(raw_state.get("last_release_fee_adjusted_price")),
            "last_trigger": raw_state.get("last_trigger", ""),
            "last_reason": raw_state.get("last_reason", ""),
        }
        protection[str(symbol)] = {
            "protected": remaining > 0,
            "cooldown_until_candle": cooldown_until,
            "cooldown_remaining_bars": remaining,
        }
        cooldowns[str(symbol)] = remaining
    return {
        "decision_state_summary": summary,
        "profitability_guard": guard,
        "buyback_protection": protection,
        "cooldown_remaining_bars": cooldowns,
    }


def _build_target_inventory_payload(latest_report: Dict[str, Any]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    guards: Dict[str, Any] = {}
    daily: Dict[str, Any] = {}
    for decision in latest_report.get("decisions", []) if isinstance(latest_report.get("decisions"), list) else []:
        if not isinstance(decision, dict):
            continue
        symbol = str(decision.get("symbol") or "")
        execution = decision.get("execution_result") if isinstance(decision.get("execution_result"), dict) else {}
        target = execution.get("target_inventory_summary") if isinstance(execution.get("target_inventory_summary"), dict) else {}
        if symbol and target:
            summary[symbol] = target
            guards[symbol] = {
                "min_effective_order_notional": _coerce_float(execution.get("min_effective_order_notional")),
                "guard_result": execution.get("guard_result", ""),
                "net_edge_pct": _coerce_float(execution.get("net_edge_pct")),
                "required_edge_pct": _coerce_float(execution.get("required_edge_pct")),
                "reason": execution.get("reason", ""),
            }
            daily[symbol] = {
                "daily_turnover_used": _coerce_float(target.get("daily_turnover_used")),
                "daily_turnover_limit": _coerce_float(target.get("daily_turnover_limit")),
                "daily_realized_pnl": _coerce_float(target.get("daily_realized_pnl")),
                "daily_loss_limit": _coerce_float(target.get("daily_loss_limit")),
                "active_trading_allowed": bool(target.get("active_trading_allowed", True)),
                "active_trading_blocker": target.get("active_trading_blocker", ""),
            }
    return {
        "target_inventory_summary": summary,
        "effective_order_guard": guards,
        "daily_risk_budget": daily,
    }


def _build_policy_payload(latest_report: Dict[str, Any]) -> Dict[str, Any]:
    policy_state_summary: Dict[str, Any] = {}
    protection_summary: Dict[str, Any] = {}
    order_proposal_summary: Dict[str, Any] = {}
    decisions = latest_report.get("policy_decisions", [])
    if not isinstance(decisions, list):
        decisions = []
    for item in decisions:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "")
        if not symbol:
            continue
        locks = item.get("protection_locks") if isinstance(item.get("protection_locks"), list) else []
        proposals = item.get("order_proposals") if isinstance(item.get("order_proposals"), list) else []
        filters = item.get("proposal_filter_results") if isinstance(item.get("proposal_filter_results"), list) else []
        active_locks = [lock for lock in locks if isinstance(lock, dict) and bool(lock.get("active"))]
        rejected = [result for result in filters if isinstance(result, dict) and not bool(result.get("allowed"))]
        policy_state_summary[symbol] = {
            "policy_state": item.get("policy_state", ""),
            "recommended_action": item.get("recommended_action", ""),
            "reason": item.get("mode_reason_cn", ""),
            "inventory_skew_summary": item.get("inventory_skew_summary", {}),
            "direction_decision": item.get("direction_decision", {}),
            "scenario_decision": item.get("scenario_decision", {}),
            "accepted_proposal_count": len(proposals),
            "rejected_proposal_count": len(rejected),
        }
        protection_summary[symbol] = {
            "active_lock_count": len(active_locks),
            "locks": locks,
        }
        order_proposal_summary[symbol] = {
            "proposals": proposals,
            "merged_proposals": item.get("merged_order_proposals", []),
            "filter_results": filters,
            "blocked_reasons_cn": [str(result.get("reason_cn", "")) for result in rejected],
        }
    return {
        "policy_state_summary": policy_state_summary,
        "protection_summary": protection_summary,
        "order_proposal_summary": order_proposal_summary,
    }


def _build_pair_payload(paper_state: Dict[str, Any]) -> Dict[str, Any]:
    open_pairs = paper_state.get("open_order_pairs", {}) if isinstance(paper_state.get("open_order_pairs"), dict) else {}
    completed_pairs = paper_state.get("completed_order_pairs", []) if isinstance(paper_state.get("completed_order_pairs"), list) else []
    pair_locks = paper_state.get("pair_locks", {}) if isinstance(paper_state.get("pair_locks"), dict) else {}
    pair_stats = paper_state.get("pair_profitability_stats", {}) if isinstance(paper_state.get("pair_profitability_stats"), dict) else {}
    positive_count = 0
    negative_count = 0
    for stats in pair_stats.values():
        if not isinstance(stats, dict):
            continue
        positive_count += _coerce_int(stats.get("positive_count"))
        negative_count += _coerce_int(stats.get("negative_count"))
    return {
        "pair_state_summary": {
            "open_pair_count": len(open_pairs),
            "completed_pair_count": len(completed_pairs),
            "open_pairs": list(open_pairs.values()),
        },
        "pair_profitability_summary": {
            "stats": pair_stats,
            "positive_pair_count": positive_count,
            "negative_pair_count": negative_count,
        },
        "protection_summary_pairs": pair_locks,
    }


def build_ops_health_payload(runtime_dir: Path) -> Dict[str, Any]:
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    paper_state = _load_json(runtime_dir / "paper_state.json", {})
    return {
        "status": "ok" if latest_report else "degraded",
        "latest_report_timestamp_ms": _latest_report_timestamp_ms(latest_report),
        "cycle_mode": latest_report.get("cycle_mode", ""),
        "cycle_reason": latest_report.get("cycle_reason", ""),
        "open_order_count": len((paper_state.get("open_orders") or {})),
        "open_pair_count": len((paper_state.get("open_order_pairs") or {})),
        "active_pair_locks": paper_state.get("pair_locks", {}),
    }


def build_ops_summary_payload(runtime_dir: Path, hours: int) -> Dict[str, Any]:
    settings = load_settings()
    if settings.db_read_mode == "prefer_db":
        try:
            return build_runtime_store(settings).query_ops_summary(hours)
        except Exception:
            pass
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    paper_state = _load_json(runtime_dir / "paper_state.json", {})
    history_path = runtime_dir / "cycle_reports.jsonl"
    fills = _extract_recent_fills_from_file(history_path, limit=2000, scan_lines=None)
    events = _extract_order_lifecycle_events_from_file(history_path, limit=2000, scan_lines=None)
    cutoff_ms = int(time.time() * 1000) - max(1, hours) * 60 * 60 * 1000
    recent_fills = [item for item in fills if _coerce_int(item.get("timestamp_ms")) >= cutoff_ms]
    recent_events = [item for item in events if _coerce_int(item.get("timestamp_ms")) >= cutoff_ms]
    rejected_count = sum(1 for item in recent_events if str(item.get("status", "")).upper() == "REJECTED")
    canceled_count = sum(1 for item in recent_events if str(item.get("status", "")).upper() in {"CANCELED", "EXPIRED"})
    fee_total = sum(_coerce_float(item.get("fee")) for item in recent_fills)
    pair_stats = paper_state.get("pair_profitability_stats", {}) if isinstance(paper_state.get("pair_profitability_stats"), dict) else {}
    positive_pairs = sum(_coerce_int(item.get("positive_count")) for item in pair_stats.values() if isinstance(item, dict))
    negative_pairs = sum(_coerce_int(item.get("negative_count")) for item in pair_stats.values() if isinstance(item, dict))
    activation_state = paper_state.get("activation_state", {}) if isinstance(paper_state.get("activation_state"), dict) else {}
    pending_buyback_quantity = 0.0
    for item in activation_state.values():
        if isinstance(item, dict):
            pending_buyback_quantity += _coerce_float(item.get("pending_buyback_quantity"))
    negative_realized_sell_count = sum(
        1
        for item in recent_fills
        if str(item.get("side", "")).upper() == "SELL"
        and _coerce_float(item.get("realized_pnl_delta")) < 0
        and _coerce_float(item.get("excluded_initial_inventory_quantity")) <= 0
    )
    return {
        "status": "ok" if latest_report else "degraded",
        "hours": hours,
        "fill_count": len(recent_fills),
        "cancel_count": canceled_count,
        "reject_count": rejected_count,
        "fee_total": fee_total,
        "completed_pair_count": len(paper_state.get("completed_order_pairs", [])) if isinstance(paper_state.get("completed_order_pairs", []), list) else 0,
        "positive_pair_count": positive_pairs,
        "negative_pair_count": negative_pairs,
        "negative_realized_sell_count": negative_realized_sell_count,
        "pending_buyback_quantity": pending_buyback_quantity,
        "below_cost_sell_block_count": sum(
            1 for item in recent_events if str(item.get("reason", "")).lower() == "below_cost_sell_blocked"
        ),
        "current_pair_locks": paper_state.get("pair_locks", {}),
        "current_open_pairs": list((paper_state.get("open_order_pairs") or {}).values()) if isinstance(paper_state.get("open_order_pairs"), dict) else [],
    }


def build_ops_pairs_payload(runtime_dir: Path, hours: int) -> Dict[str, Any]:
    paper_state = _load_json(runtime_dir / "paper_state.json", {})
    completed_pairs = paper_state.get("completed_order_pairs", []) if isinstance(paper_state.get("completed_order_pairs"), list) else []
    open_pairs = list((paper_state.get("open_order_pairs") or {}).values()) if isinstance(paper_state.get("open_order_pairs"), dict) else []
    cutoff_ms = int(time.time() * 1000) - max(1, hours) * 60 * 60 * 1000
    recent_completed = [
        item for item in completed_pairs
        if isinstance(item, dict) and _coerce_int(item.get("completed_at_ms"), _coerce_int(item.get("updated_at_ms"))) >= cutoff_ms
    ]
    return {
        "hours": hours,
        "open_pairs": open_pairs,
        "completed_pairs": recent_completed,
    }


def build_ops_orders_payload(runtime_dir: Path, status: str) -> Dict[str, Any]:
    payload = build_order_records_payload(runtime_dir)
    records = payload.get("trade_records", [])
    normalized = status.strip().lower()
    if normalized == "open":
        filtered = [item for item in records if str(item.get("status", "")).upper() in {"OPEN", "NEW", "PARTIALLY_FILLED", "UNKNOWN"}]
    elif normalized == "filled":
        filtered = [item for item in records if str(item.get("status", "")).upper() in {"FILLED", "PAPER_FILLED"}]
    elif normalized == "canceled":
        filtered = [item for item in records if str(item.get("status", "")).upper() in {"CANCELED", "EXPIRED", "REJECTED"}]
    else:
        filtered = records
    return {
        "status_filter": normalized or "all",
        "trade_records": filtered,
        "trade_record_count": len(filtered),
    }


def _parse_ops_time_ms(value: str | None, default: int) -> int:
    raw = (value or "").strip()
    if not raw:
        return default
    try:
        return int(float(raw))
    except ValueError:
        pass
    normalized = raw.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _ops_time_window(query: Dict[str, List[str]], default_hours: int = 8) -> tuple[int, int]:
    now_ms = int(time.time() * 1000)
    hours = max(1, _coerce_int((query.get("hours") or [str(default_hours)])[0], default_hours))
    from_ms = _parse_ops_time_ms((query.get("from_ms") or query.get("from") or [""])[0], now_ms - hours * 60 * 60 * 1000)
    to_ms = _parse_ops_time_ms((query.get("to_ms") or query.get("to") or [""])[0], now_ms)
    if to_ms < from_ms:
        from_ms, to_ms = to_ms, from_ms
    return from_ms, to_ms


def _filter_records_by_window(records: List[Dict[str, Any]], from_ms: int, to_ms: int) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    for record in records:
        timestamp_ms = _coerce_int(record.get("timestamp_ms"), _coerce_int(record.get("time")))
        if from_ms <= timestamp_ms <= to_ms:
            filtered.append(record)
    return filtered


def _normalize_real_trade(raw: Dict[str, Any], symbol: str) -> Dict[str, Any]:
    price = _coerce_float(raw.get("price"))
    quantity = _coerce_float(raw.get("qty"))
    quote_qty = _coerce_float(raw.get("quoteQty"), price * quantity)
    return {
        "source": "real",
        "record_type": "real_trade",
        "symbol": symbol,
        "trade_id": str(raw.get("id", "")),
        "order_id": str(raw.get("orderId", "")),
        "side": "BUY" if bool(raw.get("isBuyer")) else "SELL",
        "price": price,
        "quantity": quantity,
        "notional": quote_qty,
        "fee": _coerce_float(raw.get("commission")),
        "fee_asset": raw.get("commissionAsset", ""),
        "is_maker": bool(raw.get("isMaker")),
        "timestamp_ms": _coerce_int(raw.get("time")),
        "raw": raw,
    }


def _load_real_trades(symbols: List[str], from_ms: int, to_ms: int, limit: int) -> List[Dict[str, Any]]:
    settings = load_settings()
    if not settings.api_key or not settings.api_secret:
        return []
    client = BinanceSpotClient(settings)
    records: List[Dict[str, Any]] = []
    try:
        for symbol in symbols:
            trades = client.get_my_trades(
                symbol,
                limit=max(1, min(1000, limit)),
                start_time_ms=from_ms,
                end_time_ms=to_ms,
            )
            records.extend(_normalize_real_trade(item, symbol) for item in trades)
    finally:
        client.close()
    return sorted(records, key=lambda item: _coerce_int(item.get("timestamp_ms")), reverse=True)


def build_ops_trades_payload(runtime_dir: Path, query: Dict[str, List[str]]) -> Dict[str, Any]:
    from_ms, to_ms = _ops_time_window(query)
    source = str((query.get("source") or ["all"])[0]).strip().lower() or "all"
    status = str((query.get("status") or ["all"])[0]).strip().lower()
    limit = max(1, min(5000, _coerce_int((query.get("limit") or ["1000"])[0], 1000)))
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    settings_symbols = []
    try:
        settings_symbols = list(load_settings().trading_symbols)
    except Exception:
        settings_symbols = []
    symbol_filter = str((query.get("symbol") or [""])[0]).strip().upper()
    symbols = [symbol_filter] if symbol_filter else settings_symbols or list((latest_report.get("market_prices") or {}).keys())

    simulated_records: List[Dict[str, Any]] = []
    if source in {"all", "paper", "sim", "simulated"}:
        payload = build_order_records_payload(runtime_dir)
        simulated_records = payload.get("trade_records", []) if isinstance(payload.get("trade_records"), list) else []
        simulated_records = [dict(item, source="simulated") for item in simulated_records if isinstance(item, dict)]
        simulated_records = _filter_records_by_window(simulated_records, from_ms, to_ms)

    real_records: List[Dict[str, Any]] = []
    real_error = ""
    if source in {"all", "real", "binance"}:
        try:
            real_records = _load_real_trades(symbols, from_ms, to_ms, limit)
        except Exception as exc:  # noqa: BLE001
            real_error = str(exc)

    records = [*simulated_records, *real_records]
    if symbol_filter:
        records = [item for item in records if str(item.get("symbol", "")).upper() == symbol_filter]
    if status not in {"", "all"}:
        records = [item for item in records if str(item.get("status", item.get("event_type", ""))).lower() == status]
    records = sorted(records, key=lambda item: _coerce_int(item.get("timestamp_ms")), reverse=True)[:limit]
    return {
        "from_ms": from_ms,
        "to_ms": to_ms,
        "source": source,
        "symbol": symbol_filter,
        "records": records,
        "record_count": len(records),
        "simulated_count": len([item for item in records if item.get("source") == "simulated"]),
        "real_count": len([item for item in records if item.get("source") == "real"]),
        "real_error": real_error,
    }


def _git_value(args: List[str], cwd: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=8,
        )
    except Exception as exc:  # noqa: BLE001
        return f"ERROR:{exc}"
    if completed.returncode != 0:
        return f"ERROR:{completed.stderr.strip()}"
    return completed.stdout.strip()


def build_ops_version_payload(runtime_dir: Path, *, refresh: bool = False) -> Dict[str, Any]:
    cwd = Path.cwd()
    if refresh:
        _git_value(["fetch", "--quiet", "origin"], cwd)
    branch = _git_value(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    head = _git_value(["rev-parse", "HEAD"], cwd)
    upstream = _git_value(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], cwd)
    upstream_head = _git_value(["rev-parse", "@{u}"], cwd) if not upstream.startswith("ERROR:") else ""
    dirty = bool(_git_value(["status", "--porcelain"], cwd))
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    return {
        "repo_path": str(cwd),
        "runtime_dir": str(runtime_dir),
        "branch": branch,
        "head": head,
        "upstream": upstream,
        "upstream_head": upstream_head,
        "dirty": dirty,
        "needs_pull": bool(upstream_head and not upstream_head.startswith("ERROR:") and head != upstream_head),
        "latest_report_timestamp_ms": _latest_report_timestamp_ms(latest_report),
        "cycle_mode": latest_report.get("cycle_mode", ""),
        "cycle_reason": latest_report.get("cycle_reason", ""),
    }


def _ops_write_token() -> str:
    return (os.environ.get("BOTINANCE_OPS_TOKEN") or os.environ.get("OPS_WRITE_TOKEN") or "").strip()


def _seed_paper_from_real_account(
    runtime_dir: Path,
    *,
    archive_root: Path | None,
    cash_baseline: bool = False,
    min_cash_baseline: float = 0.0,
    min_assets: Dict[str, float] | None = None,
) -> Dict[str, Any]:
    settings = load_settings()
    if not settings.dry_run:
        raise RuntimeError("Refusing to reset paper state while DRY_RUN=false.")
    symbols = [item.strip().upper() for item in settings.trading_symbols if item.strip()]
    if not symbols:
        raise RuntimeError("No trading symbols configured.")
    client = BinanceSpotClient(settings)
    try:
        balances = client.get_account_balances(include_locked=True)
        prices = {symbol: client.get_symbol_price(symbol) for symbol in symbols}
        cost_basis_by_symbol: Dict[str, Dict[str, object]] = {}
        for symbol in symbols:
            base_asset = base_asset_for_symbol(symbol, settings.quote_asset)
            quantity = float(balances.get(base_asset, 0.0))
            if quantity <= 0:
                continue
            try:
                cost_basis_by_symbol[symbol] = infer_remaining_cost_basis_from_trades(
                    client.get_my_trades(symbol, limit=1000),
                    quantity,
                )
            except Exception as exc:  # noqa: BLE001
                cost_basis_by_symbol[symbol] = {"source": "trade_history_error", "error": str(exc)}
    finally:
        client.close()

    timestamp_ms = int(time.time() * 1000)
    cash_baseline_equity = calculate_cash_baseline_equity(
        balances=balances,
        symbols=symbols,
        quote_asset=settings.quote_asset,
        prices=prices,
    )
    validate_account_sync_requirements(
        balances=balances,
        cash_baseline=cash_baseline_equity,
        min_cash_baseline=min_cash_baseline,
        min_assets=min_assets,
    )
    validation = {
        "cash_baseline": cash_baseline_equity,
        "min_cash_baseline": min_cash_baseline,
        "min_assets": min_assets or {},
    }
    if cash_baseline:
        snapshot = build_cash_baseline_snapshot_from_balances(
            balances=balances,
            symbols=symbols,
            quote_asset=settings.quote_asset,
            prices=prices,
        )
        manifest_mode = "paper_cash_baseline_after_forced_liquidation"
    else:
        snapshot = build_paper_snapshot_from_balances(
            balances=balances,
            symbols=symbols,
            quote_asset=settings.quote_asset,
            prices=prices,
            timestamp_ms=timestamp_ms,
            cost_basis_by_symbol=cost_basis_by_symbol,
        )
        manifest_mode = "paper_state_seed_from_real_account"
    cleared_files = clear_simulated_runtime(runtime_dir, archive_root)
    storage_reset: Dict[str, Any] = {}
    try:
        settings_for_storage = load_settings()
        if settings_for_storage.db_read_mode == "prefer_db" or settings_for_storage.db_write_mode != "file":
            storage_reset = build_runtime_store(settings_for_storage).clear_simulated_runtime()
    except Exception as exc:  # noqa: BLE001
        if not load_settings().db_fallback_to_file:
            raise RuntimeError(f"PostgreSQL simulated runtime reset failed: {exc}") from exc
        storage_reset = {
            "storage_source": "postgres_error",
            "warning": str(exc)[:240],
            "cleared_tables": [],
        }
    (runtime_dir / "paper_state.json").write_text(json.dumps(asdict(snapshot), ensure_ascii=True, indent=2), encoding="utf-8")
    write_seed_manifest(
        output_dir=runtime_dir,
        snapshot=snapshot,
        balances=balances,
        prices=prices,
        cost_basis_by_symbol=cost_basis_by_symbol,
        stopped_monitor_pid=None,
        cleared_files=cleared_files,
        mode=manifest_mode,
        validation=validation,
    )
    return {
        "status": "reset_complete",
        "mode": manifest_mode,
        "cleared_files": cleared_files,
        "storage_reset": storage_reset,
        "quote_asset": snapshot.quote_asset,
        "quote_balance": snapshot.quote_balance,
        "initial_quote_balance": snapshot.initial_quote_balance,
        "validation": validation,
        "positions": {
            symbol: {
                "quantity": position.quantity,
                "average_entry_price": position.average_entry_price,
                "cost_basis_source": snapshot.activation_state.get(symbol, {}).get("cost_basis_source", ""),
                "real_average_entry_price": snapshot.activation_state.get(symbol, {}).get("real_average_entry_price", 0.0),
                "seed_price": snapshot.activation_state.get(symbol, {}).get("seed_price", 0.0),
            }
            for symbol, position in snapshot.positions.items()
        },
        "baseline": snapshot.activation_state.get("_baseline", {}),
    }


def build_dashboard_payload(
    runtime_dir: Path,
    chart_interval: str | None = None,
    *,
    include_chart: bool = True,
    order_records_limit: int | None = None,
) -> Dict[str, Any]:
    settings = load_settings()
    dashboard_order_limit = max(1, _coerce_int(os.environ.get("DASHBOARD_TRADE_RECORD_LIMIT"), 200))
    if order_records_limit is None:
        order_records_limit = dashboard_order_limit
    else:
        order_records_limit = max(1, order_records_limit)
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    paper_state = _load_json(runtime_dir / "paper_state.json", {})
    history_path = runtime_dir / "cycle_reports.jsonl"
    history_limit = 800 if include_chart else 80
    profit_history_limit = max(240, history_limit)
    full_history = _read_history(history_path, limit=profit_history_limit)
    history = full_history if include_chart else full_history[-80:] if len(full_history) > 80 else full_history
    profit_history = full_history[-profit_history_limit:] if include_chart else full_history[-profit_history_limit:]
    backtest_payload = _load_backtest_payload(runtime_dir)
    quote_asset = paper_state.get("quote_asset", "JPY")
    fee_rate = _dashboard_fee_rate()

    chart_symbol = _dashboard_chart_symbol(latest_report)
    main_interval = _detect_main_interval(latest_report, backtest_payload["backtest_manifest"])
    selected_chart_interval = _normalize_chart_interval(chart_interval)
    if include_chart and chart_symbol:
        main_bars = _build_live_main_interval_bars(history, symbol=chart_symbol, interval=main_interval, latest_report=latest_report)
        refresh_bars = _build_live_main_interval_bars(history, symbol=chart_symbol, interval="1m", limit=96)
        chart_payload = _build_dashboard_chart_payload(
            runtime_dir,
            latest_report=latest_report,
            history=full_history,
            chart_interval=selected_chart_interval,
            backtest_manifest=backtest_payload["backtest_manifest"],
        )
    else:
        main_bars = []
        refresh_bars = []
        chart_payload = {
            "live_chart_symbol": chart_symbol,
            "live_main_interval": main_interval,
            "live_chart_interval": selected_chart_interval,
            "live_chart_interval_label": _chart_interval_label(selected_chart_interval),
            "live_chart_source": "deferred",
            "live_chart_cache": {"source": "deferred", "cache_hit": False, "cache_policy": "deferred_chart_request"},
            "live_chart_bars": [],
            "live_trade_markers": [],
            "order_markers": [],
            "position_activation_markers": [],
            "live_ai_veto_markers": [],
        }

    paper_fills = paper_state.get("fills", []) if isinstance(paper_state.get("fills", []), list) else []
    scan_lines_for_dashboard = 240 if not include_chart else 800
    cycles_for_dashboard: List[Dict[str, Any]] = []
    trade_records_complete = False
    order_records_source = "file_fallback"
    storage_warning = ""
    all_fills = _dedupe_fills([*paper_fills], limit=order_records_limit)
    recent_fills = _dedupe_fills([*paper_fills], limit=max(100, min(300, order_records_limit)))
    db_order_payload: Dict[str, Any] | None = None
    runtime_store = build_runtime_store(settings)
    if settings.db_read_mode == "prefer_db" and settings.dashboard_order_source == "postgres":
        try:
            db_order_payload = runtime_store.query_order_records(limit=order_records_limit + 50, status="all", symbol=chart_symbol)
            db_fills = _dedupe_fills(db_order_payload.get("recent_fills", []), limit=order_records_limit)
            db_events = _dedupe_order_lifecycle_events(
                db_order_payload.get("order_lifecycle_events", []),
                limit=order_records_limit,
            )
            all_fills = _dedupe_fills([*db_fills, *paper_fills], limit=500)
            recent_fills = _dedupe_fills([*db_fills, *paper_fills], limit=max(100, min(300, order_records_limit)))
            order_records_source = str(db_order_payload.get("storage_source") or "postgres")
            trade_records_complete = bool(db_order_payload.get("trade_records_complete", True))
            all_order_lifecycle_events = db_events if isinstance(db_events, list) else []
            storage_warning = str(db_order_payload.get("storage_warning") or "")
        except Exception as exc:  # noqa: BLE001
            storage_warning = str(exc)[:240]
            if not settings.db_fallback_to_file:
                db_order_payload = {
                    "recent_fills": [],
                    "order_lifecycle_events": [],
                    "trade_records_complete": False,
                    "storage_source": "postgres_error",
                    "storage_warning": storage_warning,
                    "order_records_meta": {
                        "storage_source": "postgres",
                        "error": storage_warning,
                    },
                }
            else:
                all_order_lifecycle_events = []
                trade_records_complete = False

    if order_records_source != "postgres":
        cycles_for_dashboard = _load_recent_cycles_cached(history_path, scan_lines_for_dashboard)
        file_recent_fills = _extract_recent_fills(cycles_for_dashboard, limit=300)
        file_all_fills = _extract_recent_fills(cycles_for_dashboard, limit=500)
        all_fills = _dedupe_fills([*all_fills, *file_all_fills], limit=500)
        recent_fills = _dedupe_fills([*recent_fills, *file_recent_fills], limit=300)
    runtime_config = _dashboard_runtime_config()
    market_prices = latest_report.get("market_prices", {}) if isinstance(latest_report.get("market_prices"), dict) else {}
    open_orders = list((paper_state.get("open_orders") or {}).values()) or latest_report.get("open_orders", [])
    open_orders = _decorate_open_order_spreads(open_orders, market_prices, runtime_config)
    open_order_groups = _build_open_order_groups(open_orders, quote_asset)
    drawer_scan_lines = 800 if not include_chart else 1200
    if order_records_source == "postgres" and db_order_payload is not None:
        all_order_lifecycle_events = all_order_lifecycle_events if isinstance(all_order_lifecycle_events, list) else []
    else:
        if not cycles_for_dashboard:
            cycles_for_dashboard = _load_recent_cycles_cached(history_path, scan_lines_for_dashboard)
        all_order_lifecycle_events = _dedupe_order_lifecycle_events(
            _extract_order_lifecycle_events(cycles_for_dashboard, latest_report, limit=order_records_limit),
            limit=order_records_limit,
        )
        if not all_order_lifecycle_events:
            all_order_lifecycle_events = _dedupe_order_lifecycle_events(
                _extract_order_lifecycle_events_from_file(
                    history_path,
                    limit=order_records_limit,
                    scan_lines=max(1200, len(full_history), order_records_limit),
                ),
                limit=order_records_limit,
            )
    order_lifecycle_events = all_order_lifecycle_events[: min(200, order_records_limit)]
    if order_records_source == "postgres":
        try:
            db_decision_payload = runtime_store.query_decision_ledger(limit=min(200, order_records_limit), symbol=chart_symbol)
            decision_ledger = [item for item in (db_decision_payload.get("decision_ledger") if isinstance(db_decision_payload.get("decision_ledger"), list) else []) if isinstance(item, dict)]
            storage_warning = storage_warning or str(db_decision_payload.get("storage_warning") or "")
        except Exception as exc:  # noqa: BLE001
            decision_ledger = []
            storage_warning = storage_warning or str(exc)[:240]
    else:
        decision_ledger = _extract_decision_ledger(cycles_for_dashboard, latest_report, limit=min(200, order_records_limit))
        if not decision_ledger:
            decision_ledger = _extract_decision_ledger_from_file(
                history_path,
                latest_report,
                limit=min(200, order_records_limit),
                scan_lines=max(1200, len(full_history), order_records_limit),
            )
    if not decision_ledger and not all_order_lifecycle_events:
        decision_ledger = []
    trade_records = _build_trade_records(open_orders, all_fills, all_order_lifecycle_events, quote_asset, fee_rate, limit=order_records_limit)
    real_cost_basis_summary = _build_real_cost_basis_summary(runtime_dir, paper_state, latest_report)
    decision_state_payload = _build_decision_state_payload(paper_state, latest_report, runtime_config)
    target_inventory_payload = _build_target_inventory_payload(latest_report)
    policy_payload = _build_policy_payload(latest_report)
    pair_payload = _build_pair_payload(paper_state)
    boti_pnl_breakdown = {
        "realized_pnl": _coerce_float(paper_state.get("realized_pnl"), _coerce_float(latest_report.get("realized_pnl"))),
        "unrealized_pnl": _coerce_float(latest_report.get("unrealized_pnl")),
        "operation_pnl": _coerce_float(paper_state.get("realized_pnl"), _coerce_float(latest_report.get("realized_pnl"))),
        "mark_to_market_pnl": _coerce_float(latest_report.get("net_pnl")),
        "net_pnl": _coerce_float(paper_state.get("realized_pnl"), _coerce_float(latest_report.get("realized_pnl"))),
        "total_equity": _coerce_float(latest_report.get("total_equity"), _coerce_float(paper_state.get("total_equity"))),
        "baseline": _coerce_float(paper_state.get("initial_quote_balance")),
    }

    return {
        "latest_report": latest_report,
        "paper_state": paper_state,
        "history": history[-80:] if include_chart else [],
        "history_count": len(history),
        "live_profit_curve": _build_live_profit_curve(profit_history),
        "recent_fills": recent_fills,
        "open_orders": open_orders,
        "open_order_groups": open_order_groups,
        "order_ladder_summary": open_order_groups,
        "reserved_quote_balance": _coerce_float(paper_state.get("reserved_quote_balance")),
        "reserved_base_balances": paper_state.get("reserved_base_balances", {}),
        "order_lifecycle_events": order_lifecycle_events,
        "trade_records": trade_records,
        "trade_records_complete": trade_records_complete,
        "real_cost_basis_summary": real_cost_basis_summary,
        "order_records_source": order_records_source,
        "storage_warning": storage_warning,
        "sell_diagnostics": latest_report.get("sell_diagnostics", []),
        "composite_decisions": latest_report.get("composite_decisions", []),
        "policy_decisions": latest_report.get("policy_decisions", []),
        "direction_decisions": latest_report.get("direction_decisions", []),
        "direction_decision": (latest_report.get("direction_decisions", [{}]) or [{}])[0],
        "scenario_decisions": latest_report.get("scenario_decisions", []),
        "scenario_decision": (latest_report.get("scenario_decisions", [{}]) or [{}])[0],
        "external_signal_snapshots": latest_report.get("external_signal_snapshots", []),
        "external_signal_votes": latest_report.get("external_signal_votes", []),
        "external_consensus": latest_report.get("external_consensus", []),
        "external_signal_health": ((latest_report.get("external_consensus", [{}]) or [{}])[0] or {}).get("health", {}),
        "blended_scenario_decisions": latest_report.get("blended_scenario_decisions", []),
        "blended_scenario_decision": (latest_report.get("blended_scenario_decisions", [{}]) or [{}])[0],
        "decision_ledger": decision_ledger,
        "position_activation_state": paper_state.get("activation_state", {}),
        "runtime_config": runtime_config,
        **decision_state_payload,
        **target_inventory_payload,
        **policy_payload,
        **pair_payload,
        "boti_pnl_breakdown": boti_pnl_breakdown,
        "live_main_interval_bars": main_bars,
        "live_refresh_interval": "1m",
        "live_refresh_bars": refresh_bars,
        "chart_interval_options": CHART_INTERVAL_OPTIONS,
        **chart_payload,
        **backtest_payload,
    }


def build_decision_drawer_payload(runtime_dir: Path) -> Dict[str, Any]:
    settings = load_settings()
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    history_path = runtime_dir / "cycle_reports.jsonl"
    scan_lines = _coerce_int(os.environ.get("DASHBOARD_DECISION_DRAWER_SCAN_LINES"), 5000)
    drawer_record_limit = max(1, _coerce_int(os.environ.get("DASHBOARD_DECISION_DRAWER_RECORD_LIMIT"), 200))
    cache_key = _order_records_cache_key(
        runtime_dir,
        "decision_drawer",
        scan_lines,
        drawer_record_limit,
        0,
    )
    cached = _load_order_records_cache(cache_key)
    if cached:
        return cached
    decision_ledger: List[Dict[str, Any]] = []
    order_lifecycle_events: List[Dict[str, Any]] = []
    storage_warning = ""
    order_records_source = "file_fallback"
    if settings.db_read_mode == "prefer_db" and settings.dashboard_order_source == "postgres":
        try:
            db_store = build_runtime_store(settings)
            order_payload = db_store.query_order_records(limit=drawer_record_limit, status="all")
            decision_payload = db_store.query_decision_ledger(limit=drawer_record_limit)
            order_events = order_payload.get("order_lifecycle_events")
            if isinstance(order_events, list):
                order_lifecycle_events = _dedupe_order_lifecycle_events(order_events, limit=drawer_record_limit)
            decision_items = decision_payload.get("decision_ledger")
            if isinstance(decision_items, list):
                decision_ledger = [item for item in decision_items if isinstance(item, dict)]
            order_records_source = str(order_payload.get("storage_source") or "postgres")
            storage_warning = str(order_payload.get("storage_warning") or decision_payload.get("storage_warning") or "")
        except Exception as exc:  # noqa: BLE001
            storage_warning = str(exc)[:240]
            order_records_source = "postgres_error"
            decision_ledger = []
            order_lifecycle_events = []
    if order_records_source != "postgres":
        cycles = _load_recent_cycles_cached(history_path, scan_lines)
        decision_ledger = _extract_decision_ledger(cycles, latest_report, limit=drawer_record_limit)
        order_lifecycle_events = _dedupe_order_lifecycle_events(
            _extract_order_lifecycle_events(cycles, latest_report, limit=drawer_record_limit),
            limit=drawer_record_limit,
        )
        if not decision_ledger:
            decision_ledger = _extract_decision_ledger_from_file(
                history_path,
                latest_report,
                limit=drawer_record_limit,
                scan_lines=max(scan_lines, 1200),
            )
        if not order_lifecycle_events:
            order_lifecycle_events = _dedupe_order_lifecycle_events(
                _extract_order_lifecycle_events_from_file(
                    history_path,
                    limit=drawer_record_limit,
                    scan_lines=max(scan_lines, 1200),
                ),
                limit=drawer_record_limit,
            )
    payload = {
        "decision_ledger": decision_ledger,
        "order_lifecycle_events": order_lifecycle_events,
        "external_signal_snapshots": latest_report.get("external_signal_snapshots", []),
        "external_signal_votes": latest_report.get("external_signal_votes", []),
        "external_consensus": latest_report.get("external_consensus", []),
        "external_signal_health": ((latest_report.get("external_consensus", [{}]) or [{}])[0] or {}).get("health", {}),
        "blended_scenario_decisions": latest_report.get("blended_scenario_decisions", []),
        "blended_scenario_decision": (latest_report.get("blended_scenario_decisions", [{}]) or [{}])[0],
        "decision_drawer_meta": {
            "scan_lines": scan_lines,
            "decision_ledger_count": len(decision_ledger),
            "order_lifecycle_event_count": len(order_lifecycle_events),
            "storage_source": order_records_source,
            "trade_records_complete": order_records_source == "postgres",
        },
        "storage_source": order_records_source,
        "storage_warning": storage_warning,
        "trade_records_complete": order_records_source == "postgres",
    }
    _write_order_records_cache(cache_key, payload)
    return payload


def build_order_records_payload(
    runtime_dir: Path,
    status: str = "all",
    *,
    limit: int = 1000,
    offset: int = 0,
) -> Dict[str, Any]:
    settings = load_settings()
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    paper_state = _load_json(runtime_dir / "paper_state.json", {})
    history_path = runtime_dir / "cycle_reports.jsonl"
    quote_asset = paper_state.get("quote_asset", "JPY")
    fee_rate = _dashboard_fee_rate()
    open_orders = list((paper_state.get("open_orders") or {}).values()) or latest_report.get("open_orders", [])
    normalized_status = (status or "all").lower()
    normalized_limit = max(1, min(5000, _coerce_int(limit, 1000)))
    normalized_offset = max(0, _coerce_int(offset, 0))
    cache_scan_lines = _coerce_int(os.environ.get("DASHBOARD_ORDER_SCAN_LINES"), 2500)
    cache_key = _order_records_cache_key(
        runtime_dir,
        normalized_status,
        cache_scan_lines,
        normalized_limit,
        normalized_offset,
        include_runtime_signature=not (settings.db_read_mode == "prefer_db" and settings.dashboard_order_source == "postgres"),
    )
    cached = _load_order_records_cache(cache_key)
    if cached:
        return cached
    storage_warning = ""
    if settings.db_read_mode == "prefer_db" and settings.dashboard_order_source == "postgres":
        try:
            db_payload = build_runtime_store(settings).query_order_records(limit=normalized_limit + normalized_offset, status=normalized_status)
            recent_fills = _dedupe_fills(db_payload.get("recent_fills", []), limit=normalized_limit + normalized_offset)
            order_lifecycle_events = _dedupe_order_lifecycle_events(
                db_payload.get("order_lifecycle_events", []),
                limit=normalized_limit + normalized_offset,
            )
            trade_records = _build_trade_records(open_orders, recent_fills, order_lifecycle_events, quote_asset, fee_rate, limit=None)
            if normalized_status != "all":
                trade_records = _filter_records_by_status(trade_records, normalized_status)
            trade_records = trade_records[normalized_offset:normalized_offset + normalized_limit]
            meta = dict(db_payload.get("order_records_meta") or {})
            meta.update(
                {
                    "recent_fill_count": len(recent_fills),
                    "order_lifecycle_event_count": len(order_lifecycle_events),
                    "trade_record_count": len(trade_records),
                    "limit": normalized_limit,
                    "offset": normalized_offset,
                }
            )
            payload = {
                "recent_fills": recent_fills,
                "order_lifecycle_events": order_lifecycle_events,
                "trade_records": trade_records,
                "trade_records_complete": True,
                "storage_source": "postgres",
                "order_records_meta": meta,
            }
            _write_order_records_cache(cache_key, payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            storage_warning = str(exc)[:240]
            if not settings.db_fallback_to_file:
                payload = {
                    "recent_fills": [],
                    "order_lifecycle_events": [],
                    "trade_records": [],
                    "trade_records_complete": False,
                    "storage_source": "postgres_error",
                    "storage_warning": storage_warning,
                    "order_records_meta": {
                        "storage_source": "postgres",
                        "error": storage_warning,
                    },
                }
                _write_order_records_cache(cache_key, payload)
                return payload
    else:
        storage_warning = ""
    cycles = _load_recent_cycles_cached(history_path, cache_scan_lines)
    scan_limit = normalized_limit + normalized_offset
    order_lifecycle_events = _dedupe_order_lifecycle_events(
        _extract_order_lifecycle_events(cycles, latest_report, limit=scan_limit),
        limit=scan_limit,
    )
    paper_fills = paper_state.get("fills", []) if isinstance(paper_state.get("fills", []), list) else []
    file_fills = _extract_recent_fills(cycles, limit=scan_limit)
    recent_fills = _dedupe_fills([*paper_fills, *file_fills], limit=scan_limit)
    trade_records = _build_trade_records(open_orders, recent_fills, order_lifecycle_events, quote_asset, fee_rate, limit=None)
    if normalized_status != "all":
        trade_records = _filter_records_by_status(trade_records, normalized_status)
    trade_records = trade_records[normalized_offset:normalized_offset + normalized_limit]
    payload = {
        "recent_fills": recent_fills,
        "order_lifecycle_events": order_lifecycle_events,
        "trade_records": trade_records,
        "trade_records_complete": True,
        "storage_source": "file_fallback" if storage_warning else "file",
        "storage_warning": storage_warning,
        "order_records_meta": {
            "scan_lines": cache_scan_lines,
            "recent_fill_count": len(recent_fills),
            "order_lifecycle_event_count": len(order_lifecycle_events),
            "trade_record_count": len(trade_records),
            "limit": normalized_limit,
            "offset": normalized_offset,
        },
    }
    _write_order_records_cache(cache_key, payload)
    return payload


def build_ops_storage_payload(runtime_dir: Path) -> Dict[str, Any]:
    settings = load_settings()
    status = build_runtime_store(settings).storage_status()
    status.update(
        {
            "runtime_dir": str(runtime_dir),
            "dashboard_order_source": settings.dashboard_order_source,
            "json_compat_files": {
                "latest_report": (runtime_dir / "latest_report.json").exists(),
                "paper_state": (runtime_dir / "paper_state.json").exists(),
                "cycle_reports": (runtime_dir / "cycle_reports.jsonl").exists(),
            },
        }
    )
    return status


class DashboardHandler(BaseHTTPRequestHandler):
    runtime_dir: Path

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/dashboard":
            query = parse_qs(parsed.query)
            requested_interval = query.get("chart_interval", [None])[0]
            include_chart = str(query.get("include_chart", ["true"])[0]).lower() not in {"0", "false", "no"}
            self._send_json(build_dashboard_payload(self.runtime_dir, chart_interval=requested_interval, include_chart=include_chart))
            return
        if parsed.path == "/api/dashboard/chart":
            query = parse_qs(parsed.query)
            requested_interval = query.get("chart_interval", [None])[0]
            self._send_json(_build_dashboard_chart_payload(self.runtime_dir, chart_interval=requested_interval))
            return
        if parsed.path == "/api/dashboard/decision-drawer":
            self._send_json(build_decision_drawer_payload(self.runtime_dir))
            return
        if parsed.path == "/api/dashboard/orders":
            query = parse_qs(parsed.query)
            status = str((query.get("status") or ["all"])[0])
            limit = _coerce_int((query.get("limit") or ["1000"])[0], 1000)
            offset = _coerce_int((query.get("offset") or ["0"])[0], 0)
            self._send_json(build_order_records_payload(self.runtime_dir, status=status, limit=limit, offset=offset))
            return
        if parsed.path == "/api/ops/health":
            self._send_json(build_ops_health_payload(self.runtime_dir))
            return
        if parsed.path == "/api/ops/storage":
            self._send_json(build_ops_storage_payload(self.runtime_dir))
            return
        if parsed.path == "/api/ops/summary":
            query = parse_qs(parsed.query)
            hours = _coerce_int((query.get("hours") or ["8"])[0], 8)
            self._send_json(build_ops_summary_payload(self.runtime_dir, hours))
            return
        if parsed.path == "/api/ops/pairs":
            query = parse_qs(parsed.query)
            hours = _coerce_int((query.get("hours") or ["24"])[0], 24)
            self._send_json(build_ops_pairs_payload(self.runtime_dir, hours))
            return
        if parsed.path == "/api/ops/orders":
            query = parse_qs(parsed.query)
            status = str((query.get("status") or ["all"])[0])
            self._send_json(build_ops_orders_payload(self.runtime_dir, status))
            return
        if parsed.path == "/api/ops/trades":
            self._send_json(build_ops_trades_payload(self.runtime_dir, parse_qs(parsed.query)))
            return
        if parsed.path == "/api/ops/version":
            query = parse_qs(parsed.query)
            refresh = str((query.get("refresh") or ["false"])[0]).lower() in {"1", "true", "yes", "on"}
            self._send_json(build_ops_version_payload(self.runtime_dir, refresh=refresh))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/ops/reset-paper":
            if not self._ops_write_authorized():
                self._send_json(
                    {
                        "status": "FORBIDDEN",
                        "reason": "ops_write_token_required",
                    },
                    status=HTTPStatus.FORBIDDEN,
                )
                return
            payload = self._read_json_body()
            archive_root_raw = str(payload.get("archive_root") or "runtime_resets").strip()
            archive_root = Path(archive_root_raw) if archive_root_raw else None
            cash_baseline = bool(payload.get("cash_baseline") or payload.get("force_cash_baseline"))
            min_cash_baseline = _coerce_float(payload.get("min_cash_baseline"), 0.0)
            raw_min_assets = payload.get("require_asset_min") or payload.get("min_assets") or []
            if isinstance(raw_min_assets, str):
                raw_min_assets = [raw_min_assets]
            min_assets = parse_asset_minimums(raw_min_assets)
            try:
                self._send_json(
                    _seed_paper_from_real_account(
                        self.runtime_dir,
                        archive_root=archive_root,
                        cash_baseline=cash_baseline,
                        min_cash_baseline=min_cash_baseline,
                        min_assets=min_assets,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self._send_json(
                    {
                        "status": "ERROR",
                        "reason": str(exc),
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _read_json_body(self) -> Dict[str, Any]:
        length = _coerce_int(self.headers.get("Content-Length"))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _ops_write_authorized(self) -> bool:
        token = _ops_write_token()
        if not token:
            return False
        provided = (
            self.headers.get("X-Botinance-Ops-Token")
            or self.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        return bool(provided and provided == token)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve local trading dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--output-dir", default="runtime")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    runtime_dir = Path(args.output_dir)

    class ConfiguredHandler(DashboardHandler):
        pass

    ConfiguredHandler.runtime_dir = runtime_dir
    server = ThreadingHTTPServer((args.host, args.port), ConfiguredHandler)
    print(f"Dashboard serving on http://{args.host}:{args.port} using {runtime_dir}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
