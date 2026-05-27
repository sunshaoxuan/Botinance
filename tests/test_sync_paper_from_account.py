from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from binance_ai.tools.sync_paper_from_account import (
    build_cash_baseline_snapshot_from_balances,
    build_paper_snapshot_from_balances,
    calculate_cash_baseline_equity,
    clear_simulated_runtime,
    infer_remaining_cost_basis_from_trades,
    parse_asset_minimums,
    validate_account_sync_requirements,
)


class SyncPaperFromAccountTests(unittest.TestCase):
    def test_build_paper_snapshot_uses_real_balances_and_current_prices(self) -> None:
        snapshot = build_paper_snapshot_from_balances(
            balances={"JPY": 188.99, "XRP": 114.9, "BTC": 0.0},
            symbols=["XRPJPY", "BTCJPY"],
            quote_asset="JPY",
            prices={"XRPJPY": 224.0, "BTCJPY": 15_000_000.0},
            timestamp_ms=1234567890,
        )

        self.assertEqual(snapshot.quote_asset, "JPY")
        self.assertAlmostEqual(snapshot.quote_balance, 188.99)
        self.assertEqual(set(snapshot.positions), {"XRPJPY"})
        self.assertAlmostEqual(snapshot.positions["XRPJPY"].quantity, 114.9)
        self.assertAlmostEqual(snapshot.positions["XRPJPY"].average_entry_price, 224.0)
        self.assertAlmostEqual(snapshot.initial_quote_balance, 188.99 + 114.9 * 224.0)
        self.assertEqual(snapshot.realized_pnl, 0.0)
        self.assertEqual(snapshot.activation_state["XRPJPY"]["cost_basis_source"], "sync_current_price")
        release = snapshot.activation_state["XRPJPY"]["initial_inventory_release"]
        self.assertTrue(release["enabled"])
        self.assertAlmostEqual(release["remaining_quantity"], 114.9)

    def test_build_cash_baseline_snapshot_converts_inventory_to_quote(self) -> None:
        snapshot = build_cash_baseline_snapshot_from_balances(
            balances={"JPY": 188.99, "XRP": 114.9, "BTC": 0.0},
            symbols=["XRPJPY", "BTCJPY"],
            quote_asset="JPY",
            prices={"XRPJPY": 224.0, "BTCJPY": 15_000_000.0},
        )

        expected_cash = 188.99 + 114.9 * 224.0
        self.assertEqual(snapshot.quote_asset, "JPY")
        self.assertAlmostEqual(snapshot.quote_balance, expected_cash)
        self.assertAlmostEqual(snapshot.initial_quote_balance, expected_cash)
        self.assertEqual(snapshot.positions, {})
        self.assertEqual(snapshot.fills, [])
        self.assertEqual(snapshot.realized_pnl, 0.0)
        self.assertEqual(snapshot.activation_state["_baseline"]["mode"], "cash_baseline_after_forced_paper_liquidation")

    def test_validate_account_sync_requirements_rejects_stale_balances(self) -> None:
        balances = {"JPY": 188.99, "XRP": 114.9}
        cash = calculate_cash_baseline_equity(
            balances=balances,
            symbols=["XRPJPY"],
            quote_asset="JPY",
            prices={"XRPJPY": 224.0},
        )
        self.assertAlmostEqual(cash, 188.99 + 114.9 * 224.0)

        with self.assertRaisesRegex(RuntimeError, "Account sync validation failed"):
            validate_account_sync_requirements(
                balances=balances,
                cash_baseline=cash,
                min_cash_baseline=cash + 1.0,
                min_assets={},
            )

        with self.assertRaisesRegex(RuntimeError, "XRP balance"):
            validate_account_sync_requirements(
                balances=balances,
                cash_baseline=cash,
                min_assets={"XRP": 115.0},
            )

    def test_parse_asset_minimums(self) -> None:
        self.assertEqual(parse_asset_minimums(["xrp:115", "jpy:200"]), {"XRP": 115.0, "JPY": 200.0})
        with self.assertRaises(ValueError):
            parse_asset_minimums(["XRP"])

    def test_build_paper_snapshot_keeps_trade_cost_basis_as_metadata(self) -> None:
        snapshot = build_paper_snapshot_from_balances(
            balances={"JPY": 100.0, "XRP": 10.0},
            symbols=["XRPJPY"],
            quote_asset="JPY",
            prices={"XRPJPY": 224.0},
            timestamp_ms=1234567890,
            cost_basis_by_symbol={
                "XRPJPY": {
                    "source": "binance_my_trades_fifo",
                    "average_entry_price": 200.0,
                }
            },
        )

        self.assertAlmostEqual(snapshot.positions["XRPJPY"].average_entry_price, 224.0)
        self.assertEqual(snapshot.activation_state["XRPJPY"]["cost_basis_source"], "binance_my_trades_fifo")
        self.assertAlmostEqual(snapshot.activation_state["XRPJPY"]["real_average_entry_price"], 200.0)
        self.assertAlmostEqual(snapshot.activation_state["XRPJPY"]["seed_price"], 224.0)

    def test_infer_remaining_cost_basis_from_trades_uses_remaining_fifo_lots(self) -> None:
        trades = [
            {"time": 1, "isBuyer": True, "qty": "10", "price": "100"},
            {"time": 2, "isBuyer": True, "qty": "10", "price": "120"},
            {"time": 3, "isBuyer": False, "qty": "5", "price": "130"},
        ]

        basis = infer_remaining_cost_basis_from_trades(trades, current_quantity=15.0)

        self.assertEqual(basis["source"], "binance_my_trades_fifo")
        self.assertAlmostEqual(basis["average_entry_price"], ((5 * 100) + (10 * 120)) / 15)

    def test_clear_simulated_runtime_archives_only_active_simulated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = root / "runtime_visual"
            runtime.mkdir()
            for name in ("cycle_reports.jsonl", "latest_report.json", "paper_state.json"):
                (runtime / name).write_text("simulated", encoding="utf-8")
            (runtime / "news_cache.json").write_text("real-news-cache", encoding="utf-8")

            cleared = clear_simulated_runtime(runtime, root / "runtime_resets")

            self.assertEqual(set(cleared), {"cycle_reports.jsonl", "latest_report.json", "paper_state.json"})
            self.assertFalse((runtime / "cycle_reports.jsonl").exists())
            self.assertFalse((runtime / "latest_report.json").exists())
            self.assertFalse((runtime / "paper_state.json").exists())
            self.assertEqual((runtime / "news_cache.json").read_text(encoding="utf-8"), "real-news-cache")
            archived = list((root / "runtime_resets").glob("*/runtime_visual/cycle_reports.jsonl"))
            self.assertEqual(len(archived), 1)


if __name__ == "__main__":
    unittest.main()
