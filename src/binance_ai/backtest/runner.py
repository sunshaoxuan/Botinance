from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Dict, List, Sequence

from binance_ai.backtest.models import (
    BacktestConfig,
    BacktestDataset,
    BacktestRunResult,
    BacktestSummary,
    BacktestTrade,
    EquityPoint,
    OpenBacktestPosition,
    WalkForwardSegmentResult,
)
from binance_ai.backtest.data_loader import parse_date_from, parse_date_to
from binance_ai.backtest.portfolio import BacktestPortfolioEngine
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import Candle, SignalAction, SymbolFilters
from binance_ai.risk.engine import RiskEngine
from binance_ai.strategy.base import Strategy


class BacktestRunner:
    def __init__(
        self,
        config: BacktestConfig,
        client: BinanceSpotClient,
        strategy: Strategy,
        risk: RiskEngine,
    ) -> None:
        self.config = config
        self.client = client
        self.strategy = strategy
        self.risk = risk
        self.filters = self.client.get_symbol_filters(config.symbol)

    def run(
        self,
        dataset: BacktestDataset,
        *,
        evaluation_start_ms: int | None = None,
        summary_date_from: str | None = None,
        summary_date_to: str | None = None,
    ) -> BacktestRunResult:
        main_candles = list(dataset.candles_by_interval[self.config.main_interval])
        needed = max(
            self.config.slow_window + 1,
            self.config.entry_slow_window + 1,
            self.config.trend_slow_window,
        )
        if len(main_candles) < needed:
            raise ValueError(
                f"Insufficient main interval candles for backtest: {len(main_candles)}/{needed}"
            )

        portfolio = BacktestPortfolioEngine(
            quote_asset=self.config.quote_asset,
            initial_quote_balance=self.config.initial_quote_balance,
        )
        trades: List[BacktestTrade] = []
        equity_curve: List[EquityPoint] = []
        open_trade: OpenBacktestPosition | None = None
        interval_indices = {interval: 0 for interval in dataset.candles_by_interval}

        for index, main_candle in enumerate(main_candles):
            current_close_time = main_candle.close_time
            if evaluation_start_ms is not None and current_close_time < evaluation_start_ms:
                self._advance_interval_indices(
                    dataset.candles_by_interval,
                    interval_indices,
                    current_close_time,
                )
                continue

            candles_by_interval = self._slice_candles_by_interval(
                dataset.candles_by_interval,
                interval_indices,
                current_close_time,
            )
            price = main_candle.close
            portfolio.mark_to_market(
                self.config.symbol,
                mark_price=price,
                timestamp_ms=current_close_time,
                candle_close_time_ms=current_close_time,
            )

            position = portfolio.position_snapshot(self.config.symbol)
            if position is not None and open_trade is not None:
                open_trade = replace(
                    open_trade,
                    max_close_since_entry=max(open_trade.max_close_since_entry, price),
                    min_close_since_entry=min(open_trade.min_close_since_entry, price),
                )

            exited_this_bar = False
            if position is not None:
                exit_reason = self.risk.determine_exit_reason(
                    price=price,
                    position=position,
                    candles=candles_by_interval[self.config.main_interval],
                    current_timestamp_ms=current_close_time,
                )
                if exit_reason is not None:
                    exited_this_bar = self._execute_sell(
                        portfolio=portfolio,
                        trades=trades,
                        open_trade=open_trade,
                        exit_reason=exit_reason,
                        price=price,
                        current_close_time=current_close_time,
                        current_bar_index=index,
                    )
                    if exited_this_bar:
                        open_trade = None
                        position = None

            if not exited_this_bar:
                has_position = portfolio.position_snapshot(self.config.symbol) is not None
                signal = self.strategy.generate(
                    symbol=self.config.symbol,
                    candles_by_interval=candles_by_interval,
                    has_position=has_position,
                )
                if has_position and signal.action == SignalAction.SELL:
                    exited_this_bar = self._execute_sell(
                        portfolio=portfolio,
                        trades=trades,
                        open_trade=open_trade,
                        exit_reason=signal.reason,
                        price=price,
                        current_close_time=current_close_time,
                        current_bar_index=index,
                    )
                    if exited_this_bar:
                        open_trade = None
                elif (not has_position) and signal.action == SignalAction.BUY:
                    open_trade = self._execute_buy(
                        portfolio=portfolio,
                        signal=signal,
                        price=price,
                        current_close_time=current_close_time,
                        current_bar_index=index,
                    )

            summary_snapshot = portfolio.equity_summary({self.config.symbol: price})
            equity_curve.append(
                EquityPoint(
                    segment_index=0,
                    timestamp_ms=current_close_time,
                    close_price=price,
                    quote_balance=summary_snapshot["quote_balance"],
                    market_value=summary_snapshot["market_value"],
                    total_equity=summary_snapshot["total_equity"],
                    realized_pnl=summary_snapshot["realized_pnl"],
                    unrealized_pnl=summary_snapshot["unrealized_pnl"],
                    net_pnl=summary_snapshot["net_pnl"],
                )
            )

        equity_curve = self._apply_drawdown(equity_curve)
        summary = self._build_summary(
            trades=trades,
            equity_curve=equity_curve,
            portfolio=portfolio,
            summary_date_from=summary_date_from or self.config.date_from,
            summary_date_to=summary_date_to or self.config.date_to,
        )
        return BacktestRunResult(
            summary=summary,
            trades=trades,
            equity_curve=equity_curve,
            dataset_infos=dataset.infos,
            config=self.config,
        )

    def run_walk_forward(
        self,
        loader,
    ) -> tuple[BacktestRunResult, List[BacktestRunResult], List[WalkForwardSegmentResult]]:
        baseline_dataset = loader.load(
            symbol=self.config.symbol,
            intervals=self._required_intervals(),
            date_from=self.config.date_from,
            date_to=self.config.date_to,
        )
        baseline = self.run(baseline_dataset)

        start_dt = parse_date_from(self.config.date_from)
        end_dt = parse_date_to(self.config.date_to)
        current_train_start = start_dt
        segment_runs: List[BacktestRunResult] = []
        segments: List[WalkForwardSegmentResult] = []
        segment_index = 1

        while True:
            train_end = current_train_start + timedelta(days=self.config.train_days)
            test_end = train_end + timedelta(days=self.config.test_days)
            if test_end > end_dt:
                break
            dataset = loader.load(
                symbol=self.config.symbol,
                intervals=self._required_intervals(),
                date_from=current_train_start.date().isoformat(),
                date_to=(test_end - timedelta(days=1)).date().isoformat(),
            )
            segment_run = self.run(
                dataset,
                evaluation_start_ms=int(train_end.timestamp() * 1000),
                summary_date_from=train_end.date().isoformat(),
                summary_date_to=(test_end - timedelta(days=1)).date().isoformat(),
            )
            segment_runs.append(self._reindex_segment(segment_run, segment_index))
            segment_summary = segment_run.summary
            baseline_expectancy = baseline.summary.expectancy_per_trade
            segments.append(
                WalkForwardSegmentResult(
                    segment_index=segment_index,
                    train_from=current_train_start.date().isoformat(),
                    train_to=(train_end - timedelta(days=1)).date().isoformat(),
                    test_from=train_end.date().isoformat(),
                    test_to=(test_end - timedelta(days=1)).date().isoformat(),
                    summary=segment_summary,
                    baseline_expectancy_per_trade=baseline_expectancy,
                    beats_baseline=segment_summary.expectancy_per_trade >= baseline_expectancy,
                )
            )
            segment_index += 1
            current_train_start += timedelta(days=self.config.step_days)

        if not segments:
            raise ValueError("Walk-forward window does not fit inside the requested date range.")

        return baseline, segment_runs, segments

    def _execute_buy(
        self,
        *,
        portfolio: BacktestPortfolioEngine,
        signal,
        price: float,
        current_close_time: int,
        current_bar_index: int,
    ) -> OpenBacktestPosition | None:
        account = portfolio.account_snapshot()
        decision = self.risk.build_buy_order(
            self.config.symbol,
            price,
            account,
            self.filters,
            position_multiplier=1.0,
        )
        if decision.order is None:
            return None
        result = portfolio.apply_order(
            decision.order,
            fill_price=price,
            min_notional=max(self.filters.min_notional, self.config.min_order_notional),
            min_qty=self.filters.min_qty,
            timestamp_ms=current_close_time,
            entry_candle_close_time_ms=current_close_time,
        )
        if result.get("status") != "PAPER_FILLED":
            return None
        return OpenBacktestPosition(
            symbol=self.config.symbol,
            entry_time_ms=current_close_time,
            entry_bar_index=current_bar_index,
            entry_price=price,
            quantity=decision.order.quantity,
            entry_signal=signal,
            entry_signal_reason=signal.reason,
            max_close_since_entry=price,
            min_close_since_entry=price,
        )

    def _execute_sell(
        self,
        *,
        portfolio: BacktestPortfolioEngine,
        trades: List[BacktestTrade],
        open_trade: OpenBacktestPosition | None,
        exit_reason: str,
        price: float,
        current_close_time: int,
        current_bar_index: int,
    ) -> bool:
        if open_trade is None:
            return False
        position = portfolio.position_snapshot(self.config.symbol)
        if position is None:
            return False
        decision = self.risk.build_sell_order(
            self.config.symbol,
            price,
            position.quantity,
            self.filters,
        )
        if decision.order is None:
            return False
        result = portfolio.apply_order(
            decision.order,
            fill_price=price,
            min_notional=max(self.filters.min_notional, self.config.min_order_notional),
            min_qty=self.filters.min_qty,
            timestamp_ms=current_close_time,
            entry_candle_close_time_ms=current_close_time,
        )
        if result.get("status") != "PAPER_FILLED":
            return False

        notional = open_trade.entry_price * open_trade.quantity
        realized_pnl = float(result.get("realized_pnl_delta", 0.0))
        trades.append(
            BacktestTrade(
                segment_index=0,
                symbol=self.config.symbol,
                side="LONG",
                entry_time_ms=open_trade.entry_time_ms,
                exit_time_ms=current_close_time,
                entry_price=open_trade.entry_price,
                exit_price=price,
                quantity=open_trade.quantity,
                notional=notional,
                realized_pnl=realized_pnl,
                return_pct=(realized_pnl / notional * 100) if notional else 0.0,
                hold_bars=max(1, current_bar_index - open_trade.entry_bar_index),
                hold_hours=(current_close_time - open_trade.entry_time_ms) / 3_600_000,
                mfe_pct=((open_trade.max_close_since_entry - open_trade.entry_price) / open_trade.entry_price * 100)
                if open_trade.entry_price
                else 0.0,
                mae_pct=((open_trade.min_close_since_entry - open_trade.entry_price) / open_trade.entry_price * 100)
                if open_trade.entry_price
                else 0.0,
                exit_reason=exit_reason,
                entry_signal_reason=open_trade.entry_signal_reason,
                exit_signal_reason=exit_reason,
            )
        )
        return True

    def _build_summary(
        self,
        *,
        trades: Sequence[BacktestTrade],
        equity_curve: Sequence[EquityPoint],
        portfolio: BacktestPortfolioEngine,
        summary_date_from: str,
        summary_date_to: str,
    ) -> BacktestSummary:
        total_return_pct = 0.0
        max_drawdown_pct = max((point.drawdown_pct for point in equity_curve), default=0.0)
        completed_trade_count = len(trades)
        trade_count = completed_trade_count + (1 if portfolio.snapshot.positions else 0)
        ending_total_equity = equity_curve[-1].total_equity if equity_curve else self.config.initial_quote_balance
        if self.config.initial_quote_balance:
            total_return_pct = ((ending_total_equity / self.config.initial_quote_balance) - 1.0) * 100

        wins = [trade for trade in trades if trade.realized_pnl > 0]
        losses = [trade for trade in trades if trade.realized_pnl < 0]
        win_rate = (len(wins) / completed_trade_count) if completed_trade_count else 0.0
        avg_win = sum(trade.realized_pnl for trade in wins) / len(wins) if wins else 0.0
        avg_loss_abs = abs(sum(trade.realized_pnl for trade in losses) / len(losses)) if losses else 0.0
        avg_win_loss_ratio = (avg_win / avg_loss_abs) if avg_win > 0 and avg_loss_abs > 0 else 0.0
        gross_profit = sum(trade.realized_pnl for trade in wins)
        gross_loss_abs = abs(sum(trade.realized_pnl for trade in losses))
        profit_factor = (gross_profit / gross_loss_abs) if gross_profit > 0 and gross_loss_abs > 0 else 0.0
        avg_hold_bars = sum(trade.hold_bars for trade in trades) / completed_trade_count if completed_trade_count else 0.0
        avg_hold_hours = sum(trade.hold_hours for trade in trades) / completed_trade_count if completed_trade_count else 0.0
        expectancy_per_trade = sum(trade.realized_pnl for trade in trades) / completed_trade_count if completed_trade_count else 0.0
        best_trade_pct = max((trade.return_pct for trade in trades), default=0.0)
        worst_trade_pct = min((trade.return_pct for trade in trades), default=0.0)
        avg_mfe_pct = sum(trade.mfe_pct for trade in trades) / completed_trade_count if completed_trade_count else 0.0
        avg_mae_pct = sum(trade.mae_pct for trade in trades) / completed_trade_count if completed_trade_count else 0.0
        max_consecutive_wins, max_consecutive_losses = self._streaks(trades)

        open_position = portfolio.snapshot.positions.get(self.config.symbol)
        return BacktestSummary(
            symbol=self.config.symbol,
            date_from=summary_date_from,
            date_to=summary_date_to,
            total_return_pct=round(total_return_pct, 6),
            max_drawdown_pct=round(max_drawdown_pct, 6),
            win_rate=round(win_rate, 6),
            avg_win_loss_ratio=round(avg_win_loss_ratio, 6),
            profit_factor=round(profit_factor, 6),
            trade_count=trade_count,
            completed_trade_count=completed_trade_count,
            avg_hold_bars=round(avg_hold_bars, 6),
            avg_hold_hours=round(avg_hold_hours, 6),
            expectancy_per_trade=round(expectancy_per_trade, 6),
            max_consecutive_wins=max_consecutive_wins,
            max_consecutive_losses=max_consecutive_losses,
            best_trade_pct=round(best_trade_pct, 6),
            worst_trade_pct=round(worst_trade_pct, 6),
            avg_mfe_pct=round(avg_mfe_pct, 6),
            avg_mae_pct=round(avg_mae_pct, 6),
            initial_quote_balance=self.config.initial_quote_balance,
            ending_quote_balance=round(portfolio.snapshot.quote_balance, 6),
            ending_total_equity=round(ending_total_equity, 6),
            position_open=open_position is not None,
            open_position_quantity=round(open_position.quantity, 6) if open_position is not None else 0.0,
        )

    def _slice_candles_by_interval(
        self,
        candles_by_interval: Dict[str, List[Candle]],
        indices: Dict[str, int],
        current_close_time: int,
    ) -> Dict[str, List[Candle]]:
        self._advance_interval_indices(candles_by_interval, indices, current_close_time)
        return {
            interval: candles[: indices[interval]]
            for interval, candles in candles_by_interval.items()
        }

    @staticmethod
    def _advance_interval_indices(
        candles_by_interval: Dict[str, List[Candle]],
        indices: Dict[str, int],
        current_close_time: int,
    ) -> None:
        for interval, candles in candles_by_interval.items():
            cursor = indices[interval]
            while cursor < len(candles) and candles[cursor].close_time <= current_close_time:
                cursor += 1
            indices[interval] = cursor

    @staticmethod
    def _apply_drawdown(points: List[EquityPoint]) -> List[EquityPoint]:
        if not points:
            return points
        peak = points[0].total_equity
        enriched: List[EquityPoint] = []
        for point in points:
            peak = max(peak, point.total_equity)
            drawdown_pct = ((peak - point.total_equity) / peak * 100) if peak > 0 else 0.0
            enriched.append(replace(point, drawdown_pct=drawdown_pct))
        return enriched

    @staticmethod
    def _streaks(trades: Sequence[BacktestTrade]) -> tuple[int, int]:
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        for trade in trades:
            if trade.realized_pnl > 0:
                current_wins += 1
                current_losses = 0
            elif trade.realized_pnl < 0:
                current_losses += 1
                current_wins = 0
            else:
                current_wins = 0
                current_losses = 0
            max_wins = max(max_wins, current_wins)
            max_losses = max(max_losses, current_losses)
        return max_wins, max_losses

    def _required_intervals(self) -> List[str]:
        return [
            self.config.main_interval,
            self.config.entry_interval,
            self.config.trend_interval,
        ]

    @staticmethod
    def _reindex_segment(result: BacktestRunResult, segment_index: int) -> BacktestRunResult:
        return BacktestRunResult(
            summary=result.summary,
            trades=[replace(trade, segment_index=segment_index) for trade in result.trades],
            equity_curve=[replace(point, segment_index=segment_index) for point in result.equity_curve],
            dataset_infos=result.dataset_infos,
            config=result.config,
        )
