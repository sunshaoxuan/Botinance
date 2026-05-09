import tempfile
import unittest
from pathlib import Path
import json

from binance_ai.models import OrderRequest
from binance_ai.paper.portfolio import PaperPortfolio


class PaperPortfolioTests(unittest.TestCase):
    def test_buy_and_sell_updates_realized_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=Path(tmpdir) / "paper_state.json",
            )
            buy_result = portfolio.apply_order(
                OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=1.0),
                fill_price=200.0,
            )
            self.assertEqual(buy_result["status"], "PAPER_FILLED")

            sell_result = portfolio.apply_order(
                OrderRequest(symbol="XRPJPY", side="SELL", order_type="MARKET", quantity=1.0),
                fill_price=220.0,
            )
            self.assertEqual(sell_result["status"], "PAPER_FILLED")

            summary = portfolio.equity_summary({"XRPJPY": 220.0})
            self.assertAlmostEqual(summary["realized_pnl"], 20.0)
            self.assertAlmostEqual(summary["net_pnl"], 20.0)

    def test_buy_and_sell_apply_quote_fee_to_cash_cost_basis_and_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=Path(tmpdir) / "paper_state.json",
                fee_rate=0.001,
            )
            buy_result = portfolio.apply_order(
                OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=1.0),
                fill_price=200.0,
            )
            self.assertEqual(buy_result["status"], "PAPER_FILLED")
            self.assertAlmostEqual(buy_result["fee"], 0.2)
            snapshot_after_buy = portfolio.load_snapshot()
            self.assertAlmostEqual(snapshot_after_buy.quote_balance, 799.8)
            self.assertAlmostEqual(snapshot_after_buy.positions["XRPJPY"].average_entry_price, 200.2)

            sell_result = portfolio.apply_order(
                OrderRequest(symbol="XRPJPY", side="SELL", order_type="MARKET", quantity=1.0),
                fill_price=220.0,
            )
            self.assertEqual(sell_result["status"], "PAPER_FILLED")
            self.assertAlmostEqual(sell_result["fee"], 0.22)
            self.assertAlmostEqual(sell_result["realized_pnl_delta"], 19.58)

            summary = portfolio.equity_summary({"XRPJPY": 220.0})
            self.assertAlmostEqual(summary["realized_pnl"], 19.58)
            self.assertAlmostEqual(summary["net_pnl"], 19.58)

    def test_apply_order_blocks_notional_below_minimum(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=Path(tmpdir) / "paper_state.json",
            )
            result = portfolio.apply_order(
                OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=0.4),
                fill_price=222.66,
                min_notional=100.0,
                min_qty=0.1,
            )
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason"], "paper_order_below_min_notional")

    def test_limit_buy_lifecycle_locks_cash_then_fills(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=Path(tmpdir) / "paper_state.json",
                fee_rate=0.001,
            )
            order = OrderRequest(
                symbol="XRPJPY",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=200.0,
                client_order_id="buy-1",
                trigger="strategy_buy",
                expires_at_ms=10_000,
            )

            result, event = portfolio.submit_limit_order(order, timestamp_ms=1_000)
            snapshot = portfolio.load_snapshot()

            self.assertEqual(result["status"], "ORDER_OPEN")
            self.assertEqual(event.status, "OPEN")
            self.assertAlmostEqual(snapshot.reserved_quote_balance, 200.2)
            self.assertAlmostEqual(portfolio.account_snapshot().balance_of("JPY"), 799.8)

            fill_result, fill_event = portfolio.fill_open_order("buy-1", fill_price=200.0, timestamp_ms=2_000)
            snapshot = portfolio.load_snapshot()

            self.assertEqual(fill_result["status"], "PAPER_FILLED")
            self.assertEqual(fill_event.status, "FILLED")
            self.assertEqual(snapshot.open_orders, {})
            self.assertAlmostEqual(snapshot.reserved_quote_balance, 0.0)
            self.assertAlmostEqual(snapshot.quote_balance, 799.8)
            self.assertAlmostEqual(snapshot.positions["XRPJPY"].quantity, 1.0)

    def test_limit_sell_lifecycle_locks_base_then_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=Path(tmpdir) / "paper_state.json",
            )
            portfolio.apply_order(
                OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=2.0),
                fill_price=200.0,
            )
            order = OrderRequest(
                symbol="XRPJPY",
                side="SELL",
                order_type="LIMIT",
                quantity=1.0,
                limit_price=220.0,
                client_order_id="sell-1",
                trigger="grid_profit_sell",
                expires_at_ms=10_000,
            )

            result, _ = portfolio.submit_limit_order(order, timestamp_ms=1_000)
            snapshot = portfolio.load_snapshot()

            self.assertEqual(result["status"], "ORDER_OPEN")
            self.assertAlmostEqual(snapshot.reserved_base_balances["XRPJPY"], 1.0)
            self.assertAlmostEqual(portfolio.account_snapshot().balance_of("XRP"), 1.0)

            event = portfolio.cancel_open_order("sell-1", "test_cancel", timestamp_ms=2_000)
            snapshot = portfolio.load_snapshot()

            self.assertEqual(event.status, "CANCELED")
            self.assertEqual(snapshot.open_orders, {})
            self.assertEqual(snapshot.reserved_base_balances, {})
            self.assertAlmostEqual(portfolio.account_snapshot().balance_of("XRP"), 2.0)

    def test_load_snapshot_backfills_position_metadata_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "paper_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "quote_asset": "JPY",
                        "quote_balance": 910.936,
                        "initial_quote_balance": 1000.0,
                        "positions": {
                            "XRPJPY": {
                                "quantity": 0.4,
                                "average_entry_price": 222.66,
                            }
                        },
                        "realized_pnl": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            (Path(tmpdir) / "cycle_reports.jsonl").write_text(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "execution_result": {
                                    "status": "PAPER_FILLED",
                                    "symbol": "XRPJPY",
                                    "side": "BUY",
                                    "timestamp_ms": 1234567890,
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=state_path,
            )
            snapshot = portfolio.load_snapshot()
            self.assertEqual(snapshot.positions["XRPJPY"].opened_at_ms, 1234567890)
            self.assertEqual(snapshot.positions["XRPJPY"].entry_candle_close_time, 1234567890)

    def test_load_snapshot_corrects_later_incorrect_metadata_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "paper_state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "quote_asset": "JPY",
                        "quote_balance": 910.936,
                        "initial_quote_balance": 1000.0,
                        "positions": {
                            "XRPJPY": {
                                "quantity": 0.4,
                                "average_entry_price": 222.66,
                                "opened_at_ms": 9999999999,
                                "entry_candle_close_time": 9999999999,
                                "highest_price": 224.18,
                            }
                        },
                        "realized_pnl": 0.0,
                    }
                ),
                encoding="utf-8",
            )
            (Path(tmpdir) / "cycle_reports.jsonl").write_text(
                json.dumps(
                    {
                        "decisions": [
                            {
                                "execution_result": {
                                    "status": "PAPER_FILLED",
                                    "symbol": "XRPJPY",
                                    "side": "BUY",
                                    "timestamp_ms": 1234567890,
                                }
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=state_path,
            )
            snapshot = portfolio.load_snapshot()
            self.assertEqual(snapshot.positions["XRPJPY"].opened_at_ms, 1234567890)
            self.assertEqual(snapshot.positions["XRPJPY"].entry_candle_close_time, 1234567890)


if __name__ == "__main__":
    unittest.main()
