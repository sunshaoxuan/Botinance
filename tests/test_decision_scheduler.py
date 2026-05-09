import tempfile
import unittest
from pathlib import Path

from binance_ai.engine.decision_scheduler import DecisionScheduler


class DecisionSchedulerTests(unittest.TestCase):
    def test_first_cycle_runs_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            scheduler = DecisionScheduler(Path(tmpdir) / "decision_state.json", price_move_threshold_pct=0.005)
            diagnostic = scheduler.evaluate(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=100.0,
                has_position=False,
                exit_reason=None,
            )
        self.assertTrue(diagnostic.should_run_decision)
        self.assertIn("首次启动", diagnostic.decision_reason)

    def test_same_candle_without_threshold_becomes_refresh_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "decision_state.json"
            scheduler = DecisionScheduler(state_path, price_move_threshold_pct=0.005)
            scheduler.record_decision(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=100.0,
                timestamp_ms=1111,
            )
            scheduler.save()

            reloaded = DecisionScheduler(state_path, price_move_threshold_pct=0.005)
            diagnostic = reloaded.evaluate(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=100.2,
                has_position=False,
                exit_reason=None,
            )
        self.assertFalse(diagnostic.should_run_decision)
        self.assertIn("无新 K 线", diagnostic.decision_reason)

    def test_price_threshold_forces_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "decision_state.json"
            scheduler = DecisionScheduler(state_path, price_move_threshold_pct=0.005)
            scheduler.record_decision(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=100.0,
                timestamp_ms=1111,
            )
            scheduler.save()

            reloaded = DecisionScheduler(state_path, price_move_threshold_pct=0.005)
            diagnostic = reloaded.evaluate(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=100.8,
                has_position=False,
                exit_reason=None,
            )
        self.assertTrue(diagnostic.should_run_decision)
        self.assertTrue(diagnostic.threshold_triggered)

    def test_exit_reason_forces_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "decision_state.json"
            scheduler = DecisionScheduler(state_path, price_move_threshold_pct=0.02)
            scheduler.record_decision(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=100.0,
                timestamp_ms=1111,
            )
            scheduler.save()

            reloaded = DecisionScheduler(state_path, price_move_threshold_pct=0.02)
            diagnostic = reloaded.evaluate(
                symbol="XRPJPY",
                latest_closed_candle_close_time=1000,
                current_price=99.9,
                has_position=True,
                exit_reason="stop_loss",
            )
        self.assertTrue(diagnostic.should_run_decision)
        self.assertTrue(diagnostic.exit_triggered)


if __name__ == "__main__":
    unittest.main()
