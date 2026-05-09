from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from binance_ai.dashboard_server import (
    INDEX_HTML,
    _aggregate_chart_bars,
    _build_dashboard_chart_payload,
    _build_live_main_interval_bars,
    _chart_cache_needs_tail_refresh,
    _extract_chart_trade_markers_from_file,
    _extract_recent_fills_from_file,
    _extract_position_activation_markers,
    _extract_live_ai_veto_markers,
    _extract_live_trade_markers,
    _merge_chart_bars,
    _sample_rows,
    build_dashboard_payload,
)


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


class DashboardServerTests(unittest.TestCase):
    def test_index_html_exposes_botinance_shell(self) -> None:
        self.assertIn("app-shell", INDEX_HTML)
        self.assertNotIn("side-rail", INDEX_HTML)
        self.assertNotIn("rail-button", INDEX_HTML)
        self.assertIn("top-bar", INDEX_HTML)
        self.assertIn("trade-workspace", INDEX_HTML)
        self.assertIn("trade-main-column", INDEX_HTML)
        self.assertIn("trade-fill-panel", INDEX_HTML)
        self.assertIn("订单与成交记录", INDEX_HTML)
        self.assertIn("冻结资产", INDEX_HTML)
        self.assertIn("交割成本总盈亏", INDEX_HTML)
        self.assertIn("Boti接手后操作盈亏", INDEX_HTML)
        self.assertIn("real_cost_basis_summary", INDEX_HTML)
        self.assertIn("Boti 超时撤单", INDEX_HTML)
        self.assertIn("交易所过期", INDEX_HTML)
        self.assertIn("\"状态\", \"方向\", \"数量\", \"价格\", \"冻结\", \"手续费\", \"已实现\", \"时间\"", INDEX_HTML)
        self.assertIn("fillPageSize", INDEX_HTML)
        self.assertIn("fillPrev", INDEX_HTML)
        self.assertIn("fillNext", INDEX_HTML)
        self.assertIn("insight-drawer", INDEX_HTML)
        self.assertIn("chartIntervalSelect", INDEX_HTML)
        self.assertIn("chartLoading", INDEX_HTML)
        self.assertIn("dashboardRequestSeq", INDEX_HTML)
        self.assertIn("chartRenderSeq", INDEX_HTML)
        self.assertIn("tickInFlight", INDEX_HTML)
        self.assertIn("setChartLoading(false", INDEX_HTML)
        self.assertIn("requestSeq !== dashboardRequestSeq", INDEX_HTML)
        self.assertIn("requestInterval === selectedChartInterval", INDEX_HTML)
        self.assertIn("include_chart", INDEX_HTML)
        self.assertIn("/api/dashboard/chart", INDEX_HTML)
        self.assertIn("scheduleChartRender", INDEX_HTML)
        self.assertIn("preserveChartPayload", INDEX_HTML)
        self.assertIn("previous.live_trade_markers", INDEX_HTML)
        self.assertIn("triggerLabel", INDEX_HTML)
        self.assertIn("reasonLabel", INDEX_HTML)
        self.assertIn("跟踪止损", INDEX_HTML)
        self.assertIn("模拟限价单已成交", INDEX_HTML)
        self.assertIn("data-drawer=\"evidence\"", INDEX_HTML)
        self.assertIn("data-drawer=\"decision\"", INDEX_HTML)
        self.assertNotIn("bottom-insight-grid", INDEX_HTML)
        self.assertIn("实时交易", INDEX_HTML)
        self.assertIn("AI 决策", INDEX_HTML)
        self.assertIn("回测分析", INDEX_HTML)
        self.assertIn("风险控制", INDEX_HTML)
        self.assertIn("系统日志", INDEX_HTML)
        self.assertIn("drawCandlestickChart", INDEX_HTML)
        self.assertIn("volumeHeight", INDEX_HTML)
        self.assertIn("barVolumeValue", INDEX_HTML)
        self.assertNotIn("sample_count || b.volume", INDEX_HTML)

    def test_build_dashboard_payload_prefers_walk_forward_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_dir = root / "runtime_visual"
            _write_json(
                runtime_dir / "latest_report.json",
                {
                    "timestamp_ms": 3_000,
                    "decisions": [
                        {
                            "symbol": "XRPJPY",
                            "signal": {
                                "symbol": "XRPJPY",
                                "action": "HOLD",
                                "confidence": 0.4,
                                "reason": "mtf_hold 1h=above 4h=uptrend 15m=momentum_up",
                            },
                            "order": None,
                            "execution_result": {"status": "SKIPPED_REFRESH_ONLY", "reason": "refresh_only"},
                        }
                    ],
                    "market_prices": {"XRPJPY": 223.4},
                },
            )
            _write_json(
                runtime_dir / "paper_state.json",
                {
                    "quote_asset": "JPY",
                    "quote_balance": 1000.0,
                    "initial_total_equity": 1320.0,
                    "net_pnl": 126.8,
                    "positions": {
                        "XRPJPY": {
                            "quantity": 2.0,
                            "average_entry_price": 220.0,
                            "highest_price": 223.4,
                        }
                    },
                    "open_orders": {
                        "order-1": {
                            "client_order_id": "order-1",
                            "symbol": "XRPJPY",
                            "side": "BUY",
                            "order_type": "LIMIT",
                            "quantity": 1.0,
                            "limit_price": 220.0,
                            "time_in_force": "GTC",
                            "status": "OPEN",
                            "reserved_quote": 220.5,
                            "created_at_ms": 1_000,
                            "updated_at_ms": 1_000,
                            "expires_at_ms": 10_000,
                        }
                    },
                },
            )
            _write_json(
                runtime_dir / "account_seed_manifest.json",
                {
                    "quote_asset": "JPY",
                    "balances": {"JPY": 100.0, "XRP": 10.0},
                    "cost_basis_by_symbol": {
                        "XRPJPY": {
                            "source": "binance_my_trades_fifo",
                            "average_entry_price": 200.0,
                        }
                    },
                },
            )
            _write_text(
                runtime_dir / "cycle_reports.jsonl",
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp_ms": 1_000,
                                "decisions": [],
                                "decision_ledger": [
                                    {
                                        "timestamp_ms": 1_000,
                                        "cycle_mode": "REFRESH",
                                        "symbol": "XRPJPY",
                                        "price": 221.0,
                                        "sell_blocker": "继续持有",
                                        "final_action": "REFRESH_ONLY",
                                    }
                                ],
                                "order_lifecycle_events": [
                                    {
                                        "timestamp_ms": 1_000,
                                        "symbol": "XRPJPY",
                                        "client_order_id": "order-1",
                                        "event_type": "SUBMITTED",
                                        "status": "OPEN",
                                        "side": "BUY",
                                        "quantity": 1.0,
                                        "limit_price": 220.0,
                                        "trigger": "strategy_buy",
                                    }
                                ],
                                "market_prices": {"XRPJPY": 221.0},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp_ms": 2_000,
                                "decisions": [],
                                "market_prices": {"XRPJPY": 222.0},
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp_ms": 3_000,
                                "decisions": [],
                                "market_prices": {"XRPJPY": 223.0},
                            }
                        ),
                    ]
                ),
            )

            _write_json(root / "runtime_backtest_check" / "summary.json", {"symbol": "CHECK", "trade_count": 1})
            _write_json(root / "runtime_backtest_check" / "segments.json", [])
            _write_text(root / "runtime_backtest_check" / "equity_curve.csv", "timestamp_ms,total_equity,drawdown_pct\n1,1000,0\n")
            _write_text(root / "runtime_backtest_check" / "trades.csv", "side,entry_time_ms\nBUY,1\n")
            _write_json(root / "runtime_backtest_check" / "run_manifest.json", {"config": {"main_interval": "1h"}})

            _write_json(root / "runtime_backtest_walk" / "summary.json", {"symbol": "WALK", "trade_count": 2})
            _write_json(
                root / "runtime_backtest_walk" / "segments.json",
                [{"segment_index": 1, "summary": {"trade_count": 0, "total_return_pct": 0.0, "max_drawdown_pct": 0.0, "win_rate": 0.0}, "beats_baseline": True}],
            )
            _write_text(root / "runtime_backtest_walk" / "equity_curve.csv", "timestamp_ms,total_equity,drawdown_pct\n1,1000,0\n")
            _write_text(root / "runtime_backtest_walk" / "trades.csv", "side,entry_time_ms\nBUY,1\n")
            _write_json(root / "runtime_backtest_walk" / "run_manifest.json", {"config": {"main_interval": "1h"}, "walk_forward": True})

            payload = build_dashboard_payload(runtime_dir)

        self.assertTrue(payload["backtest_available"])
        self.assertEqual(payload["backtest_source"], "runtime_backtest_walk")
        self.assertEqual(payload["backtest_summary"]["symbol"], "WALK")
        self.assertEqual(payload["live_chart_symbol"], "XRPJPY")
        self.assertEqual(payload["live_main_interval"], "1h")
        self.assertEqual(payload["live_chart_interval"], "1m")
        self.assertTrue(payload["chart_interval_options"])
        self.assertEqual(payload["history_count"], 3)
        self.assertEqual(len(payload["history"]), 3)
        self.assertEqual(len(payload["live_main_interval_bars"]), 1)
        self.assertEqual(payload["live_refresh_interval"], "1m")
        self.assertEqual(len(payload["live_refresh_bars"]), 1)
        self.assertEqual(payload["live_refresh_bars"][0]["sample_count"], 3)
        self.assertEqual(len(payload["decision_ledger"]), 1)
        self.assertEqual(payload["decision_ledger"][0]["sell_blocker"], "继续持有")
        self.assertEqual(len(payload["open_orders"]), 1)
        self.assertEqual(payload["open_orders"][0]["client_order_id"], "order-1")
        self.assertEqual(len(payload["order_lifecycle_events"]), 1)
        self.assertEqual(payload["order_lifecycle_events"][0]["status"], "OPEN")
        self.assertGreaterEqual(len(payload["trade_records"]), 1)
        self.assertEqual(payload["trade_records"][0]["status"], "OPEN")
        self.assertEqual(payload["trade_records"][0]["reserved_quote"], 220.5)
        self.assertEqual(len(payload["order_markers"]), 1)
        self.assertEqual(payload["order_markers"][0]["status"], "OPEN")
        self.assertEqual(payload["real_cost_basis_summary"]["quote_asset"], "JPY")
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["original_initial_equity"], 2100.0)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["current_total_equity"], 1446.8)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["realized_pnl"], -700.0)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["unrealized_pnl"], 46.8)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["total_pnl"], -653.2)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["boti_initial_equity"], 1320.0)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["boti_net_pnl"], 126.8)
        self.assertAlmostEqual(payload["real_cost_basis_summary"]["symbols"]["XRPJPY"]["sold_quantity"], 8.0)

    def test_dashboard_payload_can_defer_chart_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime_visual"
            _write_json(
                runtime_dir / "latest_report.json",
                {
                    "timestamp_ms": 3_000,
                    "decisions": [{"symbol": "XRPJPY", "signal": {"action": "HOLD"}}],
                    "market_prices": {"XRPJPY": 223.4},
                },
            )
            _write_text(
                runtime_dir / "cycle_reports.jsonl",
                "\n".join(
                    json.dumps({"timestamp_ms": ts, "decisions": [], "market_prices": {"XRPJPY": price}})
                    for ts, price in [(1_000, 221.0), (2_000, 222.0), (3_000, 223.0)]
                ),
            )
            _write_json(
                runtime_dir / "chart_cache" / "XRPJPY_1h.json",
                {
                    "symbol": "XRPJPY",
                    "interval": "1h",
                    "source": "cache",
                    "bars": [
                        {
                            "symbol": "XRPJPY",
                            "open_time": 0,
                            "close_time": 3_599_999,
                            "open": 221.0,
                            "high": 223.0,
                            "low": 221.0,
                            "close": 223.0,
                            "volume": 1.0,
                            "sample_count": 3,
                        }
                    ],
                },
            )

            payload = build_dashboard_payload(runtime_dir, chart_interval="1h", include_chart=False)
            chart_payload = _build_dashboard_chart_payload(runtime_dir, chart_interval="1h")

        self.assertEqual(payload["live_chart_interval"], "1h")
        self.assertEqual(payload["live_chart_source"], "deferred")
        self.assertEqual(payload["live_chart_bars"], [])
        self.assertEqual(payload["live_main_interval_bars"], [])
        self.assertEqual(payload["history_count"], 3)
        self.assertEqual(payload["history"], [])
        self.assertEqual(chart_payload["live_chart_interval"], "1h")
        self.assertGreaterEqual(len(chart_payload["live_chart_bars"]), 1)

    def test_deferred_dashboard_keeps_fills_outside_light_history_window(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir) / "runtime_visual"
            _write_json(
                runtime_dir / "latest_report.json",
                {
                    "timestamp_ms": 500_000,
                    "decisions": [{"symbol": "XRPJPY", "signal": {"action": "HOLD"}}],
                    "market_prices": {"XRPJPY": 223.4},
                },
            )
            lines = [
                json.dumps(
                    {
                        "timestamp_ms": 1_000,
                        "market_prices": {"XRPJPY": 222.0},
                        "decisions": [
                            {
                                "symbol": "XRPJPY",
                                "execution_result": {
                                    "status": "PAPER_FILLED",
                                    "symbol": "XRPJPY",
                                    "side": "SELL",
                                    "quantity": 1.0,
                                    "fill_price": 222.0,
                                    "timestamp_ms": 1_000,
                                },
                            }
                        ],
                    }
                )
            ]
            lines.extend(
                json.dumps({"timestamp_ms": 2_000 + i, "market_prices": {"XRPJPY": 223.0}, "decisions": []})
                for i in range(400)
            )
            _write_text(runtime_dir / "cycle_reports.jsonl", "\n".join(lines))

            payload = build_dashboard_payload(runtime_dir, chart_interval="5m", include_chart=False)
            chart_payload = _build_dashboard_chart_payload(runtime_dir, chart_interval="1d")
            fills = _extract_recent_fills_from_file(runtime_dir / "cycle_reports.jsonl")
            markers = _extract_chart_trade_markers_from_file(runtime_dir / "cycle_reports.jsonl")

        self.assertEqual(payload["history_count"], 300)
        self.assertEqual(payload["history"], [])
        self.assertEqual(len(payload["recent_fills"]), 1)
        self.assertEqual(payload["recent_fills"][0]["status"], "PAPER_FILLED")
        self.assertEqual(len(payload["trade_records"]), 1)
        self.assertEqual(payload["trade_records"][0]["status"], "PAPER_FILLED")
        self.assertGreater(payload["trade_records"][0]["fee"], 0.0)
        self.assertEqual(payload["trade_records"][0]["reserved_quote"], 0.0)
        self.assertEqual(len(fills), 1)
        self.assertEqual(len(markers), 1)
        self.assertEqual(len(chart_payload["live_trade_markers"]), 1)
        self.assertEqual(chart_payload["live_trade_markers"][0]["trigger"], "")

    def test_build_dashboard_payload_returns_empty_backtest_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_dir = root / "runtime_visual"
            _write_json(runtime_dir / "latest_report.json", {})
            _write_json(runtime_dir / "paper_state.json", {})
            _write_text(runtime_dir / "cycle_reports.jsonl", "")

            payload = build_dashboard_payload(runtime_dir)

        self.assertFalse(payload["backtest_available"])
        self.assertIsNone(payload["backtest_source"])
        self.assertEqual(payload["backtest_summary"], {})
        self.assertEqual(payload["backtest_segments"], [])
        self.assertEqual(payload["backtest_equity_curve"], [])
        self.assertEqual(payload["backtest_trades"], [])
        self.assertEqual(payload["backtest_manifest"], {})
        self.assertIn("未找到", payload["backtest_missing_reason"])
        self.assertEqual(payload["decision_ledger"], [])
        self.assertEqual(payload["sell_diagnostics"], [])
        self.assertEqual(payload["position_activation_state"], {})
        self.assertEqual(payload["open_orders"], [])
        self.assertEqual(payload["order_lifecycle_events"], [])
        self.assertEqual(payload["trade_records"], [])
        self.assertEqual(payload["order_markers"], [])
        self.assertEqual(payload["real_cost_basis_summary"], {})

    def test_build_dashboard_payload_uses_cached_requested_chart_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime_dir = root / "runtime_visual"
            _write_json(
                runtime_dir / "latest_report.json",
                {
                    "timestamp_ms": 120_000,
                    "decisions": [{"symbol": "XRPJPY", "signal": {"action": "HOLD"}, "execution_result": {}}],
                    "market_prices": {"XRPJPY": 223.0},
                },
            )
            _write_json(runtime_dir / "paper_state.json", {"quote_asset": "JPY", "quote_balance": 1000})
            _write_text(runtime_dir / "cycle_reports.jsonl", "")
            _write_json(
                runtime_dir / "chart_cache" / "XRPJPY_10m.json",
                {
                    "symbol": "XRPJPY",
                    "interval": "10m",
                    "label": "10分",
                    "source": "aggregate:5m",
                    "fetched_at": 1.0,
                    "bars": [
                        {
                            "symbol": "XRPJPY",
                            "open_time": 0,
                            "close_time": 599_999,
                            "open": 220.0,
                            "high": 225.0,
                            "low": 219.0,
                            "close": 224.0,
                            "volume": 10.0,
                            "sample_count": 2,
                            "source": "aggregate:10m",
                        }
                    ],
                },
            )

            payload = build_dashboard_payload(runtime_dir, chart_interval="10m")

        self.assertEqual(payload["live_chart_interval"], "10m")
        self.assertEqual(payload["live_chart_interval_label"], "10分")
        self.assertEqual(payload["live_chart_source"], "aggregate:5m")
        self.assertTrue(payload["live_chart_cache"]["cache_hit"])
        self.assertEqual(payload["live_chart_cache"]["cache_policy"], "immutable_history")
        self.assertEqual(payload["live_chart_bars"][0]["close"], 224.0)

    def test_cached_chart_bars_merge_runtime_sample_without_refetching(self) -> None:
        cached = [
            {
                "symbol": "XRPJPY",
                "open_time": 0,
                "close_time": 599_999,
                "open": 220.0,
                "high": 225.0,
                "low": 219.0,
                "close": 224.0,
                "volume": 10.0,
                "sample_count": 2,
                "source": "aggregate:10m",
            }
        ]
        runtime = [
            {
                "symbol": "XRPJPY",
                "open_time": 0,
                "close_time": 599_999,
                "open": 223.0,
                "high": 226.0,
                "low": 222.0,
                "close": 225.5,
                "volume": 0.0,
                "sample_count": 4,
                "source": "runtime_sample",
            }
        ]

        merged = _merge_chart_bars(cached, runtime, limit=10)

        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["open"], 220.0)
        self.assertEqual(merged[0]["high"], 226.0)
        self.assertEqual(merged[0]["low"], 219.0)
        self.assertEqual(merged[0]["close"], 225.5)
        self.assertEqual(merged[0]["volume"], 10.0)
        self.assertIn("runtime_sample", merged[0]["source"])
        merged_again = _merge_chart_bars(merged, runtime, limit=10)
        self.assertEqual(merged_again[0]["source"].count("runtime_sample"), 1)

    def test_sample_rows_keeps_first_last_and_limits_payload(self) -> None:
        rows = [{"i": index} for index in range(1000)]

        sampled = _sample_rows(rows, 100)

        self.assertEqual(len(sampled), 100)
        self.assertEqual(sampled[0]["i"], 0)
        self.assertEqual(sampled[-1]["i"], 999)

    def test_chart_cache_tail_refresh_only_when_latest_report_passes_cached_close(self) -> None:
        fresh_enough = {
            "fetched_at": 1.0,
            "bars": [{"open_time": 0, "close_time": 599_999, "close": 224.0}],
        }
        stale = {
            "fetched_at": 1.0,
            "bars": [{"open_time": 0, "close_time": 59_999, "close": 224.0}],
        }

        self.assertFalse(
            _chart_cache_needs_tail_refresh(
                fresh_enough,
                "1m",
                {"timestamp_ms": 120_000},
            )
        )
        self.assertTrue(
            _chart_cache_needs_tail_refresh(
                stale,
                "1m",
                {"timestamp_ms": 120_000},
            )
        )

    def test_aggregate_chart_bars_builds_non_native_10m_candles(self) -> None:
        bars = [
            {"symbol": "XRPJPY", "open_time": 0, "close_time": 299_999, "open": 100, "high": 105, "low": 99, "close": 103, "volume": 2},
            {"symbol": "XRPJPY", "open_time": 300_000, "close_time": 599_999, "open": 103, "high": 108, "low": 101, "close": 107, "volume": 3},
            {"symbol": "XRPJPY", "open_time": 600_000, "close_time": 899_999, "open": 107, "high": 109, "low": 106, "close": 108, "volume": 4},
        ]

        aggregated = _aggregate_chart_bars(bars, symbol="XRPJPY", interval="10m", limit=10)

        self.assertEqual(len(aggregated), 2)
        self.assertEqual(aggregated[0]["open"], 100)
        self.assertEqual(aggregated[0]["high"], 108)
        self.assertEqual(aggregated[0]["low"], 99)
        self.assertEqual(aggregated[0]["close"], 107)
        self.assertEqual(aggregated[0]["volume"], 5)

    def test_extract_live_trade_veto_markers_and_bars(self) -> None:
        history = [
            {
                "timestamp_ms": 1_000,
                "market_prices": {"XRPJPY": 220.0},
                "decisions": [
                    {
                        "symbol": "XRPJPY",
                        "signal": {"symbol": "XRPJPY", "action": "BUY", "reason": "bullish_cross"},
                        "execution_result": {
                            "status": "PAPER_FILLED",
                            "timestamp_ms": 1_020,
                            "symbol": "XRPJPY",
                            "side": "BUY",
                            "fill_price": 220.0,
                            "quantity": 0.4,
                            "trigger": "grid_profit_sell",
                        },
                    }
                ],
                "ai_risk_assessments": [],
            },
            {
                "timestamp_ms": 2_000,
                "market_prices": {"XRPJPY": 222.0},
                "decisions": [
                    {
                        "symbol": "XRPJPY",
                        "signal": {"symbol": "XRPJPY", "action": "BUY", "reason": "bullish_cross"},
                        "execution_result": {"status": "BLOCKED", "reason": "ai_entry_veto"},
                    }
                ],
                "ai_risk_assessments": [
                    {
                        "symbol": "XRPJPY",
                        "allow_entry": False,
                        "risk_score": 0.77,
                        "veto_reason": "新闻风险过高",
                    }
                ],
            },
            {
                "timestamp_ms": 4_000,
                "market_prices": {"XRPJPY": 218.0},
                "decisions": [
                    {
                        "symbol": "XRPJPY",
                        "signal": {"symbol": "XRPJPY", "action": "HOLD", "reason": "wait"},
                        "execution_result": {"status": "SKIPPED_REFRESH_ONLY"},
                    }
                ],
                "ai_risk_assessments": [],
            },
        ]

        trade_markers = _extract_live_trade_markers(history)
        activation_markers = _extract_position_activation_markers(history)
        veto_markers = _extract_live_ai_veto_markers(history)
        bars = _build_live_main_interval_bars(history, symbol="XRPJPY", interval="1m")

        self.assertEqual(len(trade_markers), 1)
        self.assertEqual(trade_markers[0]["side"], "BUY")
        self.assertEqual(trade_markers[0]["price"], 220.0)
        self.assertEqual(activation_markers[0]["trigger"], "grid_profit_sell")

        self.assertEqual(len(veto_markers), 1)
        self.assertEqual(veto_markers[0]["reason"], "新闻风险过高")
        self.assertEqual(veto_markers[0]["price"], 222.0)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["open"], 220.0)
        self.assertEqual(bars[0]["high"], 222.0)
        self.assertEqual(bars[0]["low"], 218.0)
        self.assertEqual(bars[0]["close"], 218.0)

    def test_live_bars_fall_back_to_latest_report_real_klines(self) -> None:
        latest_report = {
            "market_snapshots": [
                {
                    "symbol": "XRPJPY",
                    "main_interval_bars": [
                        {
                            "symbol": "XRPJPY",
                            "open_time": 1_000,
                            "close_time": 1_999,
                            "open": 220.0,
                            "high": 225.0,
                            "low": 219.0,
                            "close": 224.0,
                            "volume": 10.0,
                        },
                        {
                            "symbol": "XRPJPY",
                            "open_time": 2_000,
                            "close_time": 2_999,
                            "open": 224.0,
                            "high": 226.0,
                            "low": 223.0,
                            "close": 225.0,
                            "volume": 12.0,
                        },
                    ],
                }
            ]
        }

        bars = _build_live_main_interval_bars([], symbol="XRPJPY", interval="1m", latest_report=latest_report)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["open"], 220.0)
        self.assertEqual(bars[0]["high"], 225.0)
        self.assertEqual(bars[0]["low"], 219.0)
        self.assertEqual(bars[0]["close"], 224.0)
        self.assertEqual(bars[0]["source"], "binance_kline")

    def test_live_bars_merge_runtime_sample_without_flattening_real_kline(self) -> None:
        latest_report = {
            "market_snapshots": [
                {
                    "symbol": "XRPJPY",
                    "main_interval_bars": [
                        {
                            "symbol": "XRPJPY",
                            "open_time": 60_000,
                            "close_time": 119_999,
                            "open": 220.0,
                            "high": 225.0,
                            "low": 219.0,
                            "close": 224.0,
                            "volume": 10.0,
                        }
                    ],
                }
            ]
        }
        history = [{"timestamp_ms": 90_000, "market_prices": {"XRPJPY": 223.0}}]

        bars = _build_live_main_interval_bars(history, symbol="XRPJPY", interval="1m", latest_report=latest_report)

        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0]["open"], 220.0)
        self.assertEqual(bars[0]["high"], 225.0)
        self.assertEqual(bars[0]["low"], 219.0)
        self.assertEqual(bars[0]["close"], 223.0)
        self.assertEqual(bars[0]["source"], "binance_kline+runtime_sample")


if __name__ == "__main__":
    unittest.main()
