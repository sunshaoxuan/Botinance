from __future__ import annotations

from dataclasses import replace
import time
from typing import Dict, List

from binance_ai.config import Settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.data.market_data import MarketDataService
from binance_ai.engine.decision_scheduler import DecisionScheduler
from binance_ai.execution.executor import OrderExecutor
from binance_ai.llm.market_analyst import MarketAnalyst, build_market_snapshot
from binance_ai.models import AccountSnapshot, AiRiskAssessment, BuyDecisionDiagnostic, CycleDecision, CycleReport, DecisionLedgerEntry, LlmAnalysis, PositionDiagnostic, SchedulingDiagnostic, SellDecisionDiagnostic, SignalAction
from binance_ai.news.service import NewsService
from binance_ai.paper.portfolio import PaperPortfolio
from binance_ai.position_activation import PositionActivationDecision, PositionActivationEngine
from binance_ai.risk.engine import RiskEngine
from binance_ai.strategy.base import Strategy


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        client: BinanceSpotClient,
        market_data: MarketDataService,
        strategy: Strategy,
        risk: RiskEngine,
        executor: OrderExecutor,
        scheduler: DecisionScheduler,
        paper_portfolio: PaperPortfolio | None = None,
        market_analyst: MarketAnalyst | None = None,
        news_service: NewsService | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.market_data = market_data
        self.strategy = strategy
        self.risk = risk
        self.executor = executor
        self.scheduler = scheduler
        self.paper_portfolio = paper_portfolio
        self.market_analyst = market_analyst
        self.news_service = news_service
        self.position_activation = PositionActivationEngine(settings, client)

    def run_cycle(self) -> CycleReport:
        self.risk.ensure_symbol_limit()
        cycle_timestamp_ms = int(time.time() * 1000)
        account = self._load_account_snapshot()
        news_result = (
            self.news_service.collect_for_symbols(self.settings.trading_symbols, self.settings.quote_asset)
            if self.news_service is not None
            else None
        )
        news_evidence = news_result.items if news_result is not None else []
        decisions: List[CycleDecision] = []
        buy_diagnostics: List[BuyDecisionDiagnostic] = []
        sell_diagnostics: List[SellDecisionDiagnostic] = []
        position_diagnostics: List[PositionDiagnostic] = []
        scheduling_diagnostics: List[SchedulingDiagnostic] = []
        mark_prices: Dict[str, float] = {}
        market_snapshots: List[Dict[str, object]] = []
        symbol_contexts: List[Dict[str, object]] = []

        for symbol in self.settings.trading_symbols:
            candles_by_interval = self.market_data.recent_candles_by_interval(
                symbol=symbol,
                intervals=[
                    self.settings.kline_interval,
                    self.settings.mtf_entry_interval,
                    self.settings.mtf_trend_interval,
                ],
                limit=self.settings.kline_limit,
            )
            candles = candles_by_interval[self.settings.kline_interval]
            price = candles[-1].close
            mark_prices[symbol] = price
            latest_closed_candle_close_time = self._latest_closed_candle_close_time(
                candles=candles,
                current_timestamp_ms=cycle_timestamp_ms,
            )
            filters = self.client.get_symbol_filters(symbol)
            base_asset = self.risk.base_asset_for_symbol(symbol)
            base_balance = account.balance_of(base_asset) if base_asset else 0.0
            has_position = base_balance > 0.0
            if has_position and self.settings.dry_run and self.paper_portfolio is not None:
                self.paper_portfolio.mark_to_market(
                    symbol=symbol,
                    mark_price=price,
                    timestamp_ms=cycle_timestamp_ms,
                    candle_close_time_ms=latest_closed_candle_close_time,
                )
            position = (
                self.paper_portfolio.position_snapshot(symbol)
                if has_position and self.settings.dry_run and self.paper_portfolio is not None
                else None
            )
            if has_position and position is not None:
                position_diagnostics.append(
                    self.risk.build_position_diagnostic(
                        symbol=symbol,
                        price=price,
                        position=position,
                        candles=candles,
                        current_timestamp_ms=cycle_timestamp_ms,
                    )
                )

            signal = self.strategy.generate(symbol=symbol, candles_by_interval=candles_by_interval, has_position=has_position)
            exit_reason = (
                self.risk.determine_exit_reason(
                    price=price,
                    position=position,
                    candles=candles,
                    current_timestamp_ms=cycle_timestamp_ms,
                )
                if has_position and position is not None
                else None
            )
            activation_decision = self._evaluate_position_activation(
                symbol=symbol,
                price=price,
                account=account,
                filters=filters,
                timestamp_ms=cycle_timestamp_ms,
            )
            scheduler_exit_reason = exit_reason or (
                activation_decision.trigger if activation_decision.order is not None else None
            )
            scheduling = self.scheduler.evaluate(
                symbol=symbol,
                latest_closed_candle_close_time=latest_closed_candle_close_time,
                current_price=price,
                has_position=has_position,
                exit_reason=scheduler_exit_reason,
            )
            scheduling_diagnostics.append(scheduling)
            market_snapshot = build_market_snapshot(
                symbol=symbol,
                candles_by_interval=candles_by_interval,
                signal=signal,
                has_position=has_position,
                main_interval=self.settings.kline_interval,
                fast_window=self.settings.fast_window,
                slow_window=self.settings.slow_window,
                entry_interval=self.settings.mtf_entry_interval,
                entry_fast_window=self.settings.mtf_entry_fast_window,
                entry_slow_window=self.settings.mtf_entry_slow_window,
                trend_interval=self.settings.mtf_trend_interval,
                trend_fast_window=self.settings.mtf_trend_fast_window,
                trend_slow_window=self.settings.mtf_trend_slow_window,
            )
            market_snapshots.append(market_snapshot)
            symbol_contexts.append(
                {
                    "symbol": symbol,
                    "candles": candles,
                    "candles_by_interval": candles_by_interval,
                    "price": price,
                    "filters": filters,
                    "base_balance": base_balance,
                    "has_position": has_position,
                    "position": position,
                    "signal": signal,
                    "exit_reason": exit_reason,
                    "activation_decision": activation_decision,
                    "scheduling": scheduling,
                    "latest_closed_candle_close_time": latest_closed_candle_close_time,
                }
            )

        llm_analysis = None
        should_run_llm = any(item.should_run_decision for item in scheduling_diagnostics)
        ai_risk_map = {
            str(snapshot["symbol"]).upper(): AiRiskAssessment(
                symbol=str(snapshot["symbol"]).upper(),
                status="PENDING_DECISION" if should_run_llm else "SKIPPED_REFRESH_ONLY",
                allow_entry=True,
                risk_score=0.0,
                position_multiplier=1.0,
                veto_reason="" if should_run_llm else "刷新轮不调用大模型",
            )
            for snapshot in market_snapshots
        }
        if self.market_analyst is not None and should_run_llm:
            ai_risk_map = self.market_analyst.assess_entry_risk(
                quote_asset=self.settings.quote_asset,
                kline_interval=self.settings.kline_interval,
                market_snapshots=market_snapshots,
                news_evidence=news_evidence,
            )
            llm_analysis = self.market_analyst.analyze(
                quote_asset=self.settings.quote_asset,
                kline_interval=self.settings.kline_interval,
                market_snapshots=market_snapshots,
                news_evidence=news_evidence,
            )
        elif self.market_analyst is not None:
            llm_analysis = LlmAnalysis(
                status="SKIPPED_REFRESH_ONLY",
                provider="none",
                model="",
                regime_cn="刷新轮",
                summary_cn="当前无新K线或关键阈值事件，本轮不调用大模型。",
                action_bias_cn="观望",
                confidence=0.0,
                risk_note_cn="刷新轮仅更新行情和账本，避免模型端点阻塞实时刷新。",
            )

        for context in symbol_contexts:
            symbol = str(context["symbol"])
            price = float(context["price"])
            filters = context["filters"]
            base_balance = float(context["base_balance"])
            has_position = bool(context["has_position"])
            signal = context["signal"]
            exit_reason = context["exit_reason"]
            activation_decision = context["activation_decision"]
            scheduling = context["scheduling"]
            latest_closed_candle_close_time = int(context["latest_closed_candle_close_time"])
            ai_assessment = ai_risk_map.get(symbol.upper()) or AiRiskAssessment(
                symbol=symbol.upper(),
                status="FALLBACK",
                allow_entry=True,
                risk_score=0.0,
                position_multiplier=1.0,
                veto_reason="",
            )
            applied_ai_assessment = ai_assessment if signal.action == SignalAction.BUY and not has_position else None

            order = None
            execution_result: Dict[str, object] = {"status": "NO_ACTION"}
            buy_diagnostic = self.risk.inspect_buy_decision(
                symbol=symbol,
                price=price,
                account=account,
                filters=filters,
                signal_action=signal.action.value,
                signal_reason=signal.reason,
                has_position=has_position,
                ai_assessment=applied_ai_assessment,
            )
            sell_diagnostic = self.risk.inspect_sell_decision(
                symbol=symbol,
                price=price,
                position=context["position"],
                candles=context["candles"],
                current_timestamp_ms=cycle_timestamp_ms,
                signal=signal,
                exit_reason=exit_reason,
                activation_decision=activation_decision,
            )

            if not scheduling.should_run_decision:
                execution_result = {
                    "status": "SKIPPED_REFRESH_ONLY",
                    "reason": scheduling.decision_reason,
                }
                buy_diagnostic = self._mark_refresh_only_diagnostic(buy_diagnostic, scheduling.decision_reason)
            elif exit_reason is not None and has_position:
                decision = self.risk.build_sell_order(symbol, price, base_balance, filters)
                if decision.order is not None:
                    order = decision.order
                    execution_result = self.executor.execute(
                        order,
                        fill_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                    )
                    execution_result["trigger"] = exit_reason
                else:
                    execution_result = {"status": "BLOCKED", "reason": decision.reason, "trigger": exit_reason}
            elif signal.action == SignalAction.BUY and not has_position:
                if not ai_assessment.allow_entry:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": "ai_entry_veto",
                        "ai_veto_reason": ai_assessment.veto_reason,
                    }
                else:
                    decision = self.risk.build_buy_order(
                        symbol,
                        price,
                        account,
                        filters,
                        position_multiplier=ai_assessment.position_multiplier,
                    )
                    if decision.order is not None:
                        order = decision.order
                        execution_result = self.executor.execute(
                            order,
                            fill_price=price,
                            filters=filters,
                            timestamp_ms=cycle_timestamp_ms,
                            entry_candle_close_time_ms=latest_closed_candle_close_time,
                        )
                    else:
                        execution_result = {"status": "BLOCKED", "reason": decision.reason}
            elif signal.action == SignalAction.SELL and has_position:
                decision = self.risk.build_sell_order(symbol, price, base_balance, filters)
                if decision.order is not None:
                    order = decision.order
                    execution_result = self.executor.execute(
                        order,
                        fill_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                    )
                    execution_result["trigger"] = "strategy_sell"
                else:
                    execution_result = {"status": "BLOCKED", "reason": decision.reason}
            elif activation_decision.order is not None:
                order = activation_decision.order
                execution_result = self.executor.execute(
                    order,
                    fill_price=price,
                    filters=filters,
                    timestamp_ms=cycle_timestamp_ms,
                    entry_candle_close_time_ms=latest_closed_candle_close_time,
                )
                execution_result["trigger"] = activation_decision.trigger
                if execution_result.get("status") == "PAPER_FILLED":
                    self._record_position_activation_success(
                        symbol=symbol,
                        decision=activation_decision,
                        fill_price=price,
                        timestamp_ms=cycle_timestamp_ms,
                    )
                else:
                    self._record_position_activation_state(
                        symbol=symbol,
                        decision=activation_decision,
                        timestamp_ms=cycle_timestamp_ms,
                    )
            else:
                self._record_position_activation_state(
                    symbol=symbol,
                    decision=activation_decision,
                    timestamp_ms=cycle_timestamp_ms,
                )

            if scheduling.should_run_decision:
                self.scheduler.record_decision(
                    symbol=symbol,
                    latest_closed_candle_close_time=latest_closed_candle_close_time,
                    current_price=price,
                    timestamp_ms=cycle_timestamp_ms,
                )

            buy_diagnostics.append(buy_diagnostic)
            sell_diagnostics.append(sell_diagnostic)
            decisions.append(
                CycleDecision(
                    symbol=symbol,
                    signal=signal,
                    order=order,
                    execution_result=execution_result,
                )
            )

        self.scheduler.save()
        summary = self._build_portfolio_summary(account, mark_prices)
        cycle_mode, cycle_reason = self.scheduler.summarize_cycle(scheduling_diagnostics)
        decision_ledger = self._build_decision_ledger(
            timestamp_ms=cycle_timestamp_ms,
            cycle_mode=cycle_mode,
            decisions=decisions,
            buy_diagnostics=buy_diagnostics,
            sell_diagnostics=sell_diagnostics,
            ai_risk_assessments=[ai_risk_map[str(context["symbol"]).upper()] for context in symbol_contexts],
            total_equity=summary["total_equity"],
            news_refresh_status=news_result.refresh_status if news_result is not None else "DISABLED",
        )
        return CycleReport(
            timestamp_ms=cycle_timestamp_ms,
            decisions=decisions,
            buy_diagnostics=buy_diagnostics,
            sell_diagnostics=sell_diagnostics,
            position_diagnostics=position_diagnostics,
            scheduling_diagnostics=scheduling_diagnostics,
            decision_ledger=decision_ledger,
            ai_risk_assessments=[ai_risk_map[str(context["symbol"]).upper()] for context in symbol_contexts],
            market_prices=mark_prices,
            market_snapshots=market_snapshots,
            news_evidence=news_evidence,
            news_refresh_status=news_result.refresh_status if news_result is not None else "DISABLED",
            news_last_updated_ms=news_result.last_updated_ms if news_result is not None else 0,
            news_next_refresh_ms=news_result.next_refresh_ms if news_result is not None else 0,
            cycle_mode=cycle_mode,
            cycle_reason=cycle_reason,
            quote_asset_balance=summary["quote_balance"],
            simulation_mode=self.settings.dry_run,
            total_equity=summary["total_equity"],
            realized_pnl=summary["realized_pnl"],
            unrealized_pnl=summary["unrealized_pnl"],
            net_pnl=summary["net_pnl"],
            llm_analysis=llm_analysis,
        )

    def _evaluate_position_activation(
        self,
        *,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        filters,
        timestamp_ms: int,
    ) -> PositionActivationDecision:
        if not self.settings.dry_run or self.paper_portfolio is None:
            return PositionActivationDecision("HOLD", "", "position_activation_requires_paper_mode")
        snapshot = self.paper_portfolio.load_snapshot()
        return self.position_activation.evaluate(
            symbol=symbol,
            price=price,
            account=account,
            filters=filters,
            snapshot=snapshot,
            timestamp_ms=timestamp_ms,
        )

    def _record_position_activation_success(
        self,
        *,
        symbol: str,
        decision: PositionActivationDecision,
        fill_price: float,
        timestamp_ms: int,
    ) -> None:
        if self.paper_portfolio is None:
            return
        snapshot = self.paper_portfolio.load_snapshot()
        updated = self.position_activation.apply_success(
            snapshot=snapshot,
            symbol=symbol,
            decision=decision,
            fill_price=fill_price,
            timestamp_ms=timestamp_ms,
        )
        self.paper_portfolio.save_snapshot(updated)

    def _record_position_activation_state(
        self,
        *,
        symbol: str,
        decision: PositionActivationDecision,
        timestamp_ms: int,
    ) -> None:
        if self.paper_portfolio is None:
            return
        snapshot = self.paper_portfolio.load_snapshot()
        updated = self.position_activation.apply_state_update(
            snapshot=snapshot,
            symbol=symbol,
            decision=decision,
            timestamp_ms=timestamp_ms,
        )
        if updated != snapshot:
            self.paper_portfolio.save_snapshot(updated)

    @staticmethod
    def _build_decision_ledger(
        *,
        timestamp_ms: int,
        cycle_mode: str,
        decisions: List[CycleDecision],
        buy_diagnostics: List[BuyDecisionDiagnostic],
        sell_diagnostics: List[SellDecisionDiagnostic],
        ai_risk_assessments: List[AiRiskAssessment],
        total_equity: float,
        news_refresh_status: str,
    ) -> List[DecisionLedgerEntry]:
        ledger: List[DecisionLedgerEntry] = []
        for index, decision in enumerate(decisions):
            buy = buy_diagnostics[index]
            sell = sell_diagnostics[index]
            ai = ai_risk_assessments[index]
            execution = decision.execution_result
            execution_status = str(execution.get("status", ""))
            execution_reason = str(execution.get("reason") or execution.get("trigger") or "")
            final_action = "HOLD"
            if execution_status == "PAPER_FILLED":
                final_action = str(execution.get("side", decision.order.side if decision.order else ""))
            elif execution_status == "BLOCKED":
                final_action = "BLOCKED"
            elif execution_status == "SKIPPED_REFRESH_ONLY":
                final_action = "REFRESH_ONLY"
            ledger.append(
                DecisionLedgerEntry(
                    timestamp_ms=timestamp_ms,
                    cycle_mode=cycle_mode,
                    symbol=decision.symbol,
                    price=sell.mark_price or buy.price,
                    has_position=sell.has_position,
                    position_quantity=sell.quantity,
                    average_entry_price=sell.average_entry_price,
                    unrealized_pnl=sell.unrealized_pnl,
                    total_equity=total_equity,
                    buy_signal=buy.signal_action,
                    buy_blocker=buy.blocker,
                    sell_signal=sell.strategy_signal,
                    sell_blocker=sell.blocker,
                    ai_allow_entry=ai.allow_entry,
                    ai_risk_score=ai.risk_score,
                    final_action=final_action,
                    execution_status=execution_status,
                    execution_reason=execution_reason,
                    news_refresh_status=news_refresh_status,
                )
            )
        return ledger

    @staticmethod
    def _latest_closed_candle_close_time(candles, current_timestamp_ms: int) -> int:
        for candle in reversed(candles):
            if candle.close_time <= current_timestamp_ms:
                return candle.close_time
        return candles[-1].close_time

    def _load_account_snapshot(self) -> AccountSnapshot:
        if self.settings.dry_run and self.paper_portfolio is not None:
            return self.paper_portfolio.account_snapshot()
        if self.settings.api_key and self.settings.api_secret:
            return self.client.get_account_snapshot()
        return AccountSnapshot(balances={self.settings.quote_asset: self.settings.paper_quote_balance})

    def _build_portfolio_summary(
        self,
        account: AccountSnapshot,
        mark_prices: Dict[str, float],
    ) -> Dict[str, float]:
        if self.settings.dry_run and self.paper_portfolio is not None:
            return self.paper_portfolio.equity_summary(mark_prices)

        total_equity = account.balance_of(self.settings.quote_asset)
        for symbol, price in mark_prices.items():
            base_asset = self.risk.base_asset_for_symbol(symbol)
            if base_asset:
                total_equity += account.balance_of(base_asset) * price
        return {
            "quote_balance": account.balance_of(self.settings.quote_asset),
            "total_equity": total_equity,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "net_pnl": 0.0,
        }

    @staticmethod
    def _mark_refresh_only_diagnostic(
        diagnostic: BuyDecisionDiagnostic,
        refresh_reason: str,
    ) -> BuyDecisionDiagnostic:
        blocker_details = list(diagnostic.blocker_details)
        blocker_details.insert(0, f"当前为刷新轮：{refresh_reason}")
        return replace(
            diagnostic,
            eligible_to_buy=False,
            blocker="当前为刷新轮，未进入交易决策",
            blocker_details=blocker_details,
        )
