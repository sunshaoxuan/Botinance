from __future__ import annotations

from dataclasses import replace
import time
from typing import Dict, List, Tuple

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
from binance_ai.trade_guard import TradeProfitabilityGuard


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
        self.profitability_guard = TradeProfitabilityGuard(settings)

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
            min_position_notional = max(filters.min_notional, self.settings.min_order_notional)
            has_position = base_balance >= filters.min_qty and base_balance * price >= min_position_notional
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
            if exit_reason == "stop_loss" and position is not None and self.risk.is_emergency_stop(
                price=price,
                position=position,
                candles=candles,
            ):
                exit_reason = "emergency_stop"
            activation_decision = self._evaluate_position_activation(
                symbol=symbol,
                price=price,
                account=account,
                filters=filters,
                timestamp_ms=cycle_timestamp_ms,
            )
            cooldown_remaining_bars = self._buyback_cooldown_remaining_bars(
                symbol=symbol,
                timestamp_ms=cycle_timestamp_ms,
            )
            if cooldown_remaining_bars > 0:
                if exit_reason == "stop_loss" and position is not None and self.settings.buyback_cooldown_allow_emergency_stop and self.risk.is_emergency_stop(
                    price=price,
                    position=position,
                    candles=candles,
                ):
                    exit_reason = "emergency_stop"
                elif exit_reason in {"stop_loss", "trailing_stop", "take_profit", "max_hold_exit"}:
                    exit_reason = None
                    signal = replace(
                        signal,
                        action=SignalAction.HOLD,
                        confidence=min(signal.confidence, 0.5),
                        reason=f"回补冷却保护剩余 {cooldown_remaining_bars} 根K线，暂停普通退出",
                    )
                if signal.action == SignalAction.SELL:
                    signal = replace(
                        signal,
                        action=SignalAction.HOLD,
                        confidence=min(signal.confidence, 0.5),
                        reason=f"回补冷却保护剩余 {cooldown_remaining_bars} 根K线，暂停策略卖出",
                    )
                if activation_decision.trigger == "grid_loss_recovery_sell":
                    activation_decision = PositionActivationDecision(
                        "HOLD",
                        "buyback_cooldown_blocks_loss_recovery",
                        f"回补冷却保护剩余 {cooldown_remaining_bars} 根K线，暂停亏损修复卖出",
                        state_update=activation_decision.state_update,
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
            applied_ai_assessment = ai_assessment if signal.action == SignalAction.BUY else None
            open_orders = list(context.get("open_orders", []))
            cooldown_remaining_bars = self._buyback_cooldown_remaining_bars(
                symbol=symbol,
                timestamp_ms=cycle_timestamp_ms,
            )

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
                position_value=base_balance * price,
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

            open_order_summary, open_order_events = self._manage_open_orders(
                symbol=symbol,
                open_orders=open_orders,
                price=price,
                timestamp_ms=cycle_timestamp_ms,
                signal_action=signal.action.value,
                ai_assessment=ai_assessment,
                cooldown_remaining_bars=cooldown_remaining_bars,
            )
            order_lifecycle_events.extend(open_order_events)
            if open_order_summary.get("status") != "NO_OPEN_ORDERS":
                execution_result = open_order_summary

            if open_order_summary.get("status") == "CANCELED":
                pass
            elif not scheduling.should_run_decision and open_order_summary.get("status") == "NO_OPEN_ORDERS":
                execution_result = {
                    "status": "SKIPPED_REFRESH_ONLY",
                    "reason": scheduling.decision_reason,
                }
                buy_diagnostic = self._mark_refresh_only_diagnostic(buy_diagnostic, scheduling.decision_reason)
            elif exit_reason is not None and has_position:
                decision = self.risk.build_sell_order(
                    symbol,
                    price,
                    base_balance,
                    filters,
                    sell_fraction=self.risk.exit_sell_fraction(exit_reason),
                )
                if decision.order is not None:
                    execution_result, events, orders = self._submit_ladder_orders(
                        decision.order,
                        current_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                        trigger=exit_reason,
                        urgent=True,
                        ladder_group="risk_exit",
                        tiers_raw="",
                    )
                    order_lifecycle_events.extend(events)
                    order = orders[0] if orders else None
                else:
                    execution_result = {"status": "BLOCKED", "reason": decision.reason, "trigger": exit_reason}
            elif signal.action == SignalAction.BUY and (buy_diagnostic.eligible_to_buy or not ai_assessment.allow_entry):
                if not ai_assessment.allow_entry:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": "ai_entry_veto",
                        "ai_veto_reason": ai_assessment.veto_reason,
                    }
                else:
                    target_order, target_blocker = self._build_target_position_buy_order(
                        symbol=symbol,
                        price=price,
                        account=account,
                        base_balance=base_balance,
                        filters=filters,
                        position_multiplier=ai_assessment.position_multiplier,
                    )
                    decision = self.risk.build_buy_order(
                        symbol,
                        price,
                        account,
                        filters,
                        position_multiplier=ai_assessment.position_multiplier,
                    )
                    target_budget_enabled = self.settings.order_ladder_enabled and self.settings.target_position_fraction > 0
                    decision_order = target_order if target_budget_enabled else decision.order
                    if decision_order is not None:
                        execution_result, events, orders = self._submit_ladder_orders(
                            decision_order,
                            current_price=price,
                            filters=filters,
                            timestamp_ms=cycle_timestamp_ms,
                            entry_candle_close_time_ms=latest_closed_candle_close_time,
                            trigger="strategy_buy",
                            urgent=False,
                            ladder_group="entry",
                            tiers_raw=self.settings.entry_ladder_tiers,
                        )
                        order_lifecycle_events.extend(events)
                        order = orders[0] if orders else None
                        if target_order is not None:
                            execution_result["target_position_fraction"] = self.settings.target_position_fraction
                            execution_result["min_cash_reserve_fraction"] = self.settings.min_cash_reserve_fraction
                    else:
                        execution_result = {"status": "BLOCKED", "reason": target_blocker if target_budget_enabled else decision.reason}
            elif signal.action == SignalAction.BUY:
                execution_result = {
                    "status": "BLOCKED",
                    "reason": buy_diagnostic.blocker,
                    "decision_state": self._decision_state_for_symbol(symbol),
                    "cooldown_remaining_bars": cooldown_remaining_bars,
                }
            elif signal.action == SignalAction.SELL and has_position:
                decision = self.risk.build_sell_order(
                    symbol,
                    price,
                    base_balance,
                    filters,
                    sell_fraction=self.risk.exit_sell_fraction(None, strategy_sell=True),
                )
                if decision.order is not None:
                    guard = self.profitability_guard.inspect_release(
                        price,
                        price * (1.0 - self.settings.grid_buyback_step_pct),
                    )
                    if not guard.allowed:
                        execution_result = {
                            "status": "BLOCKED",
                            "reason": "net_edge_too_small",
                            "trigger": "strategy_sell",
                            "guard_result": guard.reason,
                            "net_edge_pct": guard.net_edge_pct,
                            "required_edge_pct": guard.required_edge_pct,
                            "decision_state": self._decision_state_for_symbol(symbol),
                            "cooldown_remaining_bars": cooldown_remaining_bars,
                        }
                    else:
                        execution_result, events, orders = self._submit_ladder_orders(
                            decision.order,
                            current_price=price,
                            filters=filters,
                            timestamp_ms=cycle_timestamp_ms,
                            entry_candle_close_time_ms=latest_closed_candle_close_time,
                            trigger="strategy_sell",
                            urgent=False,
                            ladder_group="exit",
                            tiers_raw=self.settings.exit_ladder_tiers,
                        )
                        order_lifecycle_events.extend(events)
                        order = orders[0] if orders else None
                        execution_result["guard_result"] = guard.reason
                        execution_result["net_edge_pct"] = guard.net_edge_pct
                        execution_result["required_edge_pct"] = guard.required_edge_pct
                else:
                    execution_result = {"status": "BLOCKED", "reason": decision.reason}
            elif activation_decision.order is not None:
                tiers_raw = self.settings.grid_buyback_tiers if activation_decision.trigger == "grid_buyback" else ""
                ladder_group = "buyback" if activation_decision.trigger == "grid_buyback" else "activation"
                execution_result, events, orders = self._submit_ladder_orders(
                    activation_decision.order,
                    current_price=price,
                    filters=filters,
                    timestamp_ms=cycle_timestamp_ms,
                    entry_candle_close_time_ms=latest_closed_candle_close_time,
                    trigger=activation_decision.trigger,
                    urgent=False,
                    ladder_group=ladder_group,
                    tiers_raw=tiers_raw,
                )
                order_lifecycle_events.extend(events)
                order = orders[0] if orders else None
                execution_result["decision_state"] = self._decision_state_for_symbol(symbol)
                execution_result["cooldown_remaining_bars"] = cooldown_remaining_bars
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
                execution_result.setdefault("decision_state", self._decision_state_for_symbol(symbol))
                execution_result.setdefault("cooldown_remaining_bars", cooldown_remaining_bars)

            if scheduling.should_run_decision:
                self.scheduler.record_decision(
                    symbol=symbol,
                    latest_closed_candle_close_time=latest_closed_candle_close_time,
                    current_price=price,
                    timestamp_ms=cycle_timestamp_ms,
                )

            execution_result.setdefault("decision_state", self._decision_state_for_symbol(symbol))
            execution_result.setdefault("cooldown_remaining_bars", cooldown_remaining_bars)
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
        tier_index: int = 0,
        ladder_group: str = "",
        target_fraction: float = 0.0,
        limit_offset_pct: float | None = None,
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

        offset = self.settings.order_passive_offset_pct if limit_offset_pct is None else max(0.0, limit_offset_pct)
        if side == "BUY":
            raw_limit = bid * (1.0 - offset)
        elif urgent:
            raw_limit = bid * (1.0 - self.settings.order_urgent_cross_pct)
        else:
            raw_limit = ask * (1.0 + offset)

        quantize_price = getattr(self.client, "quantize_price", None)
        limit_price = (
            quantize_price(raw_limit, getattr(filters, "tick_size", 0.0))
            if callable(quantize_price)
            else raw_limit
        )
        group_part = ladder_group or trigger or "order"
        client_order_id = f"boti_{order.symbol}_{side.lower()}_{group_part}_{tier_index}_{timestamp_ms}"
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
            tier_index=tier_index,
            ladder_group=ladder_group,
            target_fraction=target_fraction,
        )

    def _build_target_position_buy_order(
        self,
        *,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        base_balance: float,
        filters,
        position_multiplier: float,
    ) -> Tuple[OrderRequest | None, str]:
        if not self.settings.order_ladder_enabled:
            return None, ""
        target_fraction = min(1.0, max(0.0, self.settings.target_position_fraction))
        cash_reserve_fraction = min(1.0, max(0.0, self.settings.min_cash_reserve_fraction))
        multiplier = min(1.0, max(0.0, position_multiplier))
        if target_fraction <= 0 or multiplier <= 0 or price <= 0:
            return None, "target_position_disabled"

        quote_balance = account.balance_of(self.settings.quote_asset)
        position_value = max(0.0, base_balance * price)
        total_equity = max(0.0, quote_balance + position_value)
        target_notional = total_equity * target_fraction * multiplier
        max_spend = max(0.0, quote_balance - total_equity * cash_reserve_fraction)
        spend = min(max_spend, max(0.0, target_notional - position_value))
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        quantity = self.client.quantize_quantity(spend / price, filters.step_size)
        final_notional = quantity * price
        if spend <= 0:
            return None, "target_position_reached_or_cash_reserved"
        if quantity <= 0 or quantity < filters.min_qty:
            return None, f"target_quantity_below_min_qty:{quantity}"
        if final_notional < min_notional:
            return None, f"target_notional_below_min_notional:{final_notional:.2f}"
        return OrderRequest(symbol=symbol, side="BUY", order_type="MARKET", quantity=quantity), ""

    def _submit_ladder_orders(
        self,
        order: OrderRequest,
        *,
        current_price: float,
        filters,
        timestamp_ms: int,
        entry_candle_close_time_ms: int,
        trigger: str,
        urgent: bool,
        ladder_group: str,
        tiers_raw: str,
    ) -> Tuple[Dict[str, object], List[OrderLifecycleEvent], List[OrderRequest]]:
        tiers = self._ladder_tiers(tiers_raw)
        if urgent or not self.settings.order_ladder_enabled or not tiers:
            tiers = [(0.0, 1.0)]

        results: List[Dict[str, object]] = []
        events: List[OrderLifecycleEvent] = []
        orders: List[OrderRequest] = []
        min_notional = max(filters.min_notional, self.settings.min_order_notional)
        for index, (offset_pct, fraction) in enumerate(tiers):
            quantity = order.quantity if len(tiers) == 1 else self.client.quantize_quantity(order.quantity * fraction, filters.step_size)
            if quantity <= 0 or quantity < filters.min_qty or quantity * current_price < min_notional:
                continue
            tier_order = self._as_limit_order(
                OrderRequest(
                    symbol=order.symbol,
                    side=order.side,
                    order_type=order.order_type,
                    quantity=quantity,
                ),
                price=current_price,
                filters=filters,
                timestamp_ms=timestamp_ms,
                trigger=trigger,
                urgent=urgent,
                tier_index=index,
                ladder_group=ladder_group,
                target_fraction=fraction,
                limit_offset_pct=offset_pct if len(tiers) > 1 else None,
            )
            result, event = self.executor.submit_limit_order(
                tier_order,
                current_price=current_price,
                filters=filters,
                timestamp_ms=timestamp_ms,
                entry_candle_close_time_ms=entry_candle_close_time_ms,
            )
            results.append(result)
            orders.append(tier_order)
            if event is not None:
                events.append(event)

        accepted = [item for item in results if str(item.get("status")) in {"ORDER_OPEN", "UNKNOWN"}]
        rejected = [item for item in results if str(item.get("status")) == "REJECTED"]
        if not results:
            return {
                "status": "BLOCKED",
                "reason": "ladder_orders_below_minimums",
                "trigger": trigger,
                "ladder_group": ladder_group,
            }, events, orders
        if len(accepted) == 1 and not rejected:
            result = dict(accepted[0])
            result["ladder_group"] = ladder_group
            result["tier_index"] = orders[0].tier_index if orders else 0
            return result, events, orders
        return {
            "status": "ORDER_LADDER_OPEN" if accepted else "REJECTED",
            "reason": "order_ladder_submitted" if accepted else "order_ladder_rejected",
            "trigger": trigger,
            "ladder_group": ladder_group,
            "submitted_count": len(accepted),
            "rejected_count": len(rejected),
            "orders": results,
        }, events, orders

    @staticmethod
    def _ladder_tiers(raw: str) -> List[Tuple[float, float]]:
        tiers: List[Tuple[float, float]] = []
        for item in str(raw or "").split(","):
            item = item.strip()
            if not item or ":" not in item:
                continue
            offset_raw, fraction_raw = item.split(":", 1)
            try:
                offset = max(0.0, float(offset_raw.strip()))
                fraction = max(0.0, float(fraction_raw.strip()))
            except ValueError:
                continue
            if fraction > 0:
                tiers.append((offset, fraction))
        return tiers

    def _manage_open_orders(
        self,
        *,
        symbol: str,
        open_orders: List[object],
        price: float,
        timestamp_ms: int,
        signal_action: str,
        ai_assessment: AiRiskAssessment,
        cooldown_remaining_bars: int,
    ) -> Tuple[Dict[str, object], List[OrderLifecycleEvent]]:
        if not open_orders:
            return {"status": "NO_OPEN_ORDERS"}, []

        events: List[OrderLifecycleEvent] = []
        actions: List[Dict[str, object]] = []
        for open_order in open_orders:
            ai_allow_open_order = ai_assessment.allow_entry
            if str(getattr(open_order, "trigger", "")) == "grid_buyback" and not self.settings.ai_can_cancel_buyback:
                ai_allow_open_order = not self._ai_extreme_risk(ai_assessment)
            open_order_action = self.executor.classify_open_order_action(
                open_order,
                current_price=price,
                timestamp_ms=timestamp_ms,
                signal_action=signal_action,
                ai_allow_entry=ai_allow_open_order,
            )
            action = str(open_order_action.get("action", "KEEP"))
            reason = str(open_order_action.get("reason", "open_order_waiting_for_touch"))
            actions.append(
                {
                    "client_order_id": getattr(open_order, "client_order_id", ""),
                    "side": getattr(open_order, "side", ""),
                    "trigger": getattr(open_order, "trigger", ""),
                    "tier_index": getattr(open_order, "tier_index", 0),
                    "ladder_group": getattr(open_order, "ladder_group", ""),
                    "limit_price": getattr(open_order, "limit_price", 0.0),
                    "action": action,
                    "reason": reason,
                    "is_stale": bool(open_order_action.get("is_stale", False)),
                }
            )
            if action in {"CANCEL", "REPRICE"}:
                event = self.executor.cancel_open_order(
                    client_order_id=getattr(open_order, "client_order_id", ""),
                    reason=reason,
                    timestamp_ms=timestamp_ms,
                )
                if event is not None:
                    events.append(event)

        canceled_count = len(events)
        kept = [item for item in actions if item["action"] not in {"CANCEL", "REPRICE"}]
        if canceled_count > 0:
            canceled_actions = [item for item in actions if item["action"] in {"CANCEL", "REPRICE"}]
            return {
                "status": "CANCELED",
                "reason": str((canceled_actions[0] if canceled_actions else actions[0]).get("reason") or "open_order_canceled"),
                "open_order_action": "CANCEL",
                "canceled_count": canceled_count,
                "kept_count": len(kept),
                "open_order_actions": actions,
                "decision_state": self._decision_state_for_symbol(symbol),
                "cooldown_remaining_bars": cooldown_remaining_bars,
            }, events

        nearest = min(open_orders, key=lambda item: abs(float(getattr(item, "limit_price", 0.0) or 0.0) - price))
        return {
            "status": "ORDER_OPEN",
            "reason": "open_order_group_waiting_for_touch",
            "open_order_action": "KEEP",
            "open_order_count": len(open_orders),
            "open_order_actions": actions,
            "client_order_id": getattr(nearest, "client_order_id", ""),
            "limit_price": getattr(nearest, "limit_price", 0.0),
            "side": getattr(nearest, "side", ""),
            "trigger": getattr(nearest, "trigger", ""),
            "decision_state": self._decision_state_for_symbol(symbol),
            "cooldown_remaining_bars": cooldown_remaining_bars,
        }, events

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

    def _activation_state_for_symbol(self, symbol: str) -> Dict[str, object]:
        if self.paper_portfolio is None:
            return {}
        raw = self.paper_portfolio.load_snapshot().activation_state.get(symbol, {})
        return raw if isinstance(raw, dict) else {}

    def _decision_state_for_symbol(self, symbol: str) -> str:
        state = self._activation_state_for_symbol(symbol)
        decision_state = str(state.get("decision_state", "NORMAL"))
        if self._buyback_cooldown_remaining_bars(symbol=symbol, timestamp_ms=int(time.time() * 1000)) > 0:
            return "BUYBACK_COOLDOWN"
        if float(state.get("pending_buyback_quantity", 0.0) or 0.0) > 0:
            return decision_state if decision_state else "RELEASED_WAIT_BUYBACK"
        return decision_state or "NORMAL"

    def _buyback_cooldown_remaining_bars(self, *, symbol: str, timestamp_ms: int) -> int:
        state = self._activation_state_for_symbol(symbol)
        cooldown_until = int(float(state.get("buyback_cooldown_until_candle", 0) or 0))
        if cooldown_until <= timestamp_ms:
            return 0
        interval_ms = max(1, self._settings_interval_ms())
        return int((cooldown_until - timestamp_ms + interval_ms - 1) // interval_ms)

    def _settings_interval_ms(self) -> int:
        raw = str(self.settings.kline_interval).strip().lower()
        try:
            value = int(raw[:-1])
        except (TypeError, ValueError):
            return 60 * 60 * 1000
        if raw.endswith("m"):
            return value * 60 * 1000
        if raw.endswith("h"):
            return value * 60 * 60 * 1000
        if raw.endswith("d"):
            return value * 24 * 60 * 60 * 1000
        return 60 * 60 * 1000

    def _ai_extreme_risk(self, assessment: AiRiskAssessment) -> bool:
        if not self.settings.ai_extreme_risk_cancel_buyback:
            return False
        text = f"{assessment.status} {assessment.veto_reason}".lower()
        return assessment.risk_score >= 0.9 or "extreme" in text or "极端" in text

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
        if trigger in {"stop_loss", "emergency_stop"}:
            return PositionActivationDecision(
                action="SELL",
                trigger=trigger,
                reason="止损成交后更新分层退出状态" if trigger == "stop_loss" else "极端风险退出成交后更新状态",
                quantity=quantity,
            )

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
            elif execution_status in {"ORDER_OPEN", "ORDER_LADDER_OPEN"}:
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
                    decision_state=str(execution.get("decision_state", "")),
                    guard_result=str(execution.get("guard_result", "")),
                    net_edge_pct=float(execution.get("net_edge_pct", 0.0) or 0.0),
                    cooldown_remaining_bars=int(execution.get("cooldown_remaining_bars", 0) or 0),
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
