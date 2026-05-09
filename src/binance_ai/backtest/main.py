from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from binance_ai.backtest.data_loader import HistoricalDatasetLoader
from binance_ai.backtest.models import BacktestConfig, BacktestManifest
from binance_ai.backtest.reporter import BacktestReporter
from binance_ai.backtest.runner import BacktestRunner
from binance_ai.config import Settings, load_settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.risk.engine import RiskEngine
from binance_ai.strategy.momentum import MovingAverageMomentumStrategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run offline Binance backtest.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from", dest="date_from", required=True)
    parser.add_argument("--to", dest="date_to", required=True)
    parser.add_argument("--output-dir", default="runtime_backtest")
    parser.add_argument("--initial-quote-balance", type=float, default=None)
    parser.add_argument("--walk-forward", action="store_true")
    parser.add_argument("--train-days", type=int, default=90)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=30)
    return parser.parse_args()


def build_backtest_config(args: argparse.Namespace, settings: Settings) -> BacktestConfig:
    return BacktestConfig(
        symbol=args.symbol.strip().upper(),
        quote_asset=settings.quote_asset,
        date_from=args.date_from,
        date_to=args.date_to,
        output_dir=args.output_dir,
        initial_quote_balance=args.initial_quote_balance or settings.paper_quote_balance,
        main_interval=settings.kline_interval,
        entry_interval=settings.mtf_entry_interval,
        trend_interval=settings.mtf_trend_interval,
        fast_window=settings.fast_window,
        slow_window=settings.slow_window,
        entry_fast_window=settings.mtf_entry_fast_window,
        entry_slow_window=settings.mtf_entry_slow_window,
        trend_fast_window=settings.mtf_trend_fast_window,
        trend_slow_window=settings.mtf_trend_slow_window,
        risk_per_trade=settings.risk_per_trade,
        min_order_notional=settings.min_order_notional,
        stop_loss_pct=settings.stop_loss_pct,
        take_profit_pct=settings.take_profit_pct,
        trailing_stop_pct=settings.trailing_stop_pct,
        max_hold_bars=settings.max_hold_bars,
        walk_forward=bool(args.walk_forward),
        train_days=args.train_days,
        test_days=args.test_days,
        step_days=args.step_days,
    )


def main() -> None:
    args = parse_args()
    settings = load_settings()
    config = build_backtest_config(args, settings)
    output_dir = Path(config.output_dir)

    client = BinanceSpotClient(settings)
    try:
        strategy = MovingAverageMomentumStrategy(
            main_interval=config.main_interval,
            fast_window=config.fast_window,
            slow_window=config.slow_window,
            entry_interval=config.entry_interval,
            entry_fast_window=config.entry_fast_window,
            entry_slow_window=config.entry_slow_window,
            trend_interval=config.trend_interval,
            trend_fast_window=config.trend_fast_window,
            trend_slow_window=config.trend_slow_window,
        )
        risk = RiskEngine(settings, client)
        runner = BacktestRunner(config, client, strategy, risk)
        loader = HistoricalDatasetLoader(client, output_dir / "cache")
        reporter = BacktestReporter(output_dir)

        if config.walk_forward:
            baseline, segment_runs, segments = runner.run_walk_forward(loader)
            dataset_infos = list(baseline.dataset_infos)
            for run in segment_runs:
                dataset_infos.extend(run.dataset_infos)
            manifest = BacktestManifest(
                config=config,
                dataset_infos=dataset_infos,
                walk_forward=True,
                segment_count=len(segments),
                baseline_summary=baseline.summary,
                notes=[
                    "walk_forward segments use the train window only for indicator warm-up",
                    "beats_baseline compares segment expectancy_per_trade against the full-sample baseline expectancy_per_trade",
                    "official backtest path disables LLM and news evidence",
                ],
            )
            reporter.write_walk_forward(baseline, segment_runs, segments, manifest)
            print(json.dumps(
                {
                    "mode": "walk_forward",
                    "segment_count": len(segments),
                    "baseline_summary": asdict(baseline.summary),
                    "output_dir": str(output_dir),
                },
                ensure_ascii=True,
                indent=2,
            ))
        else:
            dataset = loader.load(
                symbol=config.symbol,
                intervals=[config.main_interval, config.entry_interval, config.trend_interval],
                date_from=config.date_from,
                date_to=config.date_to,
            )
            result = runner.run(dataset)
            manifest = BacktestManifest(
                config=config,
                dataset_infos=result.dataset_infos,
                walk_forward=False,
                segment_count=0,
                baseline_summary=None,
                notes=[
                    "official backtest path disables LLM and news evidence",
                    "orders fill at the main interval close price with no fee or slippage model",
                ],
            )
            reporter.write_single_run(result, manifest)
            print(json.dumps(
                {
                    "mode": "single",
                    "summary": asdict(result.summary),
                    "output_dir": str(output_dir),
                },
                ensure_ascii=True,
                indent=2,
            ))
    finally:
        client.close()


if __name__ == "__main__":
    main()
