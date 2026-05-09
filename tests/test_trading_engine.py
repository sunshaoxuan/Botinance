import tempfile
import unittest
from pathlib import Path

from binance_ai.config import Settings
from binance_ai.data.market_data import MarketDataService
from binance_ai.engine.decision_scheduler import DecisionScheduler
from binance_ai.engine.trading_engine import TradingEngine
from binance_ai.execution.executor import OrderExecutor
from binance_ai.models import AiRiskAssessment, Candle, LlmAnalysis, NewsItem, SignalAction, SymbolFilters, TradeSignal
from binance_ai.paper.portfolio import PaperPortfolio
from binance_ai.risk.engine import RiskEngine
from binance_ai.strategy.base import Strategy


class _ClientStub:
    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(symbol=symbol, step_size=0.1, min_qty=0.1, min_notional=50.0)

    def quantize_quantity(self, quantity: float, step_size: float) -> float:
        return 0.5

    def close(self) -> None:
        return None


class _MarketDataStub(MarketDataService):
    def __init__(self, candles):
        self._candles = candles

    def recent_candles(self, symbol: str, interval: str, limit: int):
        return list(self._candles)


class _StrategyStub(Strategy):
    def generate(self, symbol: str, candles, has_position: bool) -> TradeSignal:
        if has_position:
            return TradeSignal(symbol=symbol, action=SignalAction.HOLD, confidence=0.5, reason="position_open")
        return TradeSignal(symbol=symbol, action=SignalAction.BUY, confidence=0.8, reason="entry_ready")


class _MarketAnalystStub:
    def __init__(self, allow_entry: bool, position_multiplier: float, veto_reason: str = "") -> None:
        self.allow_entry = allow_entry
        self.position_multiplier = position_multiplier
        self.veto_reason = veto_reason

    def assess_entry_risk(self, quote_asset: str, kline_interval: str, market_snapshots, news_evidence):
        return {
            snapshot["symbol"]: AiRiskAssessment(
                symbol=snapshot["symbol"],
                status="READY",
                allow_entry=self.allow_entry,
                risk_score=0.8 if not self.allow_entry else 0.2,
                position_multiplier=self.position_multiplier,
                veto_reason=self.veto_reason,
            )
            for snapshot in market_snapshots
        }

    def analyze(self, quote_asset: str, kline_interval: str, market_snapshots, news_evidence):
        return LlmAnalysis(
            status="READY",
            provider="openai_compat",
            model="gpt-5.5",
            regime_cn="测试",
            summary_cn="测试摘要",
            action_bias_cn="观望",
            confidence=0.5,
            risk_note_cn="测试风险",
        )


class TradingEngineSchedulingTests(unittest.TestCase):
    def test_refresh_cycle_skips_order_execution_until_new_trigger(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1h",
            kline_limit=250,
            fast_window=20,
            slow_window=50,
            risk_per_trade=0.10,
            min_order_notional=50.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            decision_price_move_threshold_pct=0.01,
        )
        candles = [
            Candle(
                open_time=index * 1000,
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.0 + index,
                volume=1.0,
                close_time=index * 1000 + 999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=runtime_dir / "paper_state.json",
            )
            client = _ClientStub()
            scheduler = DecisionScheduler(
                state_path=runtime_dir / "decision_state.json",
                price_move_threshold_pct=settings.decision_price_move_threshold_pct,
            )
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_StrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=scheduler,
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            first_report = engine.run_cycle()
            second_report = engine.run_cycle()

        self.assertEqual(first_report.cycle_mode, "DECISION")
        self.assertEqual(first_report.decisions[0].execution_result["status"], "PAPER_FILLED")
        self.assertEqual(second_report.cycle_mode, "REFRESH")
        self.assertEqual(second_report.decisions[0].execution_result["status"], "SKIPPED_REFRESH_ONLY")
        self.assertEqual(second_report.scheduling_diagnostics[0].should_run_decision, False)

    def test_ai_veto_blocks_buy_order(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1h",
            kline_limit=250,
            fast_window=20,
            slow_window=50,
            risk_per_trade=0.10,
            min_order_notional=50.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="http://localhost:49530/v1",
            llm_api_key="demo",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.02,
            trailing_stop_pct=0.0075,
            max_hold_bars=24,
            decision_price_move_threshold_pct=0.01,
        )
        candles = [
            Candle(
                open_time=index * 1000,
                open=100.0 + index,
                high=101.0 + index,
                low=99.0 + index,
                close=100.0 + index,
                volume=1.0,
                close_time=index * 1000 + 999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio(
                quote_asset="JPY",
                initial_quote_balance=1000.0,
                state_path=runtime_dir / "paper_state.json",
            )
            client = _ClientStub()
            scheduler = DecisionScheduler(
                state_path=runtime_dir / "decision_state.json",
                price_move_threshold_pct=settings.decision_price_move_threshold_pct,
            )
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_StrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=scheduler,
                paper_portfolio=portfolio,
                market_analyst=_MarketAnalystStub(allow_entry=False, position_multiplier=0.0, veto_reason="新闻风险过高"),
                news_service=None,
            )

            report = engine.run_cycle()

        self.assertEqual(report.decisions[0].execution_result["status"], "BLOCKED")
        self.assertEqual(report.decisions[0].execution_result["reason"], "ai_entry_veto")
        self.assertFalse(report.ai_risk_assessments[0].allow_entry)
        self.assertEqual(report.buy_diagnostics[0].ai_veto_reason, "新闻风险过高")


if __name__ == "__main__":
    unittest.main()
