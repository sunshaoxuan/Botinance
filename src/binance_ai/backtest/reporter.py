from __future__ import annotations

import csv
import json
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Sequence

from binance_ai.backtest.models import (
    BacktestManifest,
    BacktestRunResult,
    BacktestSummary,
    EquityPoint,
    WalkForwardSegmentResult,
)


class BacktestReporter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def write_single_run(
        self,
        result: BacktestRunResult,
        manifest: BacktestManifest,
    ) -> None:
        self._write_summary(result.summary)
        self._write_trades(result.trades)
        self._write_equity_curve(result.equity_curve)
        self._write_segments([])
        self._write_manifest(manifest)

    def write_walk_forward(
        self,
        baseline: BacktestRunResult,
        segment_runs: Sequence[BacktestRunResult],
        segments: Sequence[WalkForwardSegmentResult],
        manifest: BacktestManifest,
    ) -> None:
        aggregate_summary = self._aggregate_segment_summaries(
            symbol=baseline.summary.symbol,
            date_from=baseline.summary.date_from,
            date_to=baseline.summary.date_to,
            initial_quote_balance=baseline.summary.initial_quote_balance,
            segments=segments,
        )
        aggregate_trades = [trade for result in segment_runs for trade in result.trades]
        aggregate_equity = self._chain_segment_equity(segment_runs, baseline.summary.initial_quote_balance)

        self._write_summary(aggregate_summary)
        self._write_trades(aggregate_trades)
        self._write_equity_curve(aggregate_equity)
        self._write_segments(segments)
        self._write_manifest(manifest)

    def _write_summary(self, summary: BacktestSummary) -> None:
        self._write_json("summary.json", asdict(summary))

    def _write_segments(self, segments: Sequence[WalkForwardSegmentResult]) -> None:
        self._write_json("segments.json", [asdict(segment) for segment in segments])

    def _write_manifest(self, manifest: BacktestManifest) -> None:
        self._write_json("run_manifest.json", asdict(manifest))

    def _write_trades(self, trades: Iterable) -> None:
        path = self.output_dir / "trades.csv"
        rows = [asdict(trade) for trade in trades]
        fieldnames = list(rows[0].keys()) if rows else [
            "segment_index",
            "symbol",
            "side",
            "entry_time_ms",
            "exit_time_ms",
            "entry_price",
            "exit_price",
            "quantity",
            "notional",
            "realized_pnl",
            "return_pct",
            "hold_bars",
            "hold_hours",
            "mfe_pct",
            "mae_pct",
            "exit_reason",
            "entry_signal_reason",
            "exit_signal_reason",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _write_equity_curve(self, equity_curve: Sequence[EquityPoint]) -> None:
        path = self.output_dir / "equity_curve.csv"
        rows = [asdict(point) for point in equity_curve]
        fieldnames = list(rows[0].keys()) if rows else [
            "segment_index",
            "timestamp_ms",
            "close_price",
            "quote_balance",
            "market_value",
            "total_equity",
            "realized_pnl",
            "unrealized_pnl",
            "net_pnl",
            "drawdown_pct",
        ]
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _write_json(self, filename: str, payload) -> None:
        (self.output_dir / filename).write_text(
            json.dumps(payload, ensure_ascii=True, indent=2),
            encoding="utf-8",
        )

    def _aggregate_segment_summaries(
        self,
        *,
        symbol: str,
        date_from: str,
        date_to: str,
        initial_quote_balance: float,
        segments: Sequence[WalkForwardSegmentResult],
    ) -> BacktestSummary:
        if not segments:
            return BacktestSummary(
                symbol=symbol,
                date_from=date_from,
                date_to=date_to,
                total_return_pct=0.0,
                max_drawdown_pct=0.0,
                win_rate=0.0,
                avg_win_loss_ratio=0.0,
                profit_factor=0.0,
                trade_count=0,
                completed_trade_count=0,
                avg_hold_bars=0.0,
                avg_hold_hours=0.0,
                expectancy_per_trade=0.0,
                max_consecutive_wins=0,
                max_consecutive_losses=0,
                best_trade_pct=0.0,
                worst_trade_pct=0.0,
                avg_mfe_pct=0.0,
                avg_mae_pct=0.0,
                initial_quote_balance=initial_quote_balance,
                ending_quote_balance=initial_quote_balance,
                ending_total_equity=initial_quote_balance,
                position_open=False,
                open_position_quantity=0.0,
            )

        completed_trade_count = sum(segment.summary.completed_trade_count for segment in segments)
        weighted = completed_trade_count if completed_trade_count > 0 else len(segments)

        def weight(value_name: str) -> float:
            numerator = 0.0
            denominator = 0.0
            for segment in segments:
                current_weight = segment.summary.completed_trade_count if completed_trade_count > 0 else 1
                numerator += getattr(segment.summary, value_name) * current_weight
                denominator += current_weight
            return numerator / denominator if denominator else 0.0

        compounded = initial_quote_balance
        for segment in segments:
            compounded *= 1 + (segment.summary.total_return_pct / 100.0)
        total_return_pct = ((compounded / initial_quote_balance) - 1.0) * 100 if initial_quote_balance else 0.0

        return BacktestSummary(
            symbol=symbol,
            date_from=date_from,
            date_to=date_to,
            total_return_pct=round(total_return_pct, 6),
            max_drawdown_pct=round(max(segment.summary.max_drawdown_pct for segment in segments), 6),
            win_rate=round(weight("win_rate"), 6),
            avg_win_loss_ratio=round(weight("avg_win_loss_ratio"), 6),
            profit_factor=round(weight("profit_factor"), 6),
            trade_count=sum(segment.summary.trade_count for segment in segments),
            completed_trade_count=completed_trade_count,
            avg_hold_bars=round(weight("avg_hold_bars"), 6),
            avg_hold_hours=round(weight("avg_hold_hours"), 6),
            expectancy_per_trade=round(weight("expectancy_per_trade"), 6),
            max_consecutive_wins=max(segment.summary.max_consecutive_wins for segment in segments),
            max_consecutive_losses=max(segment.summary.max_consecutive_losses for segment in segments),
            best_trade_pct=round(max(segment.summary.best_trade_pct for segment in segments), 6),
            worst_trade_pct=round(min(segment.summary.worst_trade_pct for segment in segments), 6),
            avg_mfe_pct=round(weight("avg_mfe_pct"), 6),
            avg_mae_pct=round(weight("avg_mae_pct"), 6),
            initial_quote_balance=initial_quote_balance,
            ending_quote_balance=round(compounded, 6),
            ending_total_equity=round(compounded, 6),
            position_open=False,
            open_position_quantity=0.0,
        )

    def _chain_segment_equity(
        self,
        segment_runs: Sequence[BacktestRunResult],
        initial_quote_balance: float,
    ) -> List[EquityPoint]:
        chained: List[EquityPoint] = []
        base_equity = initial_quote_balance
        peak = initial_quote_balance
        for run in segment_runs:
            for point in run.equity_curve:
                total_equity = base_equity + point.net_pnl
                peak = max(peak, total_equity)
                drawdown_pct = ((peak - total_equity) / peak * 100) if peak > 0 else 0.0
                chained.append(
                    EquityPoint(
                        segment_index=point.segment_index,
                        timestamp_ms=point.timestamp_ms,
                        close_price=point.close_price,
                        quote_balance=base_equity + (point.quote_balance - run.config.initial_quote_balance),
                        market_value=point.market_value,
                        total_equity=total_equity,
                        realized_pnl=point.realized_pnl,
                        unrealized_pnl=point.unrealized_pnl,
                        net_pnl=total_equity - initial_quote_balance,
                        drawdown_pct=drawdown_pct,
                    )
                )
            if run.equity_curve:
                base_equity = chained[-1].total_equity
        return chained
