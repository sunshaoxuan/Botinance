from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from binance_ai.dashboard_server import (
    INDEX_HTML,
    _build_live_main_interval_bars,
    _extract_position_activation_markers,
    _extract_live_ai_veto_markers,
    _extract_live_trade_markers,
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
        self.assertIn("trade-fill-panel", INDEX_HTML)
        self.assertIn("insight-drawer", INDEX_HTML)
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
                            "created_at_ms": 1_000,
                            "updated_at_ms": 1_000,
                            "expires_at_ms": 10_000,
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
        self.assertEqual(len(payload["order_markers"]), 1)
        self.assertEqual(payload["order_markers"][0]["status"], "OPEN")

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
        self.assertEqual(payload["order_markers"], [])

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
