import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from binance_ai.config import Settings
from binance_ai.data.market_data import MarketDataService
from binance_ai.engine.decision_scheduler import DecisionScheduler
from binance_ai.engine.trading_engine import TradingEngine
from binance_ai.execution.executor import OrderExecutor
from binance_ai.models import AiRiskAssessment, Candle, LlmAnalysis, NewsItem, OrderRequest, SignalAction, SymbolFilters, TradeSignal
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


class _QuantizingClientStub(_ClientStub):
    def get_symbol_filters(self, symbol: str) -> SymbolFilters:
        return SymbolFilters(symbol=symbol, step_size=0.1, min_qty=0.1, min_notional=10.0)

    def quantize_quantity(self, quantity: float, step_size: float) -> float:
        return int(quantity / step_size) * step_size


class _MarketDataStub(MarketDataService):
    def __init__(self, candles):
        self._candles = candles

    def recent_candles(self, symbol: str, interval: str, limit: int):
        return list(self._candles)

    def recent_candles_by_interval(self, symbol: str, intervals, limit: int):
        return {interval: list(self._candles) for interval in intervals}


class _SequencedMarketDataStub(MarketDataService):
    def __init__(self, sequences):
        self._sequences = sequences
        self._calls = 0

    def recent_candles(self, symbol: str, interval: str, limit: int):
        return list(self._sequences[min(self._calls, len(self._sequences) - 1)])

    def recent_candles_by_interval(self, symbol: str, intervals, limit: int):
        candles = list(self._sequences[min(self._calls, len(self._sequences) - 1)])
        self._calls += 1
        return {interval: candles for interval in intervals}


class _StrategyStub(Strategy):
    def generate(self, symbol: str, candles_by_interval, has_position: bool) -> TradeSignal:
        if has_position:
            return TradeSignal(symbol=symbol, action=SignalAction.HOLD, confidence=0.5, reason="position_open")
        return TradeSignal(symbol=symbol, action=SignalAction.BUY, confidence=0.8, reason="entry_ready")


class _HoldStrategyStub(Strategy):
    def generate(self, symbol: str, candles_by_interval, has_position: bool) -> TradeSignal:
        return TradeSignal(symbol=symbol, action=SignalAction.HOLD, confidence=0.5, reason="hold")


class _SellStrategyStub(Strategy):
    def generate(self, symbol: str, candles_by_interval, has_position: bool) -> TradeSignal:
        return TradeSignal(symbol=symbol, action=SignalAction.SELL, confidence=0.8, reason="test_strategy_sell")


