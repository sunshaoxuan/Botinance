from __future__ import annotations

import argparse
import csv
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


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
      display: grid;
      grid-template-columns: 64px minmax(0, 1fr);
      height: 100vh;
    }

    .side-rail {
      border-right: 1px solid var(--line);
      background: rgba(250, 252, 255, 0.82);
      backdrop-filter: blur(12px);
      padding: 12px 9px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
      box-shadow: none;
      z-index: 2;
    }

    .brand-mark {
      width: 36px;
      height: 36px;
      border-radius: 8px;
      display: grid;
      place-items: center;
      color: #fff;
      font-weight: 800;
      letter-spacing: 0.08em;
      background: linear-gradient(150deg, #1f3f6d, #2f5e91);
      box-shadow: 0 8px 16px rgba(31, 63, 109, 0.18);
    }

    .rail-divider {
      width: 28px;
      height: 1px;
      background: var(--line);
      margin: 2px 0;
    }

    .rail-button {
      width: 38px;
      height: 38px;
      border: 1px solid transparent;
      border-radius: 8px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font-weight: 760;
      font-family: var(--mono);
      transition: all 160ms ease;
    }

    .rail-button:hover {
      color: var(--blue);
      background: var(--blue-soft);
    }

    .rail-button.active {
      color: var(--blue);
      border-color: rgba(37, 99, 235, 0.18);
      background: #fff;
      box-shadow: 0 1px 4px rgba(17, 27, 43, 0.04);
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
      grid-template-columns: repeat(4, minmax(110px, 1fr));
      gap: 7px;
      flex: 1;
      max-width: 720px;
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
      height: 506px;
      padding: 10px;
      background: #fff;
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

    .bottom-insight-grid {
      display: grid;
      grid-template-columns: 1.05fr 1fr 1fr;
      gap: 12px;
      margin-top: 12px;
      align-items: start;
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

    .code {
      font-family: var(--mono);
      font-size: 12px;
      color: var(--ink-soft);
    }

    .nowrap { white-space: nowrap; }

    @media (max-width: 1180px) {
      .trade-workspace,
      .backtest-grid,
      .page-grid-2 {
        grid-template-columns: 1fr;
      }

      .right-command-stack {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }

      .bottom-insight-grid,
      .page-grid-3 {
        grid-template-columns: 1fr;
      }
    }

    @media (max-width: 900px) {
      body { overflow: auto; }

      .app-shell {
        display: block;
        height: auto;
      }

      .side-rail {
        position: sticky;
        top: 0;
        flex-direction: row;
        justify-content: center;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }

      .rail-divider { display: none; }

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
    <aside class="side-rail" aria-label="主导航">
      <div class="brand-mark">B</div>
      <div class="rail-divider"></div>
      <button class="rail-button active" data-rail-tab="trading" title="实时交易">T</button>
      <button class="rail-button" data-rail-tab="ai" title="AI 决策">A</button>
      <button class="rail-button" data-rail-tab="backtest" title="回测分析">B</button>
      <button class="rail-button" data-rail-tab="risk" title="风险控制">R</button>
      <button class="rail-button" data-rail-tab="system" title="系统日志">L</button>
    </aside>

    <main class="workspace">
      <header class="top-bar">
        <div class="top-title">
          <div>
            <div class="top-kicker">XRPJPY PAPER TRADING</div>
            <div class="top-name">Botinance</div>
          </div>
          <div class="market-pill"><span class="market-dot"></span><span id="topSymbol">XRPJPY</span></div>
        </div>
        <div class="top-metrics">
          <div class="top-metric"><span>运行状态</span><strong id="topMode">读取中</strong></div>
          <div class="top-metric"><span>最新刷新</span><strong id="topUpdated">--</strong></div>
          <div class="top-metric"><span>当前价格</span><strong id="topPrice">--</strong></div>
          <div class="top-metric"><span>模拟权益</span><strong id="topEquity">--</strong></div>
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
          <article class="panel chart-card">
            <div class="panel-header">
              <div>
                <div class="panel-title">主周期行情</div>
                <div class="panel-subtitle" id="chartSubtitle">K 线、成交量、成交点、AI 否决点、退出线</div>
              </div>
              <div class="tool-row">
                <span class="tool-pill blue" id="chartInterval">主周期 --</span>
                <span class="tool-pill green">PAPER</span>
                <span class="tool-pill coral" id="chartPointCount">0 bars</span>
              </div>
            </div>
            <div class="chart-frame">
              <canvas id="tradeChart"></canvas>
            </div>
          </article>

          <aside class="right-command-stack">
            <div class="card" id="positionCard"></div>
            <div class="card" id="pnlCard"></div>
            <div class="card" id="sellDecisionCard"></div>
            <div class="card" id="riskGateCard"></div>
            <div class="card" id="executionCard"></div>
          </aside>
        </div>

        <div class="bottom-insight-grid">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">证据来源</div><div class="panel-subtitle">本轮决策引用的信息</div></div></div>
            <div class="panel-body" id="evidenceCompact"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">AI 决策时间线</div><div class="panel-subtitle">规则信号之后的裁决链路</div></div></div>
            <div class="panel-body" id="aiTimeline"></div>
          </article>
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">最近模拟成交</div><div class="panel-subtitle">仅展示 PAPER_FILLED</div></div></div>
            <div class="panel-body" id="fillsCompact"></div>
          </article>
        </div>
      </section>

      <section class="tab-panel" id="tab-ai">
        <div class="page-grid-2">
          <article class="panel">
            <div class="panel-header"><div><div class="panel-title">GPT-5.5 市场判断</div><div class="panel-subtitle">分析结论、市场状态与风险提示</div></div></div>
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
            <div class="panel-header"><div><div class="panel-title">退出风险线</div><div class="panel-subtitle">止损、止盈、跟踪止损、超时退出</div></div></div>
            <div class="panel-body" id="exitRiskCard"></div>
          </article>
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

  <script>
    const refreshMs = 2000;
    let activeTab = "trading";
    let lastPayloadSnapshot = null;

    const els = {};
    const ids = [
      "topSymbol", "topMode", "topUpdated", "topPrice", "topEquity", "chartSubtitle", "chartInterval", "chartPointCount",
      "positionCard", "pnlCard", "sellDecisionCard", "riskGateCard", "executionCard", "evidenceCompact", "aiTimeline", "fillsCompact",
      "aiSummaryCard", "ruleVsAiCard", "evidenceFull", "aiRiskFull", "btTotalReturn", "btMaxDrawdown", "btWinRate",
      "btProfitFactor", "btExpectancy", "btTradeCount", "btSourceLabel", "btSegments", "btTrades", "btManifest",
      "buyDecisionFull", "exitRiskCard", "riskParametersCard", "systemStateCard", "schedulingFull", "payloadHealthCard",
      "cycleLedger"
    ];

    function cacheEls() {
      ids.forEach((id) => { els[id] = document.getElementById(id); });
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
      if (s === "PAPER_FILLED") return "模拟成交";
      if (s === "BLOCKED") return "已阻塞";
      return signal || "--";
    }

    function executionLabel(status) {
      const s = String(status || "").toUpperCase();
      if (s === "SKIPPED_REFRESH_ONLY") return "刷新观察";
      if (s === "PAPER_FILLED") return "模拟成交";
      if (s === "BLOCKED") return "已阻塞";
      if (s === "NO_ACTION") return "无动作";
      if (s === "PASS") return "通过";
      if (s === "HOLD") return "持有";
      return status || "--";
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

    function kvRows(rows) {
      const html = rows.map(([k, v]) => `
        <div class="kv-row"><span>${escapeHtml(k)}</span><strong>${v}</strong></div>
      `).join("");
      return `<div class="kv-grid">${html}</div>`;
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
      const currentPrice = asNumber(primary.current_price || primary.mark_price || (latest.market_prices || {})[symbol] || (payload.live_main_interval_bars || []).slice(-1)[0]?.close, 0);
      const decision = (latest.decisions || [])[0] || {};
      const strategy = decision.strategy_decision || {};
      const rawSignal = strategy.signal || decision.action || decision.signal?.action || decision.signal || latest.signal?.action || latest.signal || "HOLD";
      const signal = typeof rawSignal === "object" ? (rawSignal.action || rawSignal.signal || "HOLD") : rawSignal;
      const execution = decision.execution_result || latest.execution_result || {};
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
      const bars = payload.live_main_interval_bars || [];
      const markers = payload.live_trade_markers || [];
      const vetoes = payload.live_ai_veto_markers || [];
      return { latest, paper, primary, symbol, position, quoteAsset, currentPrice, decision, strategy, signal, executionStatus, executionReason, llm, aiRisk, buyDiag, sellDiag, positionDiag, activationState, schedule, fills, bars, markers, vetoes };
    }

    function activateTab(tabName) {
      activeTab = tabName;
      document.querySelectorAll(".tab-button").forEach((btn) => btn.classList.toggle("active", btn.dataset.tab === tabName));
      document.querySelectorAll(".rail-button").forEach((btn) => btn.classList.toggle("active", btn.dataset.railTab === tabName));
      document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `tab-${tabName}`));
      window.requestAnimationFrame(() => redrawCharts(lastPayloadSnapshot));
    }

    function updateTopBar(payload) {
      const c = context(payload);
      els.topSymbol.textContent = c.symbol;
      els.topMode.textContent = cycleModeLabel(c.latest.cycle_mode);
      els.topUpdated.textContent = fmtTime(c.latest.generated_at || c.latest.timestamp || c.latest.timestamp_ms || payload.generated_at);
      els.topPrice.textContent = c.currentPrice ? fmtCurrency(c.currentPrice, c.quoteAsset) : "--";
      els.topEquity.textContent = fmtCurrency(c.paper.total_equity, c.quoteAsset);
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
      const realUnrealized = qty > 0 && realAvg > 0 && c.currentPrice > 0 ? qty * (c.currentPrice - realAvg) : botiUnrealized;
      const realTotalEquity = realAvg > 0 && qty > 0 && c.currentPrice > 0
        ? asNumber(c.paper.quote_balance, 0) + qty * c.currentPrice
        : c.paper.total_equity;
      const realized = asNumber(c.paper.realized_pnl, 0);
      const riskLines = activeRiskLines(c);
      const allowEntry = c.aiRisk.allow_entry;
      const executionResult = c.executionStatus || "无执行";

      els.chartSubtitle.textContent = `${c.symbol} 主周期 K 线、成交量、模拟成交点、AI 否决点、退出线`;
      els.chartInterval.textContent = `主周期 ${payload.live_main_interval || "--"}`;
      els.chartPointCount.textContent = `${c.bars.length} bars`;

      els.positionCard.innerHTML = `
        <div class="card-label"><span>当前持仓</span>${qty > 0 ? statusChip("持仓中", "buy") : statusChip("空仓", "wait")}</div>
        <div class="card-value">${qty > 0 ? `${fmtNumber(qty, 6)} XRP` : "0 XRP"}</div>
        <div class="card-note">真实成本 ${realAvg ? fmtCurrency(realAvg, c.quoteAsset) : "--"}，Boti接管价 ${avg ? fmtCurrency(avg, c.quoteAsset) : "--"}，最高价 ${fmtCurrency(highest, c.quoteAsset)}</div>
      `;

      els.pnlCard.innerHTML = `
        <div class="card-label"><span>真实仓位盈亏</span><span class="${pnlClass(realUnrealized)}">未实现</span></div>
        <div class="card-value ${pnlClass(realUnrealized)}">${fmtCurrency(realUnrealized, c.quoteAsset)}</div>
        <div class="card-note">Boti接管后 ${fmtCurrency(botiUnrealized, c.quoteAsset)}，模拟已实现 ${fmtCurrency(realized, c.quoteAsset)}，当前市值 ${fmtCurrency(realTotalEquity, c.quoteAsset)}</div>
      `;

      els.sellDecisionCard.innerHTML = `
        <div class="card-label"><span>卖出判断</span>${c.sellDiag.eligible_to_sell ? statusChip("可卖", "sell") : statusChip("持有", "wait")}</div>
        <div class="card-value">${c.sellDiag.eligible_to_sell ? fmtNumber(c.sellDiag.recommended_sell_quantity, 8) : "HOLD"}</div>
        <div class="card-note">${escapeHtml(c.sellDiag.blocker || "暂无卖出诊断")}；网格 ${escapeHtml(c.sellDiag.activation_trigger || c.activationState.last_trigger || "--")}</div>
      `;

      els.riskGateCard.innerHTML = `
        <div class="card-label"><span>AI 风险闸门</span>${allowEntry === false ? statusChip("否决", "block") : statusChip("允许/未触发", "wait")}</div>
        ${stateBlock(allowEntry === false ? "否决" : "通过", allowEntry === false ? "BLOCK" : "PASS")}
        <div class="card-note">${escapeHtml(c.aiRisk.reason || c.aiRisk.veto_reason || c.aiRisk.summary || "暂无 AI 风险闸门输出")}</div>
      `;

      els.executionCard.innerHTML = `
        <div class="card-label"><span>执行状态</span>${statusChip(signalLabel(c.signal), signalClass(c.signal))}</div>
        ${stateBlock(executionLabel(executionResult), executionResult)}
        <div class="card-note">止损 ${riskLines.stopLoss || "--"}，止盈 ${riskLines.takeProfit || "--"}，跟踪 ${riskLines.trailingStop || "--"}</div>
      `;

      els.evidenceCompact.innerHTML = renderEvidence(c.latest, 5);
      els.aiTimeline.innerHTML = renderAiTimeline(c);
      els.fillsCompact.innerHTML = renderFills(c.fills, c.quoteAsset, 6);
    }

    function updateAiTab(payload) {
      const c = context(payload);
      const actionBias = c.llm.action_bias_cn || c.llm.action_bias || c.llm.recommendation || c.llm.action || "未输出";
      const marketState = c.llm.regime_cn || c.llm.market_state || c.llm.market_regime || c.llm.summary_cn || c.llm.summary || "暂无市场状态";
      const riskNote = c.llm.risk_note_cn || c.llm.risk_note || c.llm.risk_summary || c.aiRisk.reason || c.aiRisk.veto_reason || "暂无风险提示";
      const ruleSignal = c.signal;
      const aiVerdict = c.aiRisk.allow_entry === false ? "AI 否决入场" : "AI 未否决";

      els.aiSummaryCard.innerHTML = kvRows([
        ["模型", escapeHtml(c.llm.model || c.latest.llm_model || "GPT-5.5 / 兼容端点")],
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

      els.btTotalReturn.textContent = fmtPercent(metrics.total_return_pct);
      els.btMaxDrawdown.textContent = fmtPercent(metrics.max_drawdown_pct);
      els.btWinRate.textContent = fmtPercent(metrics.win_rate);
      els.btProfitFactor.textContent = fmtNumber(metrics.profit_factor, 3);
      els.btExpectancy.textContent = fmtNumber(metrics.expectancy_per_trade, 4);
      els.btTradeCount.textContent = `${fmtNumber(metrics.trade_count, 0)} / ${fmtNumber(metrics.completed_trade_count, 0)}`;
      els.btSourceLabel.textContent = available ? `数据源 ${source}` : "缺失 runtime_backtest_walk / runtime_backtest_check";

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
      els.btSegments.innerHTML = table(["段", "训练窗", "测试窗", "收益", "回撤", "胜率", "优于基线"], segmentRows, "暂无 walk-forward segment 文件");

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
      els.btTrades.innerHTML = table(["开平仓", "价格", "盈亏", "收益率", "持仓", "MFE/MAE", "退出"], tradeRows, "暂无回测交易明细");

      const manifest = payload.backtest_manifest || {};
      els.btManifest.innerHTML = Object.keys(manifest).length ? kvRows(Object.entries(flattenObject(manifest)).slice(0, 20).map(([k, v]) => [k, escapeHtml(String(v))])) : emptyBox("暂无 run_manifest.json");
    }

    function updateRiskTab(payload) {
      const c = context(payload);
      const buy = c.buyDiag || {};
      const sell = c.sellDiag || {};
      const activation = c.activationState || {};
      const risk = activeRiskLines(c);

      els.buyDecisionFull.innerHTML = kvRows([
        ["规则信号", statusChip(signalLabel(c.signal), signalClass(c.signal))],
        ["Quote 预算", escapeHtml(fmtCurrency(buy.quote_budget || buy.budget_quote, c.quoteAsset))],
        ["最小成交额", escapeHtml(fmtCurrency(buy.min_notional || buy.min_notional_required, c.quoteAsset))],
        ["原始数量", escapeHtml(fmtNumber(buy.raw_quantity, 8))],
        ["取整数量", escapeHtml(fmtNumber(buy.rounded_quantity || buy.order_quantity || buy.adjusted_quantity, 8))],
        ["是否可下单", escapeHtml(boolLabel(buy.can_place_order ?? buy.eligible_to_buy))]
      ]);

      els.exitRiskCard.innerHTML = kvRows([
        ["是否适合卖出", sell.eligible_to_sell ? statusChip("可卖", "sell") : statusChip("持有", "wait")],
        ["建议卖出数量", escapeHtml(fmtNumber(sell.recommended_sell_quantity, 8))],
        ["卖出原因", escapeHtml(sell.blocker || "--")],
        ["止损线", risk.stopLoss || "--"],
        ["止盈线", risk.takeProfit || "--"],
        ["跟踪止损", risk.trailingStop || "--"],
        ["持仓 K 线数", escapeHtml(fmtNumber(sell.bars_held ?? (c.position || {}).hold_bars, 0))],
        ["触发状态", escapeHtml(c.positionDiag.exit_trigger || c.positionDiag.exit_reason || "未触发")]
      ]);

      els.riskParametersCard.innerHTML = kvRows([
        ["最近触发", escapeHtml(activation.last_trigger || sell.activation_trigger || "--")],
        ["待回补数量", escapeHtml(fmtNumber(activation.pending_buyback_quantity, 8))],
        ["最近网格卖价", escapeHtml(fmtCurrency(activation.last_grid_sell_price, c.quoteAsset))],
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
      const ledger = payload.decision_ledger || [];
      const schedule = c.schedule || {};

      els.systemStateCard.innerHTML = kvRows([
        ["Cycle Mode", escapeHtml(cycleModeLabel(c.latest.cycle_mode))],
        ["本轮时间", escapeHtml(fmtTime(c.latest.generated_at || c.latest.timestamp || c.latest.timestamp_ms))],
        ["历史周期", escapeHtml(String(history.length))],
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
        <td>${escapeHtml(r.symbol || "--")}</td>
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
      ];
      return `<div class="timeline">${items.map((item) => `
        <div class="timeline-item">
          <div class="timeline-time">${escapeHtml(item[0])}</div>
          <div class="timeline-title">${escapeHtml(item[1])}</div>
          <div class="timeline-body">${escapeHtml(item[2])}</div>
        </div>
      `).join("")}</div>`;
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

    function renderFills(fills, quoteAsset, limit) {
      const rows = fills.slice(0, limit).map((f) => {
        const side = String(f.side || f.action || "").toUpperCase();
        return `<tr>
          <td>${statusChip(side || "--", side === "BUY" ? "buy" : side === "SELL" ? "sell" : "wait")}</td>
          <td>${fmtNumber(f.quantity, 8)}</td>
          <td>${fmtCurrency(f.price || f.fill_price, quoteAsset)}</td>
          <td>${fmtCurrency(f.fee, quoteAsset)}</td>
          <td class="${pnlClass(f.realized_pnl)}">${fmtCurrency(f.realized_pnl, quoteAsset)}</td>
          <td class="nowrap">${escapeHtml(fmtTime(f.timestamp || f.timestamp_ms || f.time))}</td>
        </tr>`;
      });
      return table(["方向", "数量", "价格", "手续费", "已实现", "时间"], rows, "暂无模拟成交");
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

    function updateDom(payload) {
      lastPayloadSnapshot = payload;
      updateTopBar(payload);
      updateTradingTab(payload);
      updateAiTab(payload);
      updateBacktestTab(payload);
      updateRiskTab(payload);
      updateSystemTab(payload);
      redrawCharts(payload);
    }

    function redrawCharts(payload) {
      if (!payload) return;
      if (activeTab === "trading") {
        const c = context(payload);
        drawCandlestickChart(document.getElementById("tradeChart"), c.bars, {
          markers: c.markers,
          vetoes: c.vetoes,
          riskLines: activeRiskLines(c).numeric,
          quoteAsset: c.quoteAsset
        });
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
      const data = (bars || []).filter((b) => Number.isFinite(Number(b.close))).slice(-160);
      if (!data.length) {
        drawEmptyChart(canvas, "等待主周期 K 线数据");
        return;
      }

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
      const candleW = Math.max(3, Math.min(13, barSlot * 0.58));
      const maxVol = Math.max(...data.map((b) => asNumber(b.sample_count || b.volume || 1, 1)), 1);

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
        const vol = asNumber(b.sample_count || b.volume || 1, 1);
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

      drawRiskLine(ctx, options.riskLines?.stop_loss_price, y, pad.left, width - pad.right, "#b4232a", "止损");
      drawRiskLine(ctx, options.riskLines?.take_profit_price, y, pad.left, width - pad.right, "#15803d", "止盈");
      drawRiskLine(ctx, options.riskLines?.trailing_stop_price, y, pad.left, width - pad.right, "#c96a21", "跟踪");

      const indexByTime = buildTimeIndex(data);
      (options.markers || []).forEach((m) => drawTradeMarker(ctx, m, indexByTime, data.length, x, y));
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
        ctx.fillText(fmtShortTime(data[idx].time || data[idx].open_time), x(idx), height - 12);
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

    async function loadData() {
      const response = await fetch("/api/dashboard", { cache: "no-store" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    async function tick() {
      try {
        const payload = await loadData();
        updateDom(payload);
      } catch (err) {
        console.error(err);
        els.topMode.textContent = "数据读取失败";
      }
    }

    function wireTabs() {
      document.querySelectorAll("[data-tab]").forEach((btn) => {
        btn.addEventListener("click", () => activateTab(btn.dataset.tab));
      });
      document.querySelectorAll("[data-rail-tab]").forEach((btn) => {
        btn.addEventListener("click", () => activateTab(btn.dataset.railTab));
      });
      window.addEventListener("resize", () => redrawCharts(lastPayloadSnapshot));
    }

    cacheEls();
    wireTabs();
    activateTab("trading");
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


def _read_history(path: Path, limit: int = 6000) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    rows: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows[-limit:]


def _extract_recent_fills(history: List[Dict[str, Any]], limit: int = 12) -> List[Dict[str, Any]]:
    fills: List[Dict[str, Any]] = []
    for cycle in history:
        for decision in cycle.get("decisions", []):
            execution = decision.get("execution_result", {})
            if execution.get("status") == "PAPER_FILLED":
                fills.append(execution)
    return fills[-limit:][::-1]


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
    return markers[-limit:]


def _extract_position_activation_markers(history: List[Dict[str, Any]], limit: int = 200) -> List[Dict[str, Any]]:
    activation_triggers = {"grid_profit_sell", "grid_loss_recovery_sell", "grid_buyback"}
    markers = [
        marker
        for marker in _extract_live_trade_markers(history, limit=limit * 2)
        if marker.get("trigger") in activation_triggers
    ]
    return markers[-limit:]


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


def _extract_decision_ledger(history: List[Dict[str, Any]], latest_report: Dict[str, Any], limit: int = 200) -> List[Dict[str, Any]]:
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
    for directory in candidates:
        summary = _load_json(directory / "summary.json", {})
        if not summary:
            continue
        return {
            "backtest_available": True,
            "backtest_source": directory.name,
            "backtest_summary": summary,
            "backtest_segments": _load_json(directory / "segments.json", []),
            "backtest_equity_curve": _load_csv_rows(directory / "equity_curve.csv"),
            "backtest_trades": _load_csv_rows(directory / "trades.csv"),
            "backtest_manifest": _load_json(directory / "run_manifest.json", {}),
        }

    return {
        "backtest_available": False,
        "backtest_source": None,
        "backtest_summary": {},
        "backtest_segments": [],
        "backtest_equity_curve": [],
        "backtest_trades": [],
        "backtest_manifest": {},
    }


def build_dashboard_payload(runtime_dir: Path) -> Dict[str, Any]:
    latest_report = _load_json(runtime_dir / "latest_report.json", {})
    paper_state = _load_json(runtime_dir / "paper_state.json", {})
    history = _read_history(runtime_dir / "cycle_reports.jsonl")
    backtest_payload = _load_backtest_payload(runtime_dir)

    chart_symbol = ""
    if latest_report.get("decisions"):
        chart_symbol = latest_report["decisions"][0].get("symbol", "")
    if not chart_symbol and latest_report.get("market_prices"):
        chart_symbol = next(iter(latest_report["market_prices"]))

    main_interval = _detect_main_interval(latest_report, backtest_payload["backtest_manifest"])
    bars = (
        _build_live_main_interval_bars(history, symbol=chart_symbol, interval=main_interval, latest_report=latest_report)
        if chart_symbol
        else []
    )

    return {
        "latest_report": latest_report,
        "paper_state": paper_state,
        "history": history,
        "recent_fills": _extract_recent_fills(history),
        "sell_diagnostics": latest_report.get("sell_diagnostics", []),
        "decision_ledger": _extract_decision_ledger(history, latest_report),
        "position_activation_state": paper_state.get("activation_state", {}),
        "live_chart_symbol": chart_symbol,
        "live_main_interval": main_interval,
        "live_main_interval_bars": bars,
        "live_trade_markers": _extract_live_trade_markers(history),
        "position_activation_markers": _extract_position_activation_markers(history),
        "live_ai_veto_markers": _extract_live_ai_veto_markers(history),
        **backtest_payload,
    }


class DashboardHandler(BaseHTTPRequestHandler):
    runtime_dir: Path

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/dashboard":
            self._send_json(build_dashboard_payload(self.runtime_dir))
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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
