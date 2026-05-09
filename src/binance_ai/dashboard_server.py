from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import urlparse


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
      width: min(1380px, calc(100vw - 32px));
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
      max-width: 58ch;
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

    .metrics {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
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
      height: 280px;
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

    .list, .table {
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

    @media (max-width: 1120px) {
      .hero, .grid, .metrics {
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
        <h1>实时查看模拟交易，不再读原始 JSON。</h1>
        <div class="subtitle">
          这个看板会自动刷新，展示实时行情、模拟账户权益、净收益、策略信号、持仓状态，以及最近的模拟成交记录。
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
          <h2>价格曲线</h2>
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
          <h2>当前持仓</h2>
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
          <div class="mini">最近的纸面成交记录</div>
        </div>
        <div id="fills"></div>
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
          <h2>买入决策链路</h2>
          <div class="mini">逐步展示当前为什么可以买或不能买</div>
        </div>
        <div id="buyDecision"></div>
      </div>
    </section>

    <div class="footer-note">
      页面每 5 秒自动刷新一次。保持监控进程运行，新的价格点和交易结果才会持续出现。
    </div>
  </div>

  <script>
    const refreshMs = 5000;

    function fmtNumber(value, digits = 2) {
      if (value === null || value === undefined || Number.isNaN(Number(value))) return "-";
      return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits, minimumFractionDigits: digits });
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

    function signalLabel(action) {
      const key = String(action || "").toUpperCase();
      if (key === "BUY") return "买入";
      if (key === "SELL") return "卖出";
      if (key === "HOLD") return "持有";
      return key || "-";
    }

    function renderTableRows(container, rows, emptyText) {
      if (!rows.length) {
        container.innerHTML = `<div class="empty">${emptyText}</div>`;
        return;
      }
      container.innerHTML = rows.join("");
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

      const values = points.map(p => p.value);
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
      for (let i = 0; i <= 3; i++) {
        const y = topPad + (innerH / 3) * i;
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

    async function loadData() {
      const response = await fetch("/api/dashboard");
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    function updateDom(payload) {
      const latest = payload.latest_report || {};
      const state = payload.paper_state || {};
      const history = payload.history || [];
      const fills = payload.recent_fills || [];
      const evidence = latest.news_evidence || [];
      const buyDiagnostics = latest.buy_diagnostics || [];
      const quoteAsset = state.quote_asset || "JPY";
      const llm = latest.llm_analysis || {};
      const symbols = Object.keys(latest.market_prices || {});
      const primarySymbol = symbols[0] || ((latest.decisions || [])[0] || {}).symbol || "SYMBOL";
      const currentPrice = (latest.market_prices || {})[primarySymbol];
      const priceHistory = history
        .map(item => ({
          timestamp_ms: item.timestamp_ms,
          value: Number(((item.market_prices || {})[primarySymbol]) || 0),
        }))
        .filter(item => item.value > 0);

      document.getElementById("modePill").textContent = latest.simulation_mode ? "模拟模式" : "实盘模式";
      document.getElementById("lastUpdated").textContent = `最近周期：${fmtTime(latest.timestamp_ms)}`;
      document.getElementById("activeSymbols").textContent = String((latest.decisions || []).length);
      document.getElementById("quoteAssetLabel").textContent = `计价资产：${quoteAsset}`;

      const latestDecision = (latest.decisions || [])[0];
      document.getElementById("latestReason").textContent = latestDecision ? latestDecision.signal.reason : "当前还没有决策。";

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
            <tr><th>止损线</th><td>${fmtCurrency(pos.stop_loss_price, quoteAsset)}</td></tr>
            <tr><th>止盈线</th><td>${fmtCurrency(pos.take_profit_price, quoteAsset)}</td></tr>
            <tr><th>跟踪止损线</th><td>${fmtCurrency(pos.trailing_stop_price, quoteAsset)}<br><span class="mini">最高价 ${fmtCurrency(pos.highest_price, quoteAsset)}</span></td></tr>
            <tr><th>持仓根数</th><td>${pos.bars_held} 根 K 线</td></tr>
            <tr><th>退出监控</th><td>${pos.exit_watch_reason}</td></tr>
          </table>
        `),
        "当前没有模拟持仓。"
      );

      renderTableRows(
        document.getElementById("signals"),
        [`<table class="table">
          <thead><tr><th>交易对</th><th>动作</th><th>置信度</th><th>原因</th></tr></thead>
          <tbody>
          ${(latest.decisions || []).map(decision => `
            <tr>
              <td>${decision.symbol}</td>
              <td><span class="${signalClass(decision.signal.action)}">${signalLabel(decision.signal.action)}</span></td>
              <td>${fmtNumber((decision.signal.confidence || 0) * 100, 1)}%</td>
              <td>${decision.signal.reason}</td>
            </tr>`).join("")}
          </tbody>
        </table>`],
        "当前还没有信号记录。"
      );

      renderTableRows(
        document.getElementById("fills"),
        fills.length ? [`<table class="table">
          <thead><tr><th>时间</th><th>交易对</th><th>方向</th><th>数量</th><th>成交价</th><th>盈亏</th></tr></thead>
          <tbody>
          ${fills.map(fill => `
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
        </table>`] : [],
        "当前还没有模拟成交。"
      );

      const snapshotRows = [
        ["主交易对", primarySymbol],
        ["最新价格", fmtCurrency(currentPrice, quoteAsset)],
        ["计价资产余额", fmtCurrency(state.quote_balance, quoteAsset)],
        ["初始资金", fmtCurrency(state.initial_quote_balance, quoteAsset)],
        ["已实现收益", fmtCurrency(state.realized_pnl, quoteAsset)],
        ["新闻层状态", latest.news_refresh_status || "-"],
        ["新闻下次刷新", fmtTime(latest.news_next_refresh_ms)],
        ["历史周期数", String(history.length)],
      ].map(([label, value]) => `<tr><th>${label}</th><td>${value}</td></tr>`);
      document.getElementById("snapshot").innerHTML = snapshotRows.join("");

      const evidenceRows = evidence.slice(0, 8).map(item => `
        <tr>
          <td>${item.source}</td>
          <td>${item.title}</td>
        </tr>
      `);
      renderTableRows(
        document.getElementById("evidence"),
        evidenceRows.length ? [`<table class="table">
          <thead><tr><th>来源</th><th>标题</th></tr></thead>
          <tbody>${evidenceRows.join("")}</tbody>
        </table>`] : [],
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
        document.getElementById("buyDecision"),
        buyDiagnostics.map(item => `
          <table class="table">
            <thead>
              <tr>
                <th>交易对</th>
                <th>信号</th>
                <th>持仓</th>
                <th>预算</th>
                <th>最小成交额</th>
                <th>估算数量</th>
                <th>最小数量</th>
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
                <td class="${item.eligible_to_buy ? "good" : "bad"}">${item.eligible_to_buy ? "允许模拟买入" : item.blocker}</td>
              </tr>
              <tr>
                <th>阻塞详情</th>
                <td colspan="7">${(item.blocker_details || []).length ? item.blocker_details.join("；") : "当前没有阻塞条件。只要下一轮信号为买入，就会执行模拟买入。"}</td>
              </tr>
            </tbody>
          </table>
        `),
        "当前还没有买入决策链路数据。"
      );

      document.getElementById("priceStats").textContent = priceHistory.length ? `${priceHistory.length} 个点 · 最新 ${fmtCurrency(currentPrice, quoteAsset)}` : "暂无数据点";
      document.getElementById("equityStats").textContent = history.length ? `${history.length} 个点 · 最新 ${fmtCurrency(latest.total_equity, quoteAsset)}` : "暂无数据点";
      document.getElementById("pnlStats").textContent = history.length ? `${history.length} 个点 · 最新 ${fmtCurrency(latest.net_pnl, quoteAsset)}` : "暂无数据点";

      drawLineChart(
        document.getElementById("priceChart"),
        priceHistory.map(item => ({ value: item.value, timestamp_ms: item.timestamp_ms })),
        "#2f6fd0",
        "rgba(47,111,208,0.16)",
        { labelFormatter: (value) => fmtCurrency(value, quoteAsset) }
      );
      drawLineChart(
        document.getElementById("equityChart"),
        history.map(item => ({ value: Number(item.total_equity || 0), timestamp_ms: item.timestamp_ms })),
        "#d36b2d",
        "rgba(211,107,45,0.16)",
        { labelFormatter: (value) => fmtCurrency(value, quoteAsset) }
      );
      drawLineChart(
        document.getElementById("pnlChart"),
        history.map(item => ({ value: Number(item.net_pnl || 0), timestamp_ms: item.timestamp_ms })),
        Number(latest.net_pnl || 0) >= 0 ? "#1d7f52" : "#b13f2f",
        Number(latest.net_pnl || 0) >= 0 ? "rgba(29,127,82,0.16)" : "rgba(177,63,47,0.16)",
        { labelFormatter: (value) => fmtCurrency(value, quoteAsset) }
      );
    }

    async function tick() {
      try {
        const payload = await loadData();
        updateDom(payload);
      } catch (error) {
        document.getElementById("lastUpdated").textContent = `加载失败：${error.message}`;
      }
    }

    tick();
    setInterval(tick, refreshMs);
    window.addEventListener("resize", tick);
  </script>
</body>
</html>
"""


def _load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _read_history(path: Path, limit: int = 240) -> List[Dict[str, Any]]:
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


class DashboardHandler(BaseHTTPRequestHandler):
    runtime_dir: Path

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(INDEX_HTML)
            return
        if parsed.path == "/api/dashboard":
            self._send_json(self._dashboard_payload())
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")

    def log_message(self, format: str, *args: object) -> None:
        return

    def _dashboard_payload(self) -> Dict[str, Any]:
        latest_report = _load_json(self.runtime_dir / "latest_report.json", {})
        paper_state = _load_json(self.runtime_dir / "paper_state.json", {})
        history = _read_history(self.runtime_dir / "cycle_reports.jsonl")
        return {
            "latest_report": latest_report,
            "paper_state": paper_state,
            "history": history,
            "recent_fills": _extract_recent_fills(history),
        }

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
