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
