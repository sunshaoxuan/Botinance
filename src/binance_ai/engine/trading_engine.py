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
from binance_ai.models import AccountSnapshot, AiRiskAssessment, BuyDecisionDiagnostic, CycleDecision, CycleReport, DecisionLedgerEntry, LlmAnalysis, OrderLifecycleEvent, OrderRequest, PositionDiagnostic, SchedulingDiagnostic, SellDecisionDiagnostic, SignalAction
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
        order_lifecycle_events: List[OrderLifecycleEvent] = []
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
                    "1m",
                ],
                limit=self.settings.kline_limit,
            )
            candles = candles_by_interval[self.settings.kline_interval]
            execution_candles = candles_by_interval.get("1m", candles)
            price = candles[-1].close
            mark_prices[symbol] = price
            lifecycle_results, lifecycle_events = self.executor.process_open_orders(
                symbol=symbol,
                candles=execution_candles,
                current_price=price,
                timestamp_ms=cycle_timestamp_ms,
            )
            order_lifecycle_events.extend(lifecycle_events)
            for result in lifecycle_results:
                activation_success = self._activation_success_from_fill(symbol=symbol, result=result)
                if activation_success is not None:
                    self._record_position_activation_success(
                        symbol=symbol,
                        decision=activation_success,
                        fill_price=float(result.get("fill_price", price)),
                        timestamp_ms=cycle_timestamp_ms,
                    )
            if lifecycle_results:
                account = self._load_account_snapshot()
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
            release_exit_block_reason = self._release_exit_buyback_block_reason(
                symbol=symbol,
                exit_reason=exit_reason,
                activation_decision=activation_decision,
            )
            if release_exit_block_reason:
                exit_reason = None
            strategy_sell_block_reason = self._strategy_sell_buyback_block_reason(
                symbol=symbol,
                signal=signal,
                has_position=has_position,
                exit_reason=exit_reason,
                activation_decision=activation_decision,
            )
            if strategy_sell_block_reason or release_exit_block_reason:
                signal = replace(
                    signal,
                    action=SignalAction.HOLD,
                    confidence=min(signal.confidence, 0.5),
                    reason=strategy_sell_block_reason or release_exit_block_reason,
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
                    "open_orders": self.executor.open_orders_for_symbol(symbol),
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
            open_orders = list(context.get("open_orders", []))

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
            elif open_orders:
                cancel_reason = self._open_order_cancel_reason(open_orders[0], signal, ai_assessment)
                if cancel_reason:
                    events = self.executor.cancel_open_orders_for_symbol(
                        symbol=symbol,
                        reason=cancel_reason,
                        timestamp_ms=cycle_timestamp_ms,
                    )
                    order_lifecycle_events.extend(events)
                    execution_result = {"status": "CANCELED", "reason": cancel_reason}
                else:
                    execution_result = {
                        "status": "ORDER_OPEN",
                        "reason": "existing_open_order",
                        "client_order_id": open_orders[0].client_order_id,
                        "limit_price": open_orders[0].limit_price,
                        "side": open_orders[0].side,
                        "trigger": open_orders[0].trigger,
                    }
            elif exit_reason is not None and has_position:
                decision = self.risk.build_sell_order(
                    symbol,
                    price,
                    base_balance,
                    filters,
                    sell_fraction=self.risk.exit_sell_fraction(exit_reason),
                )
                if decision.order is not None:
                    order = self._as_limit_order(
                        decision.order,
                        price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        trigger=exit_reason,
                        urgent=True,
                    )
                    execution_result, event = self.executor.submit_limit_order(
                        order,
                        current_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                    )
                    if event is not None:
                        order_lifecycle_events.append(event)
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
                        order = self._as_limit_order(
                            decision.order,
                            price=price,
                            filters=filters,
                            timestamp_ms=cycle_timestamp_ms,
                            trigger="strategy_buy",
                            urgent=False,
                        )
                        execution_result, event = self.executor.submit_limit_order(
                            order,
                            current_price=price,
                            filters=filters,
                            timestamp_ms=cycle_timestamp_ms,
                            entry_candle_close_time_ms=latest_closed_candle_close_time,
                        )
                        if event is not None:
                            order_lifecycle_events.append(event)
                    else:
                        execution_result = {"status": "BLOCKED", "reason": decision.reason}
            elif signal.action == SignalAction.SELL and has_position:
                decision = self.risk.build_sell_order(
                    symbol,
                    price,
                    base_balance,
                    filters,
                    sell_fraction=self.risk.exit_sell_fraction(None, strategy_sell=True),
                )
                if decision.order is not None:
                    order = self._as_limit_order(
                        decision.order,
                        price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        trigger="strategy_sell",
                        urgent=False,
                    )
                    execution_result, event = self.executor.submit_limit_order(
                        order,
                        current_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                    )
                    if event is not None:
                        order_lifecycle_events.append(event)
                else:
                    execution_result = {"status": "BLOCKED", "reason": decision.reason}
            elif activation_decision.order is not None:
                order = self._as_limit_order(
                    activation_decision.order,
                    price=price,
                    filters=filters,
                    timestamp_ms=cycle_timestamp_ms,
                    trigger=activation_decision.trigger,
                    urgent=False,
                )
                execution_result, event = self.executor.submit_limit_order(
                    order,
                    current_price=price,
                    filters=filters,
                    timestamp_ms=cycle_timestamp_ms,
                    entry_candle_close_time_ms=latest_closed_candle_close_time,
                )
                if event is not None:
                    order_lifecycle_events.append(event)
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
            order_lifecycle_events=order_lifecycle_events,
            open_orders=self.executor.all_open_orders(),
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

    def _as_limit_order(
        self,
        order: OrderRequest,
        *,
        price: float,
        filters,
        timestamp_ms: int,
        trigger: str,
        urgent: bool,
    ) -> OrderRequest:
        if self.settings.order_execution_mode != "limit_lifecycle":
            return order
        side = order.side.upper()
        bid = price
        ask = price
        try:
            ticker = self.client.get_order_book_ticker(order.symbol)
            bid = float(ticker.get("bid_price") or price)
            ask = float(ticker.get("ask_price") or price)
        except Exception:  # noqa: BLE001 - price fallback keeps paper mode and tests deterministic.
            pass

        if side == "BUY":
            raw_limit = bid * (1.0 - self.settings.order_passive_offset_pct)
        elif urgent:
            raw_limit = bid * (1.0 - self.settings.order_urgent_cross_pct)
        else:
            raw_limit = ask * (1.0 + self.settings.order_passive_offset_pct)

        quantize_price = getattr(self.client, "quantize_price", None)
        limit_price = (
            quantize_price(raw_limit, getattr(filters, "tick_size", 0.0))
            if callable(quantize_price)
            else raw_limit
        )
        client_order_id = f"boti_{order.symbol}_{side.lower()}_{timestamp_ms}"
        return OrderRequest(
            symbol=order.symbol,
            side=order.side,
            order_type="LIMIT",
            quantity=order.quantity,
            limit_price=limit_price,
            time_in_force=self.settings.order_time_in_force,
            client_order_id=client_order_id,
            trigger=trigger,
            expires_at_ms=timestamp_ms + self.settings.order_ttl_seconds * 1000,
        )

    @staticmethod
    def _open_order_cancel_reason(open_order, signal, ai_assessment: AiRiskAssessment) -> str:
        side = str(open_order.side).upper()
        if side == "BUY" and not ai_assessment.allow_entry:
            return "ai_risk_worsened_cancel_open_buy"
        if side == "BUY" and signal.action == SignalAction.SELL:
            return "signal_reversed_cancel_open_buy"
        if side == "SELL" and signal.action == SignalAction.BUY:
            return "signal_reversed_cancel_open_sell"
        return ""

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

    def _strategy_sell_buyback_block_reason(
        self,
        *,
        symbol: str,
        signal,
        has_position: bool,
        exit_reason: str | None,
        activation_decision: PositionActivationDecision,
    ) -> str:
        if not has_position or signal.action != SignalAction.SELL or exit_reason:
            return ""
        if activation_decision.trigger == "grid_buyback" and activation_decision.order is not None:
            return "已有释放仓位到达回补线，本轮优先回补买入，暂停策略卖出"
        if self.paper_portfolio is None:
            return ""
        state = self.paper_portfolio.load_snapshot().activation_state.get(symbol, {})
        pending_qty = 0.0
        if isinstance(state, dict):
            try:
                pending_qty = float(state.get("pending_buyback_quantity", 0.0))
            except (TypeError, ValueError):
                pending_qty = 0.0
        if pending_qty <= 0:
            return ""
        detail = activation_decision.reason or "等待回补"
        return f"已有 {pending_qty:.8f} 待回补仓位，暂停继续策略释放卖出；{detail}"

    def _release_exit_buyback_block_reason(
        self,
        *,
        symbol: str,
        exit_reason: str | None,
        activation_decision: PositionActivationDecision,
    ) -> str:
        if exit_reason not in {"take_profit", "trailing_stop", "max_hold_exit"}:
            return ""
        if activation_decision.trigger == "grid_buyback" and activation_decision.order is not None:
            return "已有释放仓位到达回补线，本轮优先回补买入，暂停继续部分退出"
        if self.paper_portfolio is None:
            return ""
        state = self.paper_portfolio.load_snapshot().activation_state.get(symbol, {})
        pending_qty = 0.0
        if isinstance(state, dict):
            try:
                pending_qty = float(state.get("pending_buyback_quantity", 0.0))
            except (TypeError, ValueError):
                pending_qty = 0.0
        if pending_qty <= 0:
            return ""
        detail = activation_decision.reason or "等待回补"
        return f"已有 {pending_qty:.8f} 待回补仓位，暂停继续部分退出 {exit_reason}；{detail}"

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

    def _activation_success_from_fill(
        self,
        *,
        symbol: str,
        result: Dict[str, object],
    ) -> PositionActivationDecision | None:
        if result.get("status") != "PAPER_FILLED":
            return None
        trigger = str(result.get("trigger", ""))
        side = str(result.get("side", "")).upper()
        quantity = float(result.get("quantity", 0.0))
        if trigger == "grid_buyback" and side == "BUY":
            return PositionActivationDecision(
                action="BUY",
                trigger=trigger,
                reason="回补买入成交后更新仓位激活状态",
                quantity=quantity,
            )
        if side != "SELL":
            return None

        release_trigger = self._release_trigger_for_sell_fill(symbol=symbol, trigger=trigger)
        if not release_trigger:
            return None
        return PositionActivationDecision(
            action="SELL",
            trigger=release_trigger,
            reason=self._release_reason_for_trigger(release_trigger),
            quantity=quantity,
        )

    def _release_trigger_for_sell_fill(self, *, symbol: str, trigger: str) -> str:
        trigger_map = {
            "strategy_sell": "strategy_release_sell",
            "take_profit": "take_profit_release_sell",
            "trailing_stop": "trailing_stop_release_sell",
            "max_hold_exit": "max_hold_release_sell",
            "grid_profit_sell": "grid_profit_sell",
            "grid_loss_recovery_sell": "grid_loss_recovery_sell",
        }
        release_trigger = trigger_map.get(trigger, "")
        if not release_trigger or self.paper_portfolio is None:
            return ""
        remaining_position = self.paper_portfolio.position_snapshot(symbol)
        if remaining_position is None or remaining_position.quantity <= 0:
            return ""
        return release_trigger

    @staticmethod
    def _release_reason_for_trigger(trigger: str) -> str:
        labels = {
            "strategy_release_sell": "策略卖出已登记待回补",
            "take_profit_release_sell": "止盈部分卖出已登记待回补",
            "trailing_stop_release_sell": "跟踪止损部分卖出已登记待回补",
            "max_hold_release_sell": "超时退出部分卖出已登记待回补",
            "grid_profit_sell": "网格卖出成交后登记待回补",
            "grid_loss_recovery_sell": "亏损修复卖出成交后登记待回补",
        }
        return labels.get(trigger, "释放仓位已登记待回补")

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
            elif execution_status == "ORDER_OPEN":
                final_action = f"OPEN_{execution.get('side', decision.order.side if decision.order else '')}"
            elif execution_status == "REJECTED":
                final_action = "REJECTED"
            elif execution_status == "UNKNOWN":
                final_action = "UNKNOWN"
            elif execution_status == "CANCELED":
                final_action = "CANCELED"
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