class _MarketAnalystStub:
    def __init__(self, allow_entry: bool, position_multiplier: float, veto_reason: str = "") -> None:
        self.allow_entry = allow_entry
        self.position_multiplier = position_multiplier
        self.veto_reason = veto_reason
        self.assess_calls = 0
        self.analyze_calls = 0

    def assess_entry_risk(self, quote_asset: str, kline_interval: str, market_snapshots, news_evidence):
        self.assess_calls += 1
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
        self.analyze_calls += 1
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
            trading_fee_rate=0.0,
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
            analyst = _MarketAnalystStub(allow_entry=True, position_multiplier=1.0)
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_StrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=scheduler,
                paper_portfolio=portfolio,
                market_analyst=analyst,
                news_service=None,
            )

            first_report = engine.run_cycle()
            second_report = engine.run_cycle()

        self.assertEqual(first_report.cycle_mode, "DECISION")
        self.assertEqual(first_report.decisions[0].execution_result["status"], "ORDER_OPEN")
        self.assertEqual(first_report.order_lifecycle_events[0].event_type, "SUBMITTED")
        self.assertEqual(first_report.open_orders[0].status, "OPEN")
        self.assertEqual(len(first_report.market_snapshots), 1)
        self.assertEqual(first_report.market_snapshots[0]["symbol"], "XRPJPY")
        self.assertEqual(second_report.cycle_mode, "REFRESH")
        self.assertEqual(second_report.decisions[0].execution_result["status"], "ORDER_OPEN")
        self.assertEqual(second_report.decisions[0].execution_result["reason"], "open_order_waiting_for_touch")
        self.assertEqual(second_report.scheduling_diagnostics[0].should_run_decision, False)
        self.assertEqual(len(second_report.sell_diagnostics), 1)
        self.assertEqual(len(second_report.decision_ledger), 1)
        self.assertEqual(second_report.decision_ledger[0].final_action, "OPEN_BUY")
        self.assertEqual(analyst.assess_calls, 1)
        self.assertEqual(analyst.analyze_calls, 1)
        self.assertEqual(second_report.llm_analysis.status, "SKIPPED_REFRESH_ONLY")

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
            trading_fee_rate=0.0,
            paper_quote_balance=20000.0,
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
        self.assertEqual(len(report.sell_diagnostics), 1)
        self.assertEqual(report.decision_ledger[0].execution_status, "BLOCKED")

    def test_position_activation_grid_sell_executes_in_paper_mode(self) -> None:
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
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.01,
            take_profit_pct=0.20,
            trailing_stop_pct=0.50,
            max_hold_bars=0,
            decision_price_move_threshold_pct=0.01,
        )
        candles = [
            Candle(
                open_time=index * 1000,
                open=100.31,
                high=100.31,
                low=100.31,
                close=100.31,
                volume=1.0,
                close_time=index * 1000 + 999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            portfolio.apply_order(
                order=OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=100.0),
                fill_price=100.0,
            )
            client = _QuantizingClientStub()
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_HoldStrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            report = engine.run_cycle()
            snapshot = portfolio.load_snapshot()

        self.assertEqual(report.decisions[0].execution_result["status"], "ORDER_OPEN")
        self.assertEqual(report.decisions[0].execution_result["trigger"], "grid_profit_sell")
        self.assertEqual(report.sell_diagnostics[0].activation_trigger, "grid_profit_sell")
        self.assertAlmostEqual(snapshot.activation_state["XRPJPY"].get("pending_buyback_quantity", 0.0), 0.0)
        self.assertEqual(len(snapshot.open_orders), 1)

    def test_strategy_sell_fill_registers_pending_buyback(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=250,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.50,
            take_profit_pct=0.001,
            trailing_stop_pct=0.50,
            max_hold_bars=999,
            decision_price_move_threshold_pct=0.0,
            order_passive_offset_pct=0.0002,
        )
        first_candles = [
            Candle(
                open_time=index * 60_000,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1.0,
                close_time=index * 60_000 + 59_999,
            )
            for index in range(1, 60)
        ]
        second_candles = first_candles + [
            Candle(
                open_time=60 * 60_000,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.7,
                volume=1.0,
                close_time=60 * 60_000 + 59_999,
            )
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            portfolio.apply_order(
                order=OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=100.0),
                fill_price=100.0,
            )
            client = _QuantizingClientStub()
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_SequencedMarketDataStub([first_candles, second_candles]),
                strategy=_SellStrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            first_report = engine.run_cycle()
            second_report = engine.run_cycle()
            snapshot = portfolio.load_snapshot()

        self.assertEqual(first_report.decisions[0].execution_result["status"], "ORDER_OPEN")
        self.assertEqual(first_report.decisions[0].execution_result["trigger"], "strategy_sell")
        self.assertTrue(any(event.status == "FILLED" and event.trigger == "strategy_sell" for event in second_report.order_lifecycle_events))
        state = snapshot.activation_state["XRPJPY"]
        self.assertEqual(state["last_trigger"], "grid_wait_buyback")
        self.assertIn("等待回补", state["last_reason"])
        self.assertAlmostEqual(state["pending_buyback_quantity"], 50.0)
        self.assertAlmostEqual(state["last_grid_sell_price"], first_report.decisions[0].execution_result["limit_price"])
        self.assertAlmostEqual(snapshot.positions["XRPJPY"].quantity, 50.0)

    def test_pending_buyback_blocks_repeated_strategy_sell(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=250,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.50,
            take_profit_pct=0.001,
            trailing_stop_pct=0.50,
            max_hold_bars=999,
            decision_price_move_threshold_pct=0.0,
        )
        candles = [
            Candle(
                open_time=index * 60_000,
                open=100.7,
                high=100.7,
                low=100.7,
                close=100.7,
                volume=1.0,
                close_time=index * 60_000 + 59_999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            portfolio.apply_order(
                order=OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=50.0),
                fill_price=100.0,
            )
            snapshot = portfolio.load_snapshot()
            portfolio.save_snapshot(
                replace(
                    snapshot,
                    activation_state={
                        "XRPJPY": {
                            "daily_trade_day": "2026-05-10",
                            "daily_trade_count": 1,
                            "pending_buyback_quantity": 50.0,
                            "last_grid_sell_price": 100.0,
                        }
                    },
                )
            )
            engine = TradingEngine(
                settings=settings,
                client=_QuantizingClientStub(),
                market_data=_MarketDataStub(candles),
                strategy=_SellStrategyStub(),
                risk=RiskEngine(settings, _QuantizingClientStub()),
                executor=OrderExecutor(settings, _QuantizingClientStub(), paper_portfolio=portfolio),
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            report = engine.run_cycle()
            snapshot = portfolio.load_snapshot()

        self.assertEqual(report.decisions[0].signal.action, SignalAction.HOLD)
        self.assertIn("暂停继续策略释放卖出", report.decisions[0].signal.reason)
        self.assertEqual(report.decisions[0].execution_result["status"], "NO_ACTION")
        self.assertEqual(snapshot.open_orders, {})
        self.assertAlmostEqual(snapshot.positions["XRPJPY"].quantity, 50.0)
        self.assertAlmostEqual(snapshot.activation_state["XRPJPY"]["pending_buyback_quantity"], 50.0)

    def test_buyback_cooldown_blocks_strategy_sell(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=250,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.50,
            take_profit_pct=0.50,
            trailing_stop_pct=0.50,
            max_hold_bars=999,
            decision_price_move_threshold_pct=0.0,
        )
        candles = [
            Candle(
                open_time=index * 60_000,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1.0,
                close_time=index * 60_000 + 59_999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            portfolio.apply_order(
                order=OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=50.0),
                fill_price=100.0,
            )
            snapshot = portfolio.load_snapshot()
            portfolio.save_snapshot(
                replace(
                    snapshot,
                    activation_state={
                        "XRPJPY": {
                            "decision_state": "BUYBACK_COOLDOWN",
                            "buyback_cooldown_until_candle": 9_999_999_999_999,
                            "pending_buyback_quantity": 0.0,
                            "last_grid_sell_price": 0.0,
                        }
                    },
                )
            )
            client = _QuantizingClientStub()
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_SellStrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            report = engine.run_cycle()

        self.assertEqual(report.decisions[0].signal.action, SignalAction.HOLD)
        self.assertIn("回补冷却保护", report.decisions[0].signal.reason)
        self.assertEqual(report.decisions[0].execution_result["status"], "NO_ACTION")
        self.assertGreater(report.decision_ledger[0].cooldown_remaining_bars, 0)

    def test_ai_regular_veto_does_not_cancel_grid_buyback_order(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=250,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="http://localhost:49530/v1",
            llm_api_key="demo",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.50,
            take_profit_pct=0.50,
            trailing_stop_pct=0.50,
            max_hold_bars=999,
            decision_price_move_threshold_pct=0.0,
            ai_can_cancel_buyback=False,
            order_reprice_enabled=False,
        )
        candles = [
            Candle(
                open_time=index * 60_000,
                open=100.0,
                high=100.0,
                low=100.0,
                close=100.0,
                volume=1.0,
                close_time=index * 60_000 + 59_999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            client = _QuantizingClientStub()
            executor = OrderExecutor(settings, client, paper_portfolio=portfolio)
            executor.submit_limit_order(
                OrderRequest(
                    symbol="XRPJPY",
                    side="BUY",
                    order_type="LIMIT",
                    quantity=10.0,
                    limit_price=99.0,
                    client_order_id="grid-buyback",
                    trigger="grid_buyback",
                ),
                current_price=100.0,
                filters=SymbolFilters("XRPJPY", step_size=0.1, min_qty=0.1, min_notional=10.0),
                timestamp_ms=1_000,
            )
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_HoldStrategyStub(),
                risk=RiskEngine(settings, client),
                executor=executor,
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=_MarketAnalystStub(allow_entry=False, position_multiplier=0.0, veto_reason="普通风险"),
                news_service=None,
            )

            report = engine.run_cycle()
            open_order_count = len(executor.all_open_orders())

            self.assertEqual(report.decisions[0].execution_result["status"], "ORDER_OPEN")
            self.assertEqual(report.decisions[0].execution_result["trigger"], "grid_buyback")
            self.assertEqual(open_order_count, 1)

    def test_pending_buyback_order_takes_priority_over_strategy_sell(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=250,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.50,
            take_profit_pct=0.001,
            trailing_stop_pct=0.50,
            max_hold_bars=999,
            decision_price_move_threshold_pct=0.0,
            grid_buyback_step_pct=0.0025,
        )
        candles = [
            Candle(
                open_time=index * 60_000,
                open=100.7,
                high=100.7,
                low=100.7,
                close=100.7,
                volume=1.0,
                close_time=index * 60_000 + 59_999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            portfolio.apply_order(
                order=OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=50.0),
                fill_price=100.0,
            )
            snapshot = portfolio.load_snapshot()
            portfolio.save_snapshot(
                replace(
                    snapshot,
                    activation_state={
                        "XRPJPY": {
                            "daily_trade_day": "2026-05-10",
                            "daily_trade_count": 1,
                            "pending_buyback_quantity": 50.0,
                            "last_grid_sell_price": 101.0,
                        }
                    },
                )
            )
            client = _QuantizingClientStub()
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_SellStrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            report = engine.run_cycle()
            snapshot = portfolio.load_snapshot()

        self.assertEqual(report.decisions[0].signal.action, SignalAction.HOLD)
        self.assertEqual(report.decisions[0].execution_result["status"], "ORDER_OPEN")
        self.assertEqual(report.decisions[0].execution_result["trigger"], "grid_buyback")
        self.assertEqual(next(iter(snapshot.open_orders.values())).side, "BUY")

    def test_pending_buyback_blocks_repeated_partial_exit_rules(self) -> None:
        settings = Settings(
            api_key="",
            api_secret="",
            base_url="https://api.binance.com",
            recv_window=5000,
            trading_symbols=["XRPJPY"],
            max_active_symbols=3,
            quote_asset="JPY",
            kline_interval="1m",
            kline_limit=250,
            fast_window=3,
            slow_window=9,
            risk_per_trade=0.10,
            min_order_notional=10.0,
            trading_fee_rate=0.0,
            paper_quote_balance=1000.0,
            dry_run=True,
            llm_base_url="",
            llm_api_key="",
            llm_model="gpt-5.5",
            llm_timeout_seconds=20,
            news_refresh_seconds=120,
            stop_loss_pct=0.50,
            take_profit_pct=0.001,
            trailing_stop_pct=0.50,
            max_hold_bars=0,
            decision_price_move_threshold_pct=0.0,
        )
        candles = [
            Candle(
                open_time=index * 60_000,
                open=100.7,
                high=100.7,
                low=100.7,
                close=100.7,
                volume=1.0,
                close_time=index * 60_000 + 59_999,
            )
            for index in range(1, 60)
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_dir = Path(tmpdir)
            portfolio = PaperPortfolio("JPY", 20000.0, runtime_dir / "paper_state.json")
            portfolio.apply_order(
                order=OrderRequest(symbol="XRPJPY", side="BUY", order_type="MARKET", quantity=50.0),
                fill_price=100.0,
                timestamp_ms=1,
                entry_candle_close_time_ms=1,
            )
            snapshot = portfolio.load_snapshot()
            portfolio.save_snapshot(
                replace(
                    snapshot,
                    activation_state={
                        "XRPJPY": {
                            "daily_trade_day": "2026-05-10",
                            "daily_trade_count": 1,
                            "pending_buyback_quantity": 50.0,
                            "last_grid_sell_price": 100.0,
                        }
                    },
                )
            )
            client = _QuantizingClientStub()
            engine = TradingEngine(
                settings=settings,
                client=client,
                market_data=_MarketDataStub(candles),
                strategy=_HoldStrategyStub(),
                risk=RiskEngine(settings, client),
                executor=OrderExecutor(settings, client, paper_portfolio=portfolio),
                scheduler=DecisionScheduler(runtime_dir / "decision_state.json", settings.decision_price_move_threshold_pct),
                paper_portfolio=portfolio,
                market_analyst=None,
                news_service=None,
            )

            report = engine.run_cycle()
            snapshot = portfolio.load_snapshot()

        self.assertEqual(report.decisions[0].signal.action, SignalAction.HOLD)
        self.assertIn("暂停继续部分退出", report.decisions[0].signal.reason)
        self.assertEqual(report.decisions[0].execution_result["status"], "NO_ACTION")
        self.assertEqual(snapshot.open_orders, {})
        self.assertAlmostEqual(snapshot.positions["XRPJPY"].quantity, 50.0)


if __name__ == "__main__":
    unittest.main()
