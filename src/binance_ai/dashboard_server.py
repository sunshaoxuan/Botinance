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
  <title>Binance AI 模拟交易看板</title>
  <style>
    :root {
      --bg: #f4efe7;
      --panel: rgba(255,255,255,0.78);
      --panel-strong: rgba(255,255,255,0.92);
      --ink: #1f2022;
      --muted: #6c655d;
      --line: rgba(31,32,34,0.08);
      --accent: #d36b2d;
      --accent-soft: rgba(211,107,45,0.14);
      --green: #1d7f52;
      --red: #b13f2f;
      --blue: #2f6fd0;
      --shadow: 0 18px 45px rgba(78, 52, 24, 0.10);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", "Helvetica Neue", sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(211,107,45,0.16), transparent 28%),
        radial-gradient(circle at right 20%, rgba(29,127,82,0.12), transparent 18%),
        linear-gradient(180deg, #f7f2eb 0%, #f1e8dd 100%);
      min-height: 100vh;
    }

    .shell {
      width: min(1400px, calc(100vw - 32px));
      margin: 22px auto 40px;
    }

    .hero {
      display: grid;
      grid-template-columns: 1.4fr 0.8fr;
      gap: 18px;
      align-items: stretch;
      margin-bottom: 18px;
    }

    .hero-card, .panel {
      background: var(--panel);
      backdrop-filter: blur(14px);
      border: 1px solid rgba(255,255,255,0.7);
      border-radius: 24px;
      box-shadow: var(--shadow);
    }

    .hero-card {
      padding: 28px 30px;
      position: relative;
      overflow: hidden;
    }

    .hero-card::after {
      content: "";
      position: absolute;
      width: 240px;
      height: 240px;
      right: -60px;
      top: -60px;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(211,107,45,0.18) 0%, rgba(211,107,45,0) 68%);
      pointer-events: none;
    }

    .eyebrow {
      font-size: 12px;
      letter-spacing: 0.16em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 10px;
    }

    h1 {
      margin: 0 0 8px;
      font-size: clamp(30px, 4vw, 44px);
      line-height: 1.02;
      letter-spacing: -0.03em;
    }

    .subtitle {
      font-size: 15px;
      color: var(--muted);
      max-width: 62ch;
      line-height: 1.5;
    }

    .status-stack {
      display: grid;
      gap: 12px;
    }

    .status-card {
      padding: 20px 22px;
    }

    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      font-size: 13px;
      background: var(--panel-strong);
      border: 1px solid var(--line);
    }

    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--green);
      box-shadow: 0 0 0 6px rgba(29,127,82,0.12);
    }

    .view-rail {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 18px;
      margin-bottom: 18px;
    }

    .view-buttons {
      display: inline-flex;
      gap: 8px;
      padding: 6px;
      border-radius: 999px;
      background: rgba(255,255,255,0.52);
      border: 1px solid rgba(31,32,34,0.06);
    }

    .view-button {
      border: 0;
      border-radius: 999px;
      background: transparent;
      color: var(--muted);
      padding: 10px 16px;
      font: inherit;
      font-size: 14px;
      cursor: pointer;
      transition: background 120ms ease, color 120ms ease;
    }

    .view-button.active {
      background: var(--accent);
      color: white;
    }

    .view {
      display: none;
    }

    .view.active {
      display: block;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }

    .metric {
      padding: 18px 18px 16px;
    }

    .metric-label {
      font-size: 12px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.12em;
      margin-bottom: 10px;
    }

    .metric-value {
      font-size: clamp(24px, 3vw, 34px);
      letter-spacing: -0.04em;
      font-weight: 700;
    }

    .metric-sub {
      font-size: 13px;
      color: var(--muted);
      margin-top: 8px;
    }

    .good { color: var(--green); }
    .bad { color: var(--red); }

    .grid {
      display: grid;
      grid-template-columns: 1.3fr 1.3fr 1fr;
      gap: 18px;
    }

    .panel {
      padding: 20px;
      min-height: 180px;
    }

    .panel h2 {
      margin: 0 0 12px;
      font-size: 18px;
      letter-spacing: -0.02em;
    }

    .chart-wrap {
      height: 300px;
      border-radius: 18px;
      background: linear-gradient(180deg, rgba(255,255,255,0.9), rgba(255,255,255,0.45));
      border: 1px solid rgba(31,32,34,0.06);
      padding: 14px;
    }

    canvas {
      width: 100%;
      height: 100%;
      display: block;
    }

    .table-scroll {
      overflow: auto;
    }

    .table {
      width: 100%;
      border-collapse: collapse;
    }

    .table th, .table td {
      padding: 11px 0;
      border-bottom: 1px solid var(--line);
      text-align: left;
      font-size: 14px;
      vertical-align: top;
    }

    .table th {
      color: var(--muted);
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.1em;
      white-space: nowrap;
      padding-right: 12px;
    }

    .badge {
      display: inline-flex;
      align-items: center;
      padding: 5px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 600;
      background: var(--accent-soft);
      color: var(--accent);
    }

    .signal-buy { background: rgba(29,127,82,0.12); color: var(--green); }
    .signal-sell { background: rgba(177,63,47,0.12); color: var(--red); }
    .signal-hold { background: rgba(31,32,34,0.08); color: var(--ink); }

    .kicker {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 10px;
      gap: 10px;
    }

    .mini {
      font-size: 13px;
      color: var(--muted);
    }

    .empty {
      padding: 18px 0;
      color: var(--muted);
      font-size: 14px;
    }

    .footer-note {
      margin-top: 18px;
      color: var(--muted);
      font-size: 13px;
    }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(6, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 18px;
    }

    @media (max-width: 1280px) {
      .metrics, .summary-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }

    @media (max-width: 1120px) {
      .hero, .grid, .metrics, .summary-grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <div class="shell">
    <section class="hero">
      <div class="hero-card">
        <div class="eyebrow">模拟交易控制台</div>
        <h1>同一页看实时监控，也看回测结论。</h1>
        <div class="subtitle">
          这个看板会自动刷新，把实时主周期 K 线、模拟成交、退出监控线、AI 否决标记，以及 P6 回测结果统一收口到一个页面里。
        </div>
      </div>
      <div class="status-stack">
        <div class="hero-card status-card">
          <div class="kicker">
            <div class="status-pill">
              <span class="dot"></span>
              <span id="modePill">加载中</span>
            </div>
            <div class="mini" id="lastUpdated">等待数据中</div>
          </div>
          <div class="metric-label">监控交易对</div>
          <div class="metric-value" id="activeSymbols">0</div>
          <div class="metric-sub" id="quoteAssetLabel">计价资产</div>
        </div>
        <div class="hero-card status-card">
          <div class="metric-label">最新策略说明</div>
          <div style="font-size:15px; line-height:1.5; color:var(--muted)" id="latestReason">
            等待周期数据中。
          </div>
        </div>
      </div>
    </section>

    <section class="panel view-rail">
      <div class="view-buttons">
        <button class="view-button active" id="liveTab" type="button">实时监控</button>
        <button class="view-button" id="backtestTab" type="button">回测分析</button>
      </div>
      <div class="mini" id="viewHint">优先展示实时主线；回测结果随时可切换查看。</div>
    </section>

    <section class="view active" id="liveView">
      <section class="metrics">
        <div class="panel metric">
          <div class="metric-label">最新价格</div>
          <div class="metric-value" id="marketPrice">-</div>
          <div class="metric-sub" id="marketPriceLabel">当前交易对标记价格</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">总权益</div>
          <div class="metric-value" id="totalEquity">-</div>
          <div class="metric-sub">模拟账户按市值计价</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">净收益</div>
          <div class="metric-value" id="netPnl">-</div>
          <div class="metric-sub">总权益减去初始资金</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">已实现收益</div>
          <div class="metric-value" id="realizedPnl">-</div>
          <div class="metric-sub">仅统计已平仓模拟交易</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">未实现收益</div>
          <div class="metric-value" id="unrealizedPnl">-</div>
          <div class="metric-sub">按当前市价估算持仓盈亏</div>
        </div>
      </section>

      <section class="grid">
        <div class="panel">
          <div class="kicker">
            <h2>主周期 K 线</h2>
            <div class="mini" id="priceStats">暂无数据点</div>
          </div>
          <div class="chart-wrap"><canvas id="priceChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>权益曲线</h2>
            <div class="mini" id="equityStats">暂无数据点</div>
          </div>
          <div class="chart-wrap"><canvas id="equityChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>当前持仓与退出监控</h2>
            <div class="mini" id="positionCount">0 个持仓</div>
          </div>
          <div id="positions"></div>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel">
          <div class="kicker">
            <h2>净收益曲线</h2>
            <div class="mini" id="pnlStats">暂无数据点</div>
          </div>
          <div class="chart-wrap"><canvas id="pnlChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>最新信号</h2>
            <div class="mini">当前周期决策</div>
          </div>
          <div id="signals"></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>最近模拟成交</h2>
            <div class="mini">仅显示真实 PAPER_FILLED</div>
          </div>
          <div id="fills"></div>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel" style="grid-column: 1 / -1;">
          <div class="kicker">
            <h2>多时间框架结构</h2>
            <div class="mini">15m 入场动量 / 1h 主决策 / 4h 趋势过滤</div>
          </div>
          <div id="marketStructure"></div>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel">
          <div class="kicker">
            <h2>账户快照</h2>
            <div class="mini">来自模拟账户状态</div>
          </div>
          <table class="table">
            <tbody id="snapshot"></tbody>
          </table>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>证据来源</h2>
            <div class="mini">本轮抓到的新闻与公告</div>
          </div>
          <div id="evidence"></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>AI 分析</h2>
            <div class="mini" id="aiStatus">等待分析结果</div>
          </div>
          <table class="table">
            <tbody id="aiAnalysis"></tbody>
          </table>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel" style="grid-column: 1 / -1;">
          <div class="kicker">
            <h2>AI 风险闸门</h2>
            <div class="mini">AI 只能否决或缩仓，不能强制开仓</div>
          </div>
          <div id="aiRiskGate"></div>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel" style="grid-column: 1 / -1;">
          <div class="kicker">
            <h2>买入决策链路</h2>
            <div class="mini">逐步展示当前为什么可以买或不能买</div>
          </div>
          <div id="buyDecision"></div>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel" style="grid-column: 1 / -1;">
          <div class="kicker">
            <h2>决策调度状态</h2>
            <div class="mini">区分刷新轮、决策轮，以及触发原因</div>
          </div>
          <div id="scheduling"></div>
        </div>
      </section>
    </section>

    <section class="view" id="backtestView">
      <section class="summary-grid">
        <div class="panel metric">
          <div class="metric-label">总收益率</div>
          <div class="metric-value" id="btTotalReturn">-</div>
          <div class="metric-sub">P6 标准 summary.json</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">最大回撤</div>
          <div class="metric-value" id="btMaxDrawdown">-</div>
          <div class="metric-sub">聚合权益曲线最大回撤</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">胜率</div>
          <div class="metric-value" id="btWinRate">-</div>
          <div class="metric-sub">已平仓交易口径</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">Profit Factor</div>
          <div class="metric-value" id="btProfitFactor">-</div>
          <div class="metric-sub">总盈利 / 总亏损绝对值</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">单笔期望</div>
          <div class="metric-value" id="btExpectancy">-</div>
          <div class="metric-sub">expectancy_per_trade</div>
        </div>
        <div class="panel metric">
          <div class="metric-label">交易数量</div>
          <div class="metric-value" id="btTradeCount">-</div>
          <div class="metric-sub" id="btTradeCountSub">总交易 / 已平仓</div>
        </div>
      </section>

      <section class="grid">
        <div class="panel">
          <div class="kicker">
            <h2>回测权益曲线</h2>
            <div class="mini" id="btEquityStats">等待回测结果</div>
          </div>
          <div class="chart-wrap"><canvas id="btEquityChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>回测回撤曲线</h2>
            <div class="mini" id="btDrawdownStats">等待回测结果</div>
          </div>
          <div class="chart-wrap"><canvas id="btDrawdownChart"></canvas></div>
        </div>
        <div class="panel">
          <div class="kicker">
            <h2>回测运行快照</h2>
            <div class="mini" id="btSourceLabel">等待回测结果</div>
          </div>
          <table class="table">
            <tbody id="btManifest"></tbody>
          </table>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel" style="grid-column: 1 / -1;">
          <div class="kicker">
            <h2>Walk-forward Segment 对比</h2>
            <div class="mini">优先展示 runtime_backtest_walk；缺失时回退到单次回测目录</div>
          </div>
          <div class="table-scroll" id="btSegments"></div>
        </div>
      </section>

      <section class="grid" style="margin-top:18px">
        <div class="panel" style="grid-column: 1 / -1;">
          <div class="kicker">
            <h2>交易明细</h2>
            <div class="mini">P6 标准 trades.csv</div>
          </div>
          <div class="table-scroll" id="btTrades"></div>
        </div>
      </section>
    </section>

    <div class="footer-note">
      页面每 5 秒自动刷新一次。保持监控进程运行，新的价格点和交易结果才会持续出现；回测视图只消费现有 P6 文件，不会在浏览器里重跑回测。
    </div>
  </div>

  <script>
    const refreshMs = 5000;
    let activeView = null;
    let userPinnedView = false;
    let lastPayloadSnapshot = null;

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toLocaleString("zh-CN", {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
    }

    function fmtPercent(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return `${fmtNumber(value, digits)}%`;
    }

    function fmtCurrency(value, asset) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return `${fmtNumber(value, 2)} ${asset || ""}`.trim();
    }

    function fmtTime(ts) {
      if (!ts) return "-";
      return new Date(ts).toLocaleString("zh-CN");
    }

    function fmtShortTime(ts) {
      if (!ts) return "-";
      return new Date(ts).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
    }

    function classForPnl(value) {
      if (Number(value) > 0) return "good";
      if (Number(value) < 0) return "bad";
      return "";
    }

    function signalClass(action) {
      const key = String(action || "").toLowerCase();
      return `badge signal-${key || "hold"}`;
    }

    function structureStateLabel(value) {
      const key = String(value || "").toLowerCase();
      if (key === "uptrend") return "上升趋势";
      if (key === "downtrend") return "下降趋势";
      if (key === "above") return "快线在上";
      if (key === "below") return "快线在下";
      if (key === "flat") return "均线走平";
      if (key === "insufficient") return "数据不足";
      if (key === "trend_unknown") return "趋势未知";
      return value || "-";
    }

    function signalLabel(action) {
      const key = String(action || "").toUpperCase();
      if (key === "BUY") return "买入";
      if (key === "SELL") return "卖出";
      if (key === "HOLD") return "持有";
      return key || "-";
    }

    function cycleModeLabel(mode) {
      const key = String(mode || "").toUpperCase();
      if (key === "DECISION") return "决策轮";
      if (key === "REFRESH") return "刷新轮";
      if (key === "MIXED") return "混合轮";
      return key || "-";
    }

    function renderTableRows(container, rows, emptyText) {
      if (!rows.length) {
        container.innerHTML = `<div class="empty">${emptyText}</div>`;
        return;
      }
      container.innerHTML = rows.join("");
    }

    function activateView(nextView, fromUser = false) {
      const viewChanged = activeView !== nextView;
      if (fromUser) {
        userPinnedView = true;
      }
      activeView = nextView;
      const isLive = nextView === "live";
      document.getElementById("liveView").classList.toggle("active", isLive);
      document.getElementById("backtestView").classList.toggle("active", !isLive);
      document.getElementById("liveTab").classList.toggle("active", isLive);
      document.getElementById("backtestTab").classList.toggle("active", !isLive);
      if (viewChanged && lastPayloadSnapshot) {
        window.requestAnimationFrame(() => {
          updateLiveDom(lastPayloadSnapshot);
          updateBacktestDom(lastPayloadSnapshot);
        });
      }
    }

    function drawLineChart(canvas, points, color, fillColor, options = {}) {
      const ctx = canvas.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, width, height);

      if (!points.length) {
        ctx.fillStyle = "rgba(108,101,93,0.85)";
        ctx.font = '14px "Avenir Next", sans-serif';
        ctx.fillText("等待监控数据中", 16, height / 2);
        return;
      }

      const values = points.map((point) => point.value);
      let min = Math.min(...values);
      let max = Math.max(...values);
      if (min === max) {
        min -= 1;
        max += 1;
      }

      const labelFormatter = options.labelFormatter || ((value) => fmtNumber(value, 2));
      const leftPad = 14;
      const rightPad = 72;
      const topPad = 18;
      const bottomPad = 32;
      const innerW = width - leftPad - rightPad;
      const innerH = height - topPad - bottomPad;

      ctx.strokeStyle = "rgba(31,32,34,0.10)";
      ctx.lineWidth = 1;
      for (let index = 0; index <= 3; index += 1) {
        const y = topPad + (innerH / 3) * index;
        ctx.beginPath();
        ctx.moveTo(leftPad, y);
        ctx.lineTo(leftPad + innerW, y);
        ctx.stroke();
      }

      const xy = points.map((point, index) => {
        const x = leftPad + (points.length === 1 ? innerW / 2 : (innerW * index) / (points.length - 1));
        const y = topPad + innerH - ((point.value - min) / (max - min)) * innerH;
        return { x, y };
      });

      ctx.beginPath();
      ctx.moveTo(xy[0].x, topPad + innerH);
      xy.forEach(({ x, y }) => ctx.lineTo(x, y));
      ctx.lineTo(xy[xy.length - 1].x, topPad + innerH);
      ctx.closePath();
      ctx.fillStyle = fillColor;
      ctx.fill();

      ctx.beginPath();
      xy.forEach(({ x, y }, index) => {
        if (index === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2.5;
      ctx.stroke();

      const last = xy[xy.length - 1];
      ctx.beginPath();
      ctx.arc(last.x, last.y, 4.5, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();

      ctx.fillStyle = "rgba(108,101,93,0.95)";
      ctx.font = '12px "Avenir Next", sans-serif';
      ctx.textAlign = "right";
      ctx.fillText(labelFormatter(max), width - 8, topPad + 4);
      ctx.fillText(labelFormatter((max + min) / 2), width - 8, topPad + innerH / 2 + 4);
      ctx.fillText(labelFormatter(min), width - 8, topPad + innerH + 4);

      const latestValueText = labelFormatter(points[points.length - 1].value);
      const latestLabelWidth = Math.max(44, ctx.measureText(latestValueText).width + 16);
      const latestLabelX = width - latestLabelWidth - 10;
      const latestLabelY = Math.max(topPad + 4, Math.min(last.y - 14, topPad + innerH - 24));
      ctx.fillStyle = "rgba(255,255,255,0.92)";
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(latestLabelX, latestLabelY, latestLabelWidth, 22, 10);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.textAlign = "center";
      ctx.fillText(latestValueText, latestLabelX + latestLabelWidth / 2, latestLabelY + 15);

      ctx.fillStyle = "rgba(108,101,93,0.95)";
      ctx.textAlign = "left";
      ctx.fillText(fmtShortTime(points[0].timestamp_ms), leftPad, height - 8);
      ctx.textAlign = "right";
      ctx.fillText(fmtShortTime(points[points.length - 1].timestamp_ms), leftPad + innerW, height - 8);
    }

    function drawCandlestickChart(canvas, bars, options = {}) {
      const ctx = canvas.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
      ctx.scale(dpr, dpr);
      ctx.clearRect(0, 0, width, height);

      if (!bars.length) {
        ctx.fillStyle = "rgba(108,101,93,0.85)";
        ctx.font = '14px "Avenir Next", sans-serif';
        ctx.fillText("等待主周期 K 线中", 16, height / 2);
        return;
      }

      const highs = bars.map((bar) => Number(bar.high));
      const lows = bars.map((bar) => Number(bar.low));
      let min = Math.min(...lows);
      let max = Math.max(...highs);
      if (min === max) {
        min -= 1;
        max += 1;
      }

      const leftPad = 14;
      const rightPad = 78;
      const topPad = 18;
      const bottomPad = 32;
      const innerW = width - leftPad - rightPad;
      const innerH = height - topPad - bottomPad;
      const labelFormatter = options.labelFormatter || ((value) => fmtNumber(value, 2));
      const intervalLabel = options.intervalLabel || "";

      const mapY = (value) => topPad + innerH - ((Number(value) - min) / (max - min)) * innerH;
      const stepX = bars.length === 1 ? innerW : innerW / Math.max(bars.length - 1, 1);
      const bodyWidth = Math.max(4, Math.min(16, stepX * 0.52));

      ctx.strokeStyle = "rgba(31,32,34,0.10)";
      ctx.lineWidth = 1;
      for (let index = 0; index <= 3; index += 1) {
        const y = topPad + (innerH / 3) * index;
        ctx.beginPath();
        ctx.moveTo(leftPad, y);
        ctx.lineTo(leftPad + innerW, y);
        ctx.stroke();
      }

      const positions = bars.map((bar, index) => ({
        x: leftPad + (bars.length === 1 ? innerW / 2 : stepX * index),
        bar,
      }));

      positions.forEach(({ x, bar }) => {
        const openY = mapY(bar.open);
        const closeY = mapY(bar.close);
        const highY = mapY(bar.high);
        const lowY = mapY(bar.low);
        const isUp = Number(bar.close) >= Number(bar.open);
        const bodyColor = isUp ? "#1d7f52" : "#b13f2f";

        ctx.strokeStyle = "rgba(31,32,34,0.55)";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(x, highY);
        ctx.lineTo(x, lowY);
        ctx.stroke();

        const bodyTop = Math.min(openY, closeY);
        const bodyHeight = Math.max(2, Math.abs(closeY - openY));
        ctx.fillStyle = bodyColor;
        ctx.fillRect(x - bodyWidth / 2, bodyTop, bodyWidth, bodyHeight);
      });

      const lineDefinitions = [
        { label: "止损", color: "#b13f2f", value: options.stopLossPrice },
        { label: "止盈", color: "#1d7f52", value: options.takeProfitPrice },
        { label: "跟踪", color: "#d36b2d", value: options.trailingStopPrice },
      ].filter((item) => item.value !== null && item.value !== undefined && Number(item.value) > 0);

      lineDefinitions.forEach((line) => {
        const y = mapY(line.value);
        ctx.save();
        ctx.setLineDash([6, 4]);
        ctx.strokeStyle = line.color;
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(leftPad, y);
        ctx.lineTo(leftPad + innerW, y);
        ctx.stroke();
        ctx.restore();

        ctx.fillStyle = "rgba(255,255,255,0.92)";
        ctx.strokeStyle = line.color;
        ctx.lineWidth = 1;
        const text = `${line.label} ${labelFormatter(line.value)}`;
        const textWidth = Math.max(58, ctx.measureText(text).width + 14);
        const boxX = width - textWidth - 10;
        const boxY = Math.max(topPad + 4, Math.min(y - 10, topPad + innerH - 24));
        ctx.beginPath();
        ctx.roundRect(boxX, boxY, textWidth, 20, 10);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = line.color;
        ctx.font = '11px "Avenir Next", sans-serif';
        ctx.textAlign = "center";
        ctx.fillText(text, boxX + textWidth / 2, boxY + 13);
      });

      const markerIndexForTime = (timestampMs) => {
        let bestIndex = 0;
        let bestDistance = Infinity;
        bars.forEach((bar, index) => {
          const start = Number(bar.open_time || bar.timestamp_ms || 0);
          const end = Number(bar.close_time || bar.timestamp_ms || start);
          if (timestampMs >= start && timestampMs <= end) {
            bestIndex = index;
            bestDistance = 0;
            return;
          }
          const distance = Math.min(Math.abs(timestampMs - start), Math.abs(timestampMs - end));
          if (distance < bestDistance) {
            bestDistance = distance;
            bestIndex = index;
          }
        });
        return bestIndex;
      };

      (options.tradeMarkers || []).forEach((marker) => {
        const index = markerIndexForTime(Number(marker.timestamp_ms || 0));
        const x = positions[index].x;
        const y = mapY(marker.price || positions[index].bar.close);
        const isBuy = String(marker.side || "").toUpperCase() === "BUY";
        const color = isBuy ? "#1d7f52" : "#b13f2f";
        ctx.fillStyle = color;
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.4;
        ctx.beginPath();
        if (isBuy) {
          ctx.moveTo(x, y - 10);
          ctx.lineTo(x - 7, y + 3);
          ctx.lineTo(x + 7, y + 3);
        } else {
          ctx.moveTo(x, y + 10);
          ctx.lineTo(x - 7, y - 3);
          ctx.lineTo(x + 7, y - 3);
        }
        ctx.closePath();
        ctx.fill();
      });

      (options.vetoMarkers || []).forEach((marker) => {
        const index = markerIndexForTime(Number(marker.timestamp_ms || 0));
        const x = positions[index].x;
        const y = mapY(marker.price || positions[index].bar.close);
        ctx.strokeStyle = "#d36b2d";
        ctx.lineWidth = 1.8;
        ctx.beginPath();
        ctx.arc(x, y - 12, 6, 0, Math.PI * 2);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(x - 4, y - 16);
        ctx.lineTo(x + 4, y - 8);
        ctx.moveTo(x + 4, y - 16);
        ctx.lineTo(x - 4, y - 8);
        ctx.stroke();
      });

      ctx.fillStyle = "rgba(108,101,93,0.95)";
      ctx.font = '12px "Avenir Next", sans-serif';
      ctx.textAlign = "right";
      ctx.fillText(labelFormatter(max), width - 8, topPad + 4);
      ctx.fillText(labelFormatter((max + min) / 2), width - 8, topPad + innerH / 2 + 4);
      ctx.fillText(labelFormatter(min), width - 8, topPad + innerH + 4);

      const lastBar = bars[bars.length - 1];
      const latestValueText = labelFormatter(lastBar.close);
      const latestY = mapY(lastBar.close);
      const latestLabelWidth = Math.max(44, ctx.measureText(latestValueText).width + 16);
      const latestLabelX = width - latestLabelWidth - 10;
      const latestLabelY = Math.max(topPad + 4, Math.min(latestY - 14, topPad + innerH - 24));
      ctx.fillStyle = "rgba(255,255,255,0.92)";
      ctx.strokeStyle = "#2f6fd0";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.roundRect(latestLabelX, latestLabelY, latestLabelWidth, 22, 10);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#2f6fd0";
      ctx.textAlign = "center";
      ctx.fillText(latestValueText, latestLabelX + latestLabelWidth / 2, latestLabelY + 15);

      ctx.fillStyle = "rgba(108,101,93,0.95)";
      ctx.textAlign = "left";
      ctx.fillText(fmtShortTime(Number(bars[0].open_time)), leftPad, height - 8);
      const rightLabel = intervalLabel ? `${fmtShortTime(Number(lastBar.close_time))} · ${intervalLabel}` : fmtShortTime(Number(lastBar.close_time));
      ctx.textAlign = "right";
      ctx.fillText(rightLabel, leftPad + innerW, height - 8);
    }

    async function loadData() {
      const response = await fetch("/api/dashboard");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    function updateLiveDom(payload) {
      const latest = payload.latest_report || {};
      const state = payload.paper_state || {};
      const history = payload.history || [];
      const fills = payload.recent_fills || [];
      const evidence = latest.news_evidence || [];
      const marketSnapshots = latest.market_snapshots || [];
      const buyDiagnostics = latest.buy_diagnostics || [];
      const aiRiskAssessments = latest.ai_risk_assessments || [];
      const schedulingDiagnostics = latest.scheduling_diagnostics || [];
      const quoteAsset = state.quote_asset || "JPY";
      const llm = latest.llm_analysis || {};
      const symbols = Object.keys(latest.market_prices || {});
      const primarySymbol = payload.live_chart_symbol || symbols[0] || ((latest.decisions || [])[0] || {}).symbol || "SYMBOL";
      const currentPrice = (latest.market_prices || {})[primarySymbol];
      const liveBars = (payload.live_main_interval_bars || []).filter((bar) => bar.symbol === primarySymbol);
      const liveTradeMarkers = (payload.live_trade_markers || []).filter((marker) => marker.symbol === primarySymbol);
      const liveAiVetoMarkers = (payload.live_ai_veto_markers || []).filter((marker) => marker.symbol === primarySymbol);
      const currentPosition = (latest.position_diagnostics || []).find((item) => item.symbol === primarySymbol) || null;

      document.getElementById("modePill").textContent = `${latest.simulation_mode ? "模拟模式" : "实盘模式"} · ${cycleModeLabel(latest.cycle_mode)}`;
      document.getElementById("lastUpdated").textContent = `最近周期：${fmtTime(latest.timestamp_ms)}`;
      document.getElementById("activeSymbols").textContent = String((latest.decisions || []).length);
      document.getElementById("quoteAssetLabel").textContent = `计价资产：${quoteAsset}`;

      const latestDecision = (latest.decisions || [])[0];
      document.getElementById("latestReason").textContent = latest.cycle_reason || (latestDecision ? latestDecision.signal.reason : "当前还没有决策。");

      document.getElementById("marketPrice").textContent = fmtCurrency(currentPrice, quoteAsset);
      document.getElementById("marketPriceLabel").textContent = `${primarySymbol} 当前标记价格`;

      const totalEquity = document.getElementById("totalEquity");
      totalEquity.textContent = fmtCurrency(latest.total_equity, quoteAsset);

      const netPnl = document.getElementById("netPnl");
      netPnl.textContent = fmtCurrency(latest.net_pnl, quoteAsset);
      netPnl.className = `metric-value ${classForPnl(latest.net_pnl)}`;

      const realized = document.getElementById("realizedPnl");
      realized.textContent = fmtCurrency(latest.realized_pnl, quoteAsset);
      realized.className = `metric-value ${classForPnl(latest.realized_pnl)}`;

      const unrealized = document.getElementById("unrealizedPnl");
      unrealized.textContent = fmtCurrency(latest.unrealized_pnl, quoteAsset);
      unrealized.className = `metric-value ${classForPnl(latest.unrealized_pnl)}`;

      const positions = latest.position_diagnostics || [];
      document.getElementById("positionCount").textContent = `${positions.length} 个持仓`;
      renderTableRows(
        document.getElementById("positions"),
        positions.map((pos) => `
          <table class="table">
            <tr><th>交易对</th><td>${pos.symbol}</td></tr>
            <tr><th>数量</th><td>${fmtNumber(pos.quantity, 4)}</td></tr>
            <tr><th>持仓均价</th><td>${fmtCurrency(pos.average_entry_price, quoteAsset)}</td></tr>
            <tr><th>当前价格</th><td>${fmtCurrency(pos.mark_price, quoteAsset)}</td></tr>
            <tr><th>浮动盈亏</th><td class="${classForPnl(pos.unrealized_pnl)}">${fmtCurrency(pos.unrealized_pnl, quoteAsset)}</td></tr>
            <tr><th>最高价</th><td>${fmtCurrency(pos.highest_price, quoteAsset)}</td></tr>
            <tr><th>止损线</th><td>${fmtCurrency(pos.stop_loss_price, quoteAsset)}</td></tr>
            <tr><th>止盈线</th><td>${fmtCurrency(pos.take_profit_price, quoteAsset)}</td></tr>
            <tr><th>跟踪止损线</th><td>${fmtCurrency(pos.trailing_stop_price, quoteAsset)}</td></tr>
            <tr><th>持仓根数</th><td>${pos.bars_held} 根 K 线</td></tr>
            <tr><th>退出状态</th><td>${pos.exit_watch_reason}</td></tr>
          </table>
        `),
        "当前没有模拟持仓。"
      );

      renderTableRows(
        document.getElementById("signals"),
        [`<div class="table-scroll"><table class="table">
          <thead><tr><th>交易对</th><th>动作</th><th>结构</th><th>置信度</th><th>原因</th></tr></thead>
          <tbody>
          ${(latest.decisions || []).map((decision) => `
            <tr>
              <td>${decision.symbol}</td>
              <td><span class="${signalClass(decision.signal.action)}">${signalLabel(decision.signal.action)}</span></td>
              <td>${decision.signal.regime || "-"}</td>
              <td>${fmtNumber((decision.signal.confidence || 0) * 100, 1)}%</td>
              <td>${decision.signal.reason}</td>
            </tr>`).join("")}
          </tbody>
        </table></div>`],
        "当前还没有信号记录。"
      );

      renderTableRows(
        document.getElementById("marketStructure"),
        marketSnapshots.length ? [`<div class="table-scroll"><table class="table">
          <thead>
            <tr>
              <th>交易对</th>
              <th>当前结构</th>
              <th>1h 主决策</th>
              <th>15m 入场</th>
              <th>4h 趋势</th>
              <th>24根涨跌</th>
              <th>长周期涨跌</th>
            </tr>
          </thead>
          <tbody>
            ${marketSnapshots.map((item) => `
              <tr>
                <td>${item.symbol}<br><span class="mini">最新 ${fmtCurrency(item.last_price, quoteAsset)}</span></td>
                <td>${structureStateLabel(item.signal_regime)}</td>
                <td>
                  ${signalLabel(item.signal_action)}<br>
                  <span class="mini">
                    快线 ${fmtNumber(item.ma_fast, 4)} / 慢线 ${fmtNumber(item.ma_slow, 4)}
                  </span>
                </td>
                <td>
                  ${structureStateLabel((item.entry_interval_summary || {}).state)}<br>
                  <span class="mini">
                    快线 ${fmtNumber((item.entry_interval_summary || {}).fast_ma, 4)} /
                    慢线 ${fmtNumber((item.entry_interval_summary || {}).slow_ma, 4)}
                  </span>
                </td>
                <td>
                  ${structureStateLabel((item.trend_interval_summary || {}).state)}<br>
                  <span class="mini">
                    快线 ${fmtNumber((item.trend_interval_summary || {}).fast_ma, 4)} /
                    慢线 ${fmtNumber((item.trend_interval_summary || {}).slow_ma, 4)}
                  </span>
                </td>
                <td class="${classForPnl(item.change_24_pct)}">${fmtPercent(item.change_24_pct, 2)}</td>
                <td class="${classForPnl(item.change_long_pct)}">${fmtPercent(item.change_long_pct, 2)}</td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>`] : [],
        "当前还没有多时间框架结构数据。"
      );

      renderTableRows(
        document.getElementById("fills"),
        fills.length ? [`<div class="table-scroll"><table class="table">
          <thead><tr><th>时间</th><th>交易对</th><th>方向</th><th>数量</th><th>成交价</th><th>盈亏</th></tr></thead>
          <tbody>
          ${fills.map((fill) => `
            <tr>
              <td>${fmtTime(fill.timestamp_ms)}</td>
              <td>${fill.symbol}</td>
              <td><span class="${signalClass(fill.side)}">${signalLabel(fill.side)}</span></td>
              <td>${fmtNumber(fill.quantity, 4)}</td>
              <td>${fmtCurrency(fill.fill_price, quoteAsset)}</td>
              <td class="${classForPnl(fill.realized_pnl_delta)}">${fmtCurrency(fill.realized_pnl_delta, quoteAsset)}</td>
            </tr>
          `).join("")}
          </tbody>
        </table></div>`] : [],
        "当前还没有模拟成交。"
      );

      const snapshotRows = [
        ["主交易对", primarySymbol],
        ["主周期", payload.live_main_interval || "-"],
        ["最新价格", fmtCurrency(currentPrice, quoteAsset)],
        ["计价资产余额", fmtCurrency(state.quote_balance, quoteAsset)],
        ["初始资金", fmtCurrency(state.initial_quote_balance, quoteAsset)],
        ["已实现收益", fmtCurrency(state.realized_pnl, quoteAsset)],
        ["周期类型", cycleModeLabel(latest.cycle_mode)],
        ["周期原因", latest.cycle_reason || "-"],
        ["新闻层状态", latest.news_refresh_status || "-"],
        ["新闻下次刷新", fmtTime(latest.news_next_refresh_ms)],
        ["历史周期数", String(history.length)],
      ].map(([label, value]) => `<tr><th>${label}</th><td>${value}</td></tr>`);
      document.getElementById("snapshot").innerHTML = snapshotRows.join("");

      const evidenceRows = evidence.slice(0, 8).map((item) => `
        <tr>
          <td>${item.source}</td>
          <td>${item.title}<br><span class="mini">${item.matched_keywords && item.matched_keywords.length ? item.matched_keywords.join(" / ") : "未命中关键词"}</span></td>
        </tr>
      `);
      renderTableRows(
        document.getElementById("evidence"),
        evidenceRows.length ? [`<div class="table-scroll"><table class="table">
          <thead><tr><th>来源</th><th>标题</th></tr></thead>
          <tbody>${evidenceRows.join("")}</tbody>
        </table></div>`] : [],
        "当前还没有抓到相关证据。"
      );

      const aiRows = [
        ["状态", llm.status || "未启用"],
        ["模型", llm.model || "-"],
        ["证据刷新", latest.news_refresh_status || "-"],
        ["证据时间", fmtTime(latest.news_last_updated_ms)],
        ["市场状态", llm.regime_cn || "-"],
        ["行动偏向", llm.action_bias_cn || "-"],
        ["模型置信度", llm.confidence !== undefined ? `${fmtNumber((Number(llm.confidence) || 0) * 100, 1)}%` : "-"],
        ["中文摘要", llm.summary_cn || "-"],
        ["风险提示", llm.risk_note_cn || "-"],
      ].map(([label, value]) => `<tr><th>${label}</th><td>${value}</td></tr>`);
      document.getElementById("aiAnalysis").innerHTML = aiRows.join("");
      document.getElementById("aiStatus").textContent = llm.status === "READY" ? "已完成本轮分析" : (llm.status === "ERROR" ? "分析失败，已回退规则策略" : "等待分析结果");

      renderTableRows(
        document.getElementById("aiRiskGate"),
        aiRiskAssessments.length ? [`<div class="table-scroll"><table class="table">
          <thead>
            <tr>
              <th>交易对</th>
              <th>状态</th>
              <th>允许入场</th>
              <th>风险分</th>
              <th>仓位系数</th>
              <th>否决原因</th>
            </tr>
          </thead>
          <tbody>
            ${aiRiskAssessments.map((item) => `
              <tr>
                <td>${item.symbol}</td>
                <td>${item.status}</td>
                <td><span class="badge ${item.allow_entry ? "signal-buy" : "signal-sell"}">${item.allow_entry ? "允许" : "否决"}</span></td>
                <td>${fmtNumber((Number(item.risk_score) || 0) * 100, 1)}%</td>
                <td>${fmtNumber(item.position_multiplier, 2)}</td>
                <td>${item.veto_reason || "无"}</td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>`] : [],
        "当前还没有 AI 风险闸门数据。"
      );

      renderTableRows(
        document.getElementById("buyDecision"),
        buyDiagnostics.map((item) => `
          <div class="table-scroll"><table class="table">
            <thead>
              <tr>
                <th>交易对</th>
                <th>信号</th>
                <th>持仓</th>
                <th>预算</th>
                <th>最小成交额</th>
                <th>估算数量</th>
                <th>最小数量</th>
                <th>AI 风险闸门</th>
                <th>最终结论</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>${item.symbol}</td>
                <td><span class="${signalClass(item.signal_action)}">${signalLabel(item.signal_action)}</span><br><span class="mini">${item.signal_reason}</span></td>
                <td>${item.has_position ? "已有持仓" : "空仓"}</td>
                <td>${fmtCurrency(item.quote_budget, quoteAsset)}</td>
                <td>${fmtCurrency(item.min_notional_required, quoteAsset)}<br><span class="mini ${item.min_notional_passed ? "good" : "bad"}">最终 ${fmtCurrency(item.final_notional, quoteAsset)}</span></td>
                <td>${fmtNumber(item.adjusted_quantity, 4)}<br><span class="mini">原始 ${fmtNumber(item.raw_quantity, 4)}</span></td>
                <td>${fmtNumber(item.min_qty, 4)}</td>
                <td>${item.ai_allow_entry ? "允许" : "否决"}<br><span class="mini">风险分 ${fmtNumber((Number(item.ai_risk_score) || 0) * 100, 1)}% / 系数 ${fmtNumber(item.ai_position_multiplier, 2)}</span>${item.ai_veto_reason ? `<br><span class="mini bad">${item.ai_veto_reason}</span>` : ""}</td>
                <td class="${item.eligible_to_buy ? "good" : "bad"}">${item.eligible_to_buy ? "允许模拟买入" : item.blocker}</td>
              </tr>
              <tr>
                <th>阻塞详情</th>
                <td colspan="8">${(item.blocker_details || []).length ? item.blocker_details.join("；") : "当前没有阻塞条件。只要下一轮信号为买入，就会执行模拟买入。"}</td>
              </tr>
            </tbody>
          </table></div>
        `),
        "当前还没有买入决策链路数据。"
      );

      renderTableRows(
        document.getElementById("scheduling"),
        schedulingDiagnostics.length ? [`<div class="table-scroll"><table class="table">
          <thead>
            <tr>
              <th>交易对</th>
              <th>轮次类型</th>
              <th>触发原因</th>
              <th>最新收盘 K</th>
              <th>上次决策 K</th>
              <th>价格偏移</th>
            </tr>
          </thead>
          <tbody>
            ${schedulingDiagnostics.map((item) => `
              <tr>
                <td>${item.symbol}</td>
                <td><span class="badge ${item.should_run_decision ? "signal-buy" : "signal-hold"}">${item.should_run_decision ? "进入决策" : "仅刷新"}</span></td>
                <td>${item.decision_reason}</td>
                <td>${fmtTime(item.latest_closed_candle_close_time)}</td>
                <td>${item.last_decision_candle_close_time ? fmtTime(item.last_decision_candle_close_time) : "无"}</td>
                <td>${fmtNumber((Number(item.price_move_pct) || 0) * 100, 2)}%</td>
              </tr>
            `).join("")}
          </tbody>
        </table></div>`] : [],
        "当前还没有调度状态数据。"
      );

      document.getElementById("priceStats").textContent = liveBars.length ? `${liveBars.length} 根 ${payload.live_main_interval || ""} K 线 · 最新 ${fmtCurrency(currentPrice, quoteAsset)}` : "暂无主周期 K 线";
      document.getElementById("equityStats").textContent = history.length ? `${history.length} 个点 · 最新 ${fmtCurrency(latest.total_equity, quoteAsset)}` : "暂无数据点";
      document.getElementById("pnlStats").textContent = history.length ? `${history.length} 个点 · 最新 ${fmtCurrency(latest.net_pnl, quoteAsset)}` : "暂无数据点";

      drawCandlestickChart(
        document.getElementById("priceChart"),
        liveBars,
        {
          intervalLabel: payload.live_main_interval || "",
          labelFormatter: (value) => fmtCurrency(value, quoteAsset),
          tradeMarkers: liveTradeMarkers,
          vetoMarkers: liveAiVetoMarkers,
          stopLossPrice: currentPosition ? currentPosition.stop_loss_price : null,
          takeProfitPrice: currentPosition ? currentPosition.take_profit_price : null,
          trailingStopPrice: currentPosition ? currentPosition.trailing_stop_price : null,
        }
      );

      drawLineChart(
        document.getElementById("equityChart"),
        history.map((item) => ({ value: Number(item.total_equity || 0), timestamp_ms: item.timestamp_ms })),
        "#d36b2d",
        "rgba(211,107,45,0.16)",
        { labelFormatter: (value) => fmtCurrency(value, quoteAsset) }
      );

      drawLineChart(
        document.getElementById("pnlChart"),
        history.map((item) => ({ value: Number(item.net_pnl || 0), timestamp_ms: item.timestamp_ms })),
        Number(latest.net_pnl || 0) >= 0 ? "#1d7f52" : "#b13f2f",
        Number(latest.net_pnl || 0) >= 0 ? "rgba(29,127,82,0.16)" : "rgba(177,63,47,0.16)",
        { labelFormatter: (value) => fmtCurrency(value, quoteAsset) }
      );
    }

    function updateBacktestDom(payload) {
      const summary = payload.backtest_summary || {};
      const segments = payload.backtest_segments || [];
      const equityCurve = payload.backtest_equity_curve || [];
      const trades = payload.backtest_trades || [];
      const manifest = payload.backtest_manifest || {};
      const backtestAvailable = Boolean(payload.backtest_available);
      const sourceName = payload.backtest_source || "未找到目录";
      const quoteAsset = ((manifest.config || {}).quote_asset) || ((payload.paper_state || {}).quote_asset) || "JPY";

      document.getElementById("btSourceLabel").textContent = backtestAvailable ? `来源：${sourceName}` : "当前没有回测目录";
      document.getElementById("btTotalReturn").textContent = backtestAvailable ? fmtPercent(summary.total_return_pct, 2) : "-";
      document.getElementById("btTotalReturn").className = `metric-value ${classForPnl(summary.total_return_pct)}`;
      document.getElementById("btMaxDrawdown").textContent = backtestAvailable ? fmtPercent(summary.max_drawdown_pct, 2) : "-";
      document.getElementById("btWinRate").textContent = backtestAvailable ? fmtPercent(summary.win_rate, 2) : "-";
      document.getElementById("btProfitFactor").textContent = backtestAvailable ? fmtNumber(summary.profit_factor, 2) : "-";
      document.getElementById("btExpectancy").textContent = backtestAvailable ? fmtNumber(summary.expectancy_per_trade, 4) : "-";
      document.getElementById("btTradeCount").textContent = backtestAvailable ? String(summary.trade_count || 0) : "-";
      document.getElementById("btTradeCountSub").textContent = backtestAvailable ? `总交易 ${summary.trade_count || 0} / 已平仓 ${summary.completed_trade_count || 0}` : "总交易 / 已平仓";

      const manifestRows = backtestAvailable ? [
        ["结果目录", sourceName],
        ["交易对", summary.symbol || ((manifest.config || {}).symbol) || "-"],
        ["区间", `${summary.date_from || "-"} ~ ${summary.date_to || "-"}`],
        ["主周期", (manifest.config || {}).main_interval || "-"],
        ["Walk-forward", manifest.walk_forward ? "是" : "否"],
        ["训练 / 测试 / 步长", manifest.walk_forward ? `${(manifest.config || {}).train_days || 0}d / ${(manifest.config || {}).test_days || 0}d / ${(manifest.config || {}).step_days || 0}d` : "单次回测"],
        ["数据文件数", String((manifest.dataset_infos || []).length)],
        ["备注", (manifest.notes || []).length ? manifest.notes.join("；") : "无"],
      ] : [
        ["结果目录", "未发现 runtime_backtest_walk 或 runtime_backtest_check"],
      ];
      document.getElementById("btManifest").innerHTML = manifestRows.map(([label, value]) => `<tr><th>${label}</th><td>${value}</td></tr>`).join("");

      document.getElementById("btEquityStats").textContent = backtestAvailable ? `${equityCurve.length} 个主周期点 · 期末 ${fmtCurrency(summary.ending_total_equity, quoteAsset)}` : "等待回测结果";
      document.getElementById("btDrawdownStats").textContent = backtestAvailable ? `最大回撤 ${fmtPercent(summary.max_drawdown_pct, 2)}` : "等待回测结果";

      drawLineChart(
        document.getElementById("btEquityChart"),
        equityCurve.map((row) => ({ value: Number(row.total_equity || 0), timestamp_ms: Number(row.timestamp_ms || 0) })),
        "#d36b2d",
        "rgba(211,107,45,0.16)",
        { labelFormatter: (value) => fmtCurrency(value, quoteAsset) }
      );

      drawLineChart(
        document.getElementById("btDrawdownChart"),
        equityCurve.map((row) => ({ value: Number(row.drawdown_pct || 0), timestamp_ms: Number(row.timestamp_ms || 0) })),
        "#b13f2f",
        "rgba(177,63,47,0.16)",
        { labelFormatter: (value) => fmtPercent(value, 2) }
      );

      renderTableRows(
        document.getElementById("btSegments"),
        segments.length ? [`<table class="table">
          <thead>
            <tr>
              <th>Segment</th>
              <th>训练窗口</th>
              <th>测试窗口</th>
              <th>总收益率</th>
              <th>最大回撤</th>
              <th>胜率</th>
              <th>优于基线</th>
            </tr>
          </thead>
          <tbody>
            ${segments.map((segment) => `
              <tr>
                <td>#${segment.segment_index}</td>
                <td>${segment.train_from} ~ ${segment.train_to}</td>
                <td>${segment.test_from} ~ ${segment.test_to}</td>
                <td class="${classForPnl(segment.summary.total_return_pct)}">${fmtPercent(segment.summary.total_return_pct, 2)}</td>
                <td>${fmtPercent(segment.summary.max_drawdown_pct, 2)}</td>
                <td>${fmtPercent(segment.summary.win_rate, 2)}</td>
                <td><span class="badge ${segment.beats_baseline ? "signal-buy" : "signal-sell"}">${segment.beats_baseline ? "是" : "否"}</span></td>
              </tr>
            `).join("")}
          </tbody>
        </table>`] : [],
        backtestAvailable ? "当前结果不是 walk-forward，或该目录没有 segment 数据。" : "当前没有回测结果。"
      );

      const tradeRows = trades.slice().reverse();
      renderTableRows(
        document.getElementById("btTrades"),
        tradeRows.length ? [`<table class="table">
          <thead>
            <tr>
              <th>方向</th>
              <th>开仓</th>
              <th>平仓</th>
              <th>开仓价</th>
              <th>平仓价</th>
              <th>已实现盈亏</th>
              <th>收益率</th>
              <th>持仓根数</th>
              <th>持仓小时</th>
              <th>MFE / MAE</th>
              <th>退出原因</th>
            </tr>
          </thead>
          <tbody>
            ${tradeRows.map((trade) => `
              <tr>
                <td><span class="${signalClass(trade.side)}">${signalLabel(trade.side)}</span></td>
                <td>${fmtTime(Number(trade.entry_time_ms || 0))}</td>
                <td>${fmtTime(Number(trade.exit_time_ms || 0))}</td>
                <td>${fmtCurrency(trade.entry_price, quoteAsset)}</td>
                <td>${fmtCurrency(trade.exit_price, quoteAsset)}</td>
                <td class="${classForPnl(trade.realized_pnl)}">${fmtCurrency(trade.realized_pnl, quoteAsset)}</td>
                <td class="${classForPnl(trade.return_pct)}">${fmtPercent(trade.return_pct, 2)}</td>
                <td>${trade.hold_bars || "0"}</td>
                <td>${fmtNumber(trade.hold_hours, 2)}</td>
                <td>${fmtPercent(trade.mfe_pct, 2)} / ${fmtPercent(trade.mae_pct, 2)}</td>
                <td>${trade.exit_reason || "-"}</td>
              </tr>
            `).join("")}
          </tbody>
        </table>`] : [],
        backtestAvailable ? "该回测样本没有触发交易。" : "当前没有回测结果。"
      );
    }

    function updateDom(payload) {
      lastPayloadSnapshot = payload;
      const defaultView = (!userPinnedView && payload.backtest_available) ? "backtest" : "live";
      if (!activeView) {
        activateView(defaultView);
      } else if (!userPinnedView) {
        activateView(defaultView);
      }

      document.getElementById("viewHint").textContent = payload.backtest_available
        ? `回测来源：${payload.backtest_source} · 实时与回测可随时切换`
        : "当前未发现回测目录，已聚焦实时监控";

      updateLiveDom(payload);
      updateBacktestDom(payload);
    }

    async function tick() {
      try {
        const payload = await loadData();
        updateDom(payload);
      } catch (error) {
        document.getElementById("lastUpdated").textContent = `加载失败：${error.message}`;
      }
    }

    document.getElementById("liveTab").addEventListener("click", () => activateView("live", true));
    document.getElementById("backtestTab").addEventListener("click", () => activateView("backtest", true));
    tick();
    setInterval(tick, refreshMs);
    window.addEventListener("resize", tick);
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
                }
            )
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
    limit: int = 120,
) -> List[Dict[str, Any]]:
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
    return bars[-limit:]


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
    bars = _build_live_main_interval_bars(history, symbol=chart_symbol, interval=main_interval) if chart_symbol else []

    return {
        "latest_report": latest_report,
        "paper_state": paper_state,
        "history": history,
        "recent_fills": _extract_recent_fills(history),
        "live_chart_symbol": chart_symbol,
        "live_main_interval": main_interval,
        "live_main_interval_bars": bars,
        "live_trade_markers": _extract_live_trade_markers(history),
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
