from __future__ import annotations

from dataclasses import asdict, replace
import time
from typing import Dict, List, Tuple

from binance_ai.config import Settings
from binance_ai.composite_decision import CompositeDecisionEngine
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.data.market_data import MarketDataService
from binance_ai.direction import DirectionDecisionEngine
from binance_ai.engine.decision_scheduler import DecisionScheduler
from binance_ai.execution.executor import OrderExecutor
from binance_ai.llm.market_analyst import MarketAnalyst, build_market_snapshot
from binance_ai.models import AccountSnapshot, AiRiskAssessment, BuyDecisionDiagnostic, CompositeDecision, CycleDecision, CycleReport, DecisionLedgerEntry, DirectionDecision, LlmAnalysis, OrderLifecycleEvent, OrderProposal, OrderRequest, PolicyDecision, PositionDiagnostic, ScenarioDecision, SchedulingDiagnostic, SellDecisionDiagnostic, SignalAction, make_client_order_id
from binance_ai.news.service import NewsService
from binance_ai.paper.portfolio import PaperPortfolio
from binance_ai.policy.engine import PolicyContext, PolicyEngine
from binance_ai.position_activation import PositionActivationDecision, PositionActivationEngine
from binance_ai.risk.engine import RiskEngine
from binance_ai.scenario import ScenarioEngine
from binance_ai.strategy.base import Strategy
from binance_ai.target_inventory import TargetInventoryDecision, TargetInventoryEngine
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
        self.target_inventory = TargetInventoryEngine(settings)
        self.composite_decision = CompositeDecisionEngine(settings)
        self.policy_engine = PolicyEngine(settings)
        self.direction_decision = DirectionDecisionEngine(settings)
        self.scenario_engine = ScenarioEngine(settings)

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
        composite_decisions: List[CompositeDecision] = []
        policy_decisions: List[PolicyDecision] = []
        direction_decisions: List[DirectionDecision] = []
        scenario_decisions: List[ScenarioDecision] = []
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
                    "3m",
                    "5m",
                    "30m",
                    "1h",
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
                self._record_daily_risk_fill(symbol=symbol, result=result, timestamp_ms=cycle_timestamp_ms)
                activation_success = self._activation_success_from_fill(symbol=symbol, result=result)
                if activation_success is not None:
                    self._record_position_activation_success(
                        symbol=symbol,
                        decision=activation_success,
                        fill_price=float(result.get("fill_price", price)),
                        timestamp_ms=cycle_timestamp_ms,
                    )
                self._record_entry_success_from_fill(
                    symbol=symbol,
                    result=result,
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
            protective_exit_block_reason = self._protective_exit_block_reason(symbol=symbol, exit_reason=exit_reason)
            if protective_exit_block_reason:
                exit_reason = None
                signal = replace(
                    signal,
                    action=SignalAction.HOLD,
                    confidence=min(signal.confidence, 0.5),
                    reason=protective_exit_block_reason,
                )
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
            if scheduler_exit_reason is None and has_position and signal.action == SignalAction.SELL:
                scheduler_exit_reason = "strategy_sell"
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
            target_inventory = self.target_inventory.evaluate(
                symbol=symbol,
                price=price,
                account=account,
                base_balance=base_balance,
                signal=signal,
                candles=context["candles"],
                ai_assessment=ai_assessment,
                daily_risk_state=self._daily_risk_state(symbol=symbol, timestamp_ms=cycle_timestamp_ms),
            )
            open_orders = list(context.get("open_orders", []))
            cooldown_remaining_bars = self._buyback_cooldown_remaining_bars(
                symbol=symbol,
                timestamp_ms=cycle_timestamp_ms,
            )
            composite_decision = self.composite_decision.evaluate(
                symbol=symbol,
                price=price,
                candles=context["candles"],
                signal=signal,
                position=context["position"],
                quote_balance=account.balance_of(self.settings.quote_asset),
                target_inventory=target_inventory,
                ai_assessment=ai_assessment,
                open_orders=open_orders,
                activation_state=self._activation_state_for_symbol(symbol),
                timestamp_ms=cycle_timestamp_ms,
            )
            composite_decisions.append(composite_decision)
            if self.settings.composite_decision_enabled:
                if composite_decision.entry_protection.get("active"):
                    cooldown_remaining_bars = max(
                        cooldown_remaining_bars,
                        int(composite_decision.entry_protection.get("remaining_bars", 0) or 0),
                    )
                if composite_decision.recommended_action == "BUY" and signal.action != SignalAction.BUY:
                    signal = replace(
                        signal,
                        action=SignalAction.BUY,
                        confidence=max(signal.confidence, composite_decision.buy_score),
                        reason=composite_decision.explanation_cn,
                        regime=composite_decision.scenario,
                    )
                elif composite_decision.recommended_action == "SELL" and signal.action != SignalAction.SELL:
                    signal = replace(
                        signal,
                        action=SignalAction.SELL,
                        confidence=max(signal.confidence, composite_decision.sell_score),
                        reason=composite_decision.explanation_cn,
                        regime=composite_decision.scenario,
                    )
                if (
                    exit_reason in {"stop_loss", "trailing_stop", "take_profit", "max_hold_exit"}
                    and composite_decision.recommended_action != "RISK_EXIT"
                    and composite_decision.risk_score < self.settings.risk_exit_score_threshold
                ):
                    exit_reason = None
                if (
                    exit_reason == "emergency_stop"
                    and composite_decision.recommended_action != "RISK_EXIT"
                    and composite_decision.risk_score < self.settings.risk_exit_score_threshold
                ):
                    exit_reason = None
                if composite_decision.entry_protection.get("active"):
                    remaining = int(composite_decision.entry_protection.get("remaining_bars", 0) or 0)
                    protection_reason = f"入场保护剩余 {remaining} 根K线，暂停普通卖出/跟踪止损/超时退出"
                    if exit_reason in {"trailing_stop", "take_profit", "max_hold_exit"}:
                        exit_reason = None
                    elif exit_reason == "stop_loss" and (
                        not self.settings.entry_protection_allow_emergency_stop
                        or composite_decision.risk_score < self.settings.risk_exit_score_threshold
                    ):
                        exit_reason = None
                    if signal.action == SignalAction.SELL and composite_decision.risk_score < self.settings.risk_exit_score_threshold:
                        signal = replace(
                            signal,
                            action=SignalAction.HOLD,
                            confidence=min(signal.confidence, 0.5),
                            reason=protection_reason,
                            regime=composite_decision.scenario,
                        )
            scenario_decision = self.scenario_engine.evaluate(
                symbol=symbol,
                price=price,
                candles_by_interval=context["candles_by_interval"],
                target_inventory=target_inventory,
                ai_assessment=ai_assessment,
                has_position=has_position,
            )
            if not self.settings.scenario_engine_enabled:
                scenario_decision = ScenarioDecision(
                    symbol=symbol,
                    scenario_state="RANGE_MARKET_MAKING",
                    reason_cn="场景引擎关闭，沿用区间做市模板",
                    allowed_actions=["BUY", "SELL"],
                    order_templates=[{"name": "pair_market_making", "source": "disabled"}],
                )
            scenario_decisions.append(scenario_decision)
            direction_decision = self.direction_decision.evaluate(
                symbol=symbol,
                price=price,
                candles=context["candles"],
                signal=signal,
                target_inventory=target_inventory,
                ai_assessment=ai_assessment,
                open_orders=open_orders,
                exit_reason=exit_reason,
            )
            direction_decisions.append(direction_decision)
            policy_decision = self.policy_engine.evaluate(
                PolicyContext(
                    symbol=symbol,
                    price=price,
                    candles=context["candles"],
                    signal=signal,
                    exit_reason=exit_reason,
                    has_position=has_position,
                    base_balance=base_balance,
                    quote_balance=account.balance_of(self.settings.quote_asset),
                    filters=filters,
                    target_inventory=target_inventory,
                    composite_decision=composite_decision,
                    ai_assessment=ai_assessment,
                    open_orders=open_orders,
                    activation_state=self._activation_state_for_symbol(symbol),
                    timestamp_ms=cycle_timestamp_ms,
                    direction_decision=direction_decision if self.settings.direction_engine_enabled else None,
                    scenario_decision=scenario_decision if self.settings.scenario_engine_enabled else None,
                    pair_profitability_stats=self._pair_profitability_stats(symbol),
                )
            )
            policy_decisions.append(policy_decision)
            self._record_pair_policy_state(symbol=symbol, policy_decision=policy_decision, timestamp_ms=cycle_timestamp_ms)
            if self.settings.policy_engine_enabled:
                if policy_decision.policy_state in {"PAIR_LOCKED_AFTER_STOP", "OBSERVE_ONLY"} and signal.action == SignalAction.BUY:
                    signal = replace(
                        signal,
                        action=SignalAction.HOLD,
                        confidence=min(signal.confidence, 0.5),
                        reason=policy_decision.mode_reason_cn,
                        regime=policy_decision.policy_state,
                    )
            applied_ai_assessment = ai_assessment if signal.action == SignalAction.BUY else None

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
            risk_reentry_block_reason = self._risk_exit_reentry_block_reason(symbol=symbol, price=price)

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

            policy_handled = False
            policy_has_active_locks = any(lock.active for lock in policy_decision.protection_locks)
            if (
                self.settings.policy_engine_enabled
                and scheduling.should_run_decision
                and open_order_summary.get("status") in {"NO_OPEN_ORDERS", "CANCELED"}
            ):
                if policy_decision.order_proposals:
                    execution_result, events, orders = self._submit_policy_proposals(
                        policy_decision=policy_decision,
                        current_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                    )
                    order_lifecycle_events.extend(events)
                    order = orders[0] if orders else None
                    policy_handled = True
                elif policy_has_active_locks:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": "policy_protection_lock_active",
                        "policy_state": policy_decision.policy_state,
                        "policy_reason": policy_decision.mode_reason_cn,
                        "proposal_filter_results": [asdict(item) for item in policy_decision.proposal_filter_results],
                    }
                    policy_handled = True
                elif (
                    self.settings.direction_engine_enabled
                    and not self.settings.legacy_direct_order_fallback
                    and activation_decision.order is None
                    and not risk_reentry_block_reason
                ):
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": "direction_policy_no_order",
                        "direction_reason": direction_decision.reason_cn,
                        "direction_decision": asdict(direction_decision),
                        "policy_state": policy_decision.policy_state,
                        "policy_reason": policy_decision.mode_reason_cn,
                        "proposal_filter_results": [asdict(item) for item in policy_decision.proposal_filter_results],
                    }
                    policy_handled = True

            if policy_handled:
                pass
            elif open_order_summary.get("status") == "CANCELED":
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
                if risk_reentry_block_reason:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": "risk_exit_reentry_price_not_reached",
                        "detail": risk_reentry_block_reason,
                        "trigger": "strategy_buy",
                        "scenario": composite_decision.scenario,
                    }
                elif not ai_assessment.allow_entry:
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
                        target_inventory=target_inventory,
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
                            execution_result["target_inventory_summary"] = target_inventory.as_dict()
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
                if self.settings.target_inventory_enabled:
                    decision_order, decision_reason = self._build_target_position_sell_order(
                        symbol=symbol,
                        price=price,
                        filters=filters,
                        target_inventory=target_inventory,
                    )
                else:
                    decision = self.risk.build_sell_order(
                        symbol,
                        price,
                        base_balance,
                        filters,
                        sell_fraction=self.risk.exit_sell_fraction(None, strategy_sell=True),
                    )
                    decision_order = decision.order
                    decision_reason = decision.reason
                if decision_order is not None:
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
                            decision_order,
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
                        execution_result["target_inventory_summary"] = target_inventory.as_dict()
                else:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": decision_reason,
                        "target_inventory_summary": target_inventory.as_dict(),
                    }
            elif (
                self.settings.target_inventory_enabled
                and has_position
                and target_inventory.active_trading_allowed
                and target_inventory.allowed_sell_quantity > 0
            ):
                target_sell_order, target_sell_blocker = self._build_target_position_sell_order(
                    symbol=symbol,
                    price=price,
                    filters=filters,
                    target_inventory=target_inventory,
                )
                if target_sell_order is not None:
                    execution_result, events, orders = self._submit_ladder_orders(
                        target_sell_order,
                        current_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                        trigger="target_rebalance_sell",
                        urgent=False,
                        ladder_group="exit",
                        tiers_raw=self.settings.exit_ladder_tiers,
                    )
                    order_lifecycle_events.extend(events)
                    order = orders[0] if orders else None
                    execution_result["target_inventory_summary"] = target_inventory.as_dict()
                else:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": target_sell_blocker,
                        "trigger": "target_rebalance_sell",
                        "target_inventory_summary": target_inventory.as_dict(),
                    }
            elif activation_decision.order is not None:
                tiers_raw = ""
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
            elif self._can_run_capital_deployment(
                symbol=symbol,
                signal_action=signal.action.value,
                exit_reason=exit_reason,
                ai_assessment=ai_assessment,
                timestamp_ms=cycle_timestamp_ms,
            ):
                target_order, target_blocker = self._build_target_position_buy_order(
                    symbol=symbol,
                    price=price,
                    account=account,
                    base_balance=base_balance,
                    filters=filters,
                    position_multiplier=ai_assessment.position_multiplier,
                    target_inventory=target_inventory,
                )
                if risk_reentry_block_reason:
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": "risk_exit_reentry_price_not_reached",
                        "detail": risk_reentry_block_reason,
                        "trigger": "target_rebuild_buy",
                        "decision_state": self._decision_state_for_symbol(symbol),
                        "cooldown_remaining_bars": cooldown_remaining_bars,
                    }
                elif target_order is not None:
                    execution_result, events, orders = self._submit_ladder_orders(
                        target_order,
                        current_price=price,
                        filters=filters,
                        timestamp_ms=cycle_timestamp_ms,
                        entry_candle_close_time_ms=latest_closed_candle_close_time,
                        trigger="target_rebuild_buy",
                        urgent=False,
                        ladder_group="entry",
                        tiers_raw=self.settings.entry_ladder_tiers,
                    )
                    order_lifecycle_events.extend(events)
                    order = orders[0] if orders else None
                    execution_result["target_position_fraction"] = self.settings.target_position_fraction
                    execution_result["min_cash_reserve_fraction"] = self.settings.min_cash_reserve_fraction
                    execution_result["target_inventory_summary"] = target_inventory.as_dict()
                    execution_result["capital_deployment"] = True
                else:
                    self._record_position_activation_state(
                        symbol=symbol,
                        decision=activation_decision,
                        timestamp_ms=cycle_timestamp_ms,
                    )
                    execution_result = {
                        "status": "BLOCKED",
                        "reason": target_blocker,
                        "trigger": "target_rebuild_buy",
                        "decision_state": self._decision_state_for_symbol(symbol),
                        "cooldown_remaining_bars": cooldown_remaining_bars,
                    }
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
            execution_result.setdefault("target_inventory_summary", target_inventory.as_dict())
            execution_result.setdefault("composite_decision", asdict(composite_decision))
            execution_result.setdefault("direction_decision", asdict(direction_decision))
            execution_result.setdefault("fair_value_summary", direction_decision.fair_value_summary)
            execution_result.setdefault("price_zone", direction_decision.price_zone)
            execution_result.setdefault("expected_net_edge_pct", direction_decision.expected_net_edge_pct)
            execution_result.setdefault("paired_order_state", direction_decision.paired_order_state)
            execution_result.setdefault("policy_decision", asdict(policy_decision))
            execution_result.setdefault("scenario_decision", asdict(scenario_decision))
            execution_result.setdefault("scenario_state", scenario_decision.scenario_state)
            execution_result.setdefault("scenario_reason_cn", scenario_decision.reason_cn)
            execution_result.setdefault("scenario_indicators", scenario_decision.indicators)
            execution_result.setdefault("scenario_order_templates", scenario_decision.order_templates)
            execution_result.setdefault("policy_state", policy_decision.policy_state)
            execution_result.setdefault("protection_locks", [asdict(item) for item in policy_decision.protection_locks])
            execution_result.setdefault("proposal_filter_results", [asdict(item) for item in policy_decision.proposal_filter_results])
            if policy_decision.inventory_skew_summary is not None:
                execution_result.setdefault("inventory_skew_summary", asdict(policy_decision.inventory_skew_summary))
            execution_result.setdefault("scenario", composite_decision.scenario)
            execution_result.setdefault("score_breakdown", composite_decision.score_breakdown)
            execution_result.setdefault("target_position_summary", composite_decision.target_position_summary)
            execution_result.setdefault("entry_protection", composite_decision.entry_protection)
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
            composite_decisions=composite_decisions,
            policy_decisions=policy_decisions,
            direction_decisions=direction_decisions,
            scenario_decisions=scenario_decisions,
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
        signal_action: str = "",
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
        client_order_id = make_client_order_id(
            symbol=order.symbol,
            side=side,
            trigger=ladder_group or trigger or "order",
            tier_index=tier_index,
            timestamp_ms=timestamp_ms,
        )
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
            target_spread_pct=offset,
            created_reference_price=price,
            created_signal_action=signal_action,
        )

    def _can_run_capital_deployment(
        self,
        *,
        symbol: str,
        signal_action: str,
        exit_reason: str | None,
        ai_assessment: AiRiskAssessment,
        timestamp_ms: int,
    ) -> bool:
        if not self.settings.order_ladder_enabled or self.settings.target_position_fraction <= 0:
            return False
        if not self.settings.target_inventory_enabled:
            state = self._activation_state_for_symbol(symbol)
            if float(state.get("pending_buyback_quantity", 0.0) or 0.0) > 0:
                return False
            if self._buyback_cooldown_remaining_bars(symbol=symbol, timestamp_ms=timestamp_ms) > 0:
                return False
        if signal_action.upper() == "SELL":
            return False
        if exit_reason in {"emergency_stop", "stop_loss"}:
            return False
        if not ai_assessment.allow_entry or self._ai_extreme_risk(ai_assessment):
            return False
        return True

    def _build_target_position_buy_order(
        self,
        *,
        symbol: str,
        price: float,
        account: AccountSnapshot,
        base_balance: float,
        filters,
        position_multiplier: float,
        target_inventory: TargetInventoryDecision | None = None,
    ) -> Tuple[OrderRequest | None, str]:
        if self.settings.target_inventory_enabled and target_inventory is not None:
            order, reason = self.target_inventory.build_buy_order(
                decision=target_inventory,
                price=price,
                quantize_quantity=self.client.quantize_quantity,
                step_size=filters.step_size,
                min_qty=filters.min_qty,
            )
            if order is None:
                return None, reason
            return order, ""

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

    def _build_target_position_sell_order(
        self,
        *,
        symbol: str,
        price: float,
        filters,
        target_inventory: TargetInventoryDecision,
    ) -> Tuple[OrderRequest | None, str]:
        order, reason = self.target_inventory.build_sell_order(
            decision=target_inventory,
            price=price,
            quantize_quantity=self.client.quantize_quantity,
            step_size=filters.step_size,
            min_qty=filters.min_qty,
        )
        if order is None:
            return None, reason
        return order, ""

    def _submit_policy_proposals(
        self,
        *,
        policy_decision: PolicyDecision,
        current_price: float,
        filters,
        timestamp_ms: int,
        entry_candle_close_time_ms: int,
    ) -> Tuple[Dict[str, object], List[OrderLifecycleEvent], List[OrderRequest]]:
        results: List[Dict[str, object]] = []
        events: List[OrderLifecycleEvent] = []
        orders: List[OrderRequest] = []
        for proposal in policy_decision.order_proposals:
            order = self._order_from_policy_proposal(
                proposal,
                current_price=current_price,
                filters=filters,
                timestamp_ms=timestamp_ms,
            )
            if order is None:
                results.append(
                    {
                        "status": "BLOCKED",
                        "reason": "policy_proposal_quantity_below_minimum",
                        "trigger": proposal.trigger,
                        "ladder_group": proposal.ladder_group,
                    }
                )
                continue
            result, event = self.executor.submit_limit_order(
                order,
                current_price=order.limit_price or current_price,
                filters=filters,
                timestamp_ms=timestamp_ms,
                entry_candle_close_time_ms=entry_candle_close_time_ms,
            )
            results.append(result)
            orders.append(order)
            if event is not None:
                events.append(event)

        accepted = [item for item in results if str(item.get("status")) in {"ORDER_OPEN", "ORDER_LADDER_OPEN", "UNKNOWN"}]
        if len(results) == 1:
            payload = dict(results[0])
        else:
            payload = {
                "status": "ORDER_LADDER_OPEN" if accepted else "BLOCKED",
                "reason": "policy_proposals_submitted" if accepted else "policy_proposals_blocked",
                "submitted_count": len(accepted),
                "proposal_count": len(policy_decision.order_proposals),
                "orders": results,
            }
        payload["policy_state"] = policy_decision.policy_state
        payload["policy_reason"] = policy_decision.mode_reason_cn
        if orders:
            payload["trigger"] = orders[0].trigger
            payload["side"] = orders[0].side
            payload["pair_id"] = orders[0].pair_id
            payload["pair_role"] = orders[0].pair_role
        payload["order_proposals"] = [asdict(item) for item in policy_decision.order_proposals]
        payload["proposal_filter_results"] = [asdict(item) for item in policy_decision.proposal_filter_results]
        payload["protection_locks"] = [asdict(item) for item in policy_decision.protection_locks]
        if policy_decision.inventory_skew_summary is not None:
            payload["inventory_skew_summary"] = asdict(policy_decision.inventory_skew_summary)
        if policy_decision.direction_decision is not None:
            payload["direction_decision"] = asdict(policy_decision.direction_decision)
            payload["fair_value_summary"] = policy_decision.direction_decision.fair_value_summary
            payload["price_zone"] = policy_decision.direction_decision.price_zone
            payload["expected_net_edge_pct"] = policy_decision.direction_decision.expected_net_edge_pct
            payload["paired_order_state"] = policy_decision.direction_decision.paired_order_state
        return payload, events, orders

    def _order_from_policy_proposal(
        self,
        proposal: OrderProposal,
        *,
        current_price: float,
        filters,
        timestamp_ms: int,
    ) -> OrderRequest | None:
        quantity = self.client.quantize_quantity(proposal.quantity, filters.step_size)
        if quantity <= 0 or quantity < filters.min_qty:
            return None
        side = proposal.side.upper()
        raw_limit = current_price * (1.0 - proposal.target_spread_pct) if side == "BUY" else current_price * (1.0 + proposal.target_spread_pct)
        quantize_price = getattr(self.client, "quantize_price", None)
        limit_price = (
            quantize_price(raw_limit, getattr(filters, "tick_size", 0.0))
            if callable(quantize_price)
            else raw_limit
        )
        return OrderRequest(
            symbol=proposal.symbol,
            side=proposal.side,
            order_type="LIMIT",
            quantity=quantity,
            limit_price=limit_price,
            time_in_force=self.settings.order_time_in_force,
            client_order_id=make_client_order_id(
                symbol=proposal.symbol,
                side=proposal.side,
                trigger=proposal.pair_id or proposal.trigger,
                tier_index=proposal.tier_index,
                timestamp_ms=timestamp_ms,
            ),
            trigger=proposal.trigger,
            ladder_group=proposal.ladder_group,
            target_fraction=proposal.target_fraction,
            target_spread_pct=proposal.target_spread_pct,
            created_reference_price=current_price,
            created_signal_action=proposal.side.upper(),
            pair_id=proposal.pair_id,
            pair_role=proposal.pair_role,
            intended_counter_price=proposal.intended_counter_price,
            expected_pair_net_edge_pct=proposal.expected_pair_net_edge_pct,
        )

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
        min_notional = self._effective_min_notional(filters=filters, urgent=urgent)
        if len(tiers) > 1 and order.quantity * current_price < min_notional * len(tiers):
            tiers = [(tiers[0][0] if tiers else 0.0, 1.0)]
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
                signal_action=order.side.upper(),
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
        duplicate_rejections = [
            item
            for item in rejected
            if str(item.get("reason")) == "duplicate_open_ladder_order"
        ]
        if not results:
            return {
                "status": "BLOCKED",
                "reason": "ladder_orders_below_minimums",
                "trigger": trigger,
                "ladder_group": ladder_group,
                "min_effective_order_notional": min_notional,
            }, events, orders
        if len(accepted) == 1 and not rejected:
            result = dict(accepted[0])
            result["ladder_group"] = ladder_group
            result["tier_index"] = orders[0].tier_index if orders else 0
            result["min_effective_order_notional"] = min_notional
            return result, events, orders
        if rejected and len(duplicate_rejections) == len(rejected) and not accepted:
            return {
                "status": "ORDER_OPEN",
                "reason": "open_order_group_waiting_for_touch",
                "trigger": trigger,
                "ladder_group": ladder_group,
                "submitted_count": 0,
                "rejected_count": len(rejected),
            }, events, orders
        return {
            "status": "ORDER_LADDER_OPEN" if accepted else "REJECTED",
            "reason": "order_ladder_submitted" if accepted else "order_ladder_rejected",
            "trigger": trigger,
            "ladder_group": ladder_group,
            "submitted_count": len(accepted),
            "rejected_count": len(rejected),
            "orders": results,
            "min_effective_order_notional": min_notional,
        }, events, orders

    def _effective_min_notional(self, *, filters, urgent: bool) -> float:
        base_min = max(filters.min_notional, self.settings.min_order_notional)
        if urgent:
            return base_min
        configured = max(0.0, self.settings.min_effective_order_notional)
        if configured <= base_min:
            return base_min
        if self.paper_portfolio is not None:
            try:
                snapshot = self.paper_portfolio.load_snapshot()
                if snapshot.initial_quote_balance < configured * 2:
                    return base_min
            except Exception:  # noqa: BLE001 - fall back to exchange minimums.
                return base_min
        return configured

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
                    "target_spread_pct": float(open_order_action.get("target_spread_pct", 0.0) or 0.0),
                    "current_spread_pct": float(open_order_action.get("current_spread_pct", 0.0) or 0.0),
                    "spread_delta_pct": float(open_order_action.get("spread_delta_pct", 0.0) or 0.0),
                    "reprice_tolerance_pct": float(open_order_action.get("reprice_tolerance_pct", 0.0) or 0.0),
                    "age_seconds": float(open_order_action.get("age_seconds", 0.0) or 0.0),
                    "compare_mode": str(open_order_action.get("compare_mode", "")),
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

    def _pair_profitability_stats(self, symbol: str) -> Dict[str, object]:
        if self.paper_portfolio is None:
            return {}
        raw = self.paper_portfolio.load_snapshot().pair_profitability_stats.get(symbol, {})
        return raw if isinstance(raw, dict) else {}

    def _record_pair_policy_state(
        self,
        *,
        symbol: str,
        policy_decision: PolicyDecision,
        timestamp_ms: int,
    ) -> None:
        if self.paper_portfolio is None:
            return
        snapshot = self.paper_portfolio.load_snapshot()
        pair_locks = dict(snapshot.pair_locks)
        pair_locks[symbol] = {
            "timestamp_ms": timestamp_ms,
            "policy_state": policy_decision.policy_state,
            "mode_reason_cn": policy_decision.mode_reason_cn,
            "protection_locks": [asdict(item) for item in policy_decision.protection_locks],
            "inventory_skew_summary": asdict(policy_decision.inventory_skew_summary) if policy_decision.inventory_skew_summary is not None else {},
            "blockers": list(policy_decision.blockers),
        }
        self.paper_portfolio.save_snapshot(replace(snapshot, pair_locks=pair_locks))

    def _daily_risk_state(self, *, symbol: str, timestamp_ms: int) -> Dict[str, object]:
        if self.paper_portfolio is None:
            return self.target_inventory.normalized_daily_state({}, timestamp_ms=timestamp_ms)
        snapshot = self.paper_portfolio.load_snapshot()
        raw = snapshot.activation_state.get("_daily_risk", {})
        symbol_state = raw.get(symbol, {}) if isinstance(raw, dict) else {}
        return self.target_inventory.normalized_daily_state(
            symbol_state if isinstance(symbol_state, dict) else {},
            timestamp_ms=timestamp_ms,
        )

    def _record_daily_risk_fill(self, *, symbol: str, result: Dict[str, object], timestamp_ms: int) -> None:
        if self.paper_portfolio is None or result.get("status") != "PAPER_FILLED":
            return
        snapshot = self.paper_portfolio.load_snapshot()
        activation_state = dict(snapshot.activation_state)
        daily_all = dict(activation_state.get("_daily_risk", {}) if isinstance(activation_state.get("_daily_risk"), dict) else {})
        state = self.target_inventory.normalized_daily_state(
            daily_all.get(symbol, {}) if isinstance(daily_all.get(symbol, {}), dict) else {},
            timestamp_ms=timestamp_ms,
        )
        state["turnover_notional"] = float(state.get("turnover_notional", 0.0) or 0.0) + float(result.get("notional", 0.0) or 0.0)
        state["realized_pnl"] = float(state.get("realized_pnl", 0.0) or 0.0) + float(result.get("realized_pnl_delta", 0.0) or 0.0)
        daily_all[symbol] = state
        activation_state["_daily_risk"] = daily_all
        self.paper_portfolio.save_snapshot(replace(snapshot, activation_state=activation_state))

    def _decision_state_for_symbol(self, symbol: str) -> str:
        state = self._activation_state_for_symbol(symbol)
        decision_state = str(state.get("decision_state", "NORMAL"))
        if self._entry_protection_remaining_bars(symbol=symbol, timestamp_ms=int(time.time() * 1000)) > 0:
            return "ENTRY_PROTECTION"
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

    def _entry_protection_remaining_bars(self, *, symbol: str, timestamp_ms: int) -> int:
        state = self._activation_state_for_symbol(symbol)
        protection_until = int(float(state.get("entry_protection_until_candle", 0) or 0))
        if protection_until <= timestamp_ms:
            return 0
        interval_ms = max(1, int(float(state.get("entry_protection_interval_ms", 0) or 0)) or self._settings_interval_ms())
        return int((protection_until - timestamp_ms + interval_ms - 1) // interval_ms)

    def _risk_exit_reentry_block_reason(self, *, symbol: str, price: float) -> str:
        state = self._activation_state_for_symbol(symbol)
        last_exit_price = float(state.get("last_risk_exit_price", 0.0) or 0.0)
        reentry_price = float(state.get("risk_exit_reentry_price", 0.0) or 0.0)
        if last_exit_price <= 0 or reentry_price <= 0:
            return ""
        if price <= reentry_price:
            return ""
        return (
            f"最近风险卖出价 {last_exit_price:.4f}，回补必须低于 {reentry_price:.4f} "
            f"以覆盖手续费和净边际；当前价 {price:.4f}，禁止高价买回"
        )

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
        if exit_reason != "take_profit":
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

    def _record_entry_success_from_fill(
        self,
        *,
        symbol: str,
        result: Dict[str, object],
        fill_price: float,
        timestamp_ms: int,
    ) -> None:
        if self.paper_portfolio is None or result.get("status") != "PAPER_FILLED":
            return
        if str(result.get("side", "")).upper() != "BUY":
            return
        trigger = str(result.get("trigger", ""))
        if trigger not in {"strategy_buy", "target_rebuild_buy", "grid_buyback"}:
            return
        snapshot = self.paper_portfolio.load_snapshot()
        activation_state = dict(snapshot.activation_state)
        state = dict(activation_state.get(symbol, {}) if isinstance(activation_state.get(symbol, {}), dict) else {})
        interval_ms = self._settings_interval_ms()
        protection_until = timestamp_ms + interval_ms * max(0, self.settings.entry_protection_bars)
        state.update(
            {
                "decision_state": "ENTRY_PROTECTION",
                "entry_protection_until_candle": protection_until,
                "entry_protection_interval_ms": interval_ms,
                "entry_protection_remaining_bars": max(0, self.settings.entry_protection_bars),
                "last_entry_trigger": trigger,
                "last_entry_price": fill_price,
            }
        )
        activation_state[symbol] = state
        self.paper_portfolio.save_snapshot(replace(snapshot, activation_state=activation_state))

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
        if trigger in {"stop_loss", "emergency_stop", "trailing_stop", "max_hold_exit"}:
            return PositionActivationDecision(
                action="SELL",
                trigger=trigger,
                reason=self._protective_exit_success_reason(trigger),
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
    def _protective_exit_success_reason(trigger: str) -> str:
        labels = {
            "stop_loss": "止损成交后更新分层退出状态",
            "emergency_stop": "极端风险退出成交后更新状态",
            "trailing_stop": "跟踪止损成交后进入保护退出状态",
            "max_hold_exit": "超时退出成交后进入保护退出状态",
        }
        return labels.get(trigger, "保护性退出成交后更新状态")

    def _protective_exit_block_reason(self, *, symbol: str, exit_reason: str | None) -> str:
        if exit_reason not in {"trailing_stop", "max_hold_exit"} or self.paper_portfolio is None:
            return ""
        state = self.paper_portfolio.load_snapshot().activation_state.get(symbol, {})
        if not isinstance(state, dict):
            return ""
        if state.get("decision_state") == "PROTECTIVE_EXIT_ACTIVE" and state.get("last_trigger") == exit_reason:
            return f"{exit_reason} 已执行过保护性部分退出，等待新入场/回补条件，不重复连续卖出"
        return ""

    @staticmethod
    def _release_reason_for_trigger(trigger: str) -> str:
        labels = {
            "strategy_release_sell": "策略卖出已登记待回补",
            "take_profit_release_sell": "止盈部分卖出已登记待回补",
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
                    policy_state=str(execution.get("policy_state", "")),
                    policy_reason=str(execution.get("policy_reason", "")),
                    direction_mode=str((execution.get("direction_decision") or {}).get("mode", "") if isinstance(execution.get("direction_decision"), dict) else ""),
                    price_zone=str(execution.get("price_zone", "")),
                    direction_reason=str(execution.get("direction_reason") or ((execution.get("direction_decision") or {}).get("reason_cn", "") if isinstance(execution.get("direction_decision"), dict) else "")),
                    pair_id=str(execution.get("pair_id", "")),
                    pair_role=str(execution.get("pair_role", "")),
                    expected_pair_net_edge_pct=float(execution.get("expected_pair_net_edge_pct", 0.0) or 0.0),
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
