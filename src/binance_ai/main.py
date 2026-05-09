from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from pathlib import Path

from binance_ai.config import load_settings
from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.data.market_data import MarketDataService
from binance_ai.engine.trading_engine import TradingEngine
from binance_ai.execution.executor import OrderExecutor
from binance_ai.llm.market_analyst import MarketAnalyst
from binance_ai.llm.openai_compat import OpenAICompatibleChatClient
from binance_ai.news.service import NewsService
from binance_ai.paper.portfolio import PaperPortfolio
from binance_ai.reporting.recorder import ReportRecorder
from binance_ai.risk.engine import RiskEngine
from binance_ai.strategy.momentum import MovingAverageMomentumStrategy


def build_engine(output_dir: Path) -> TradingEngine:
    settings = load_settings()
    client = BinanceSpotClient(settings)
    market_data = MarketDataService(client)
    strategy = MovingAverageMomentumStrategy(
        fast_window=settings.fast_window,
        slow_window=settings.slow_window,
    )
    risk = RiskEngine(settings, client)
    news_service = NewsService(
        cache_path=output_dir / "news_cache.json",
        refresh_seconds=settings.news_refresh_seconds,
    )
    market_analyst = None
    if settings.llm_enabled:
        market_analyst = MarketAnalyst(
            client=OpenAICompatibleChatClient(settings),
            model=settings.llm_model,
        )
    paper_portfolio = None
    if settings.dry_run:
        paper_portfolio = PaperPortfolio(
            quote_asset=settings.quote_asset,
            initial_quote_balance=settings.paper_quote_balance,
            state_path=output_dir / "paper_state.json",
        )
    executor = OrderExecutor(settings, client, paper_portfolio=paper_portfolio)
    return TradingEngine(
        settings,
        client,
        market_data,
        strategy,
        risk,
        executor,
        paper_portfolio=paper_portfolio,
        market_analyst=market_analyst,
        news_service=news_service,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Binance AI trading cycle.")
    parser.add_argument("--loop", action="store_true", help="Run continuously.")
    parser.add_argument(
        "--sleep-seconds",
        type=int,
        default=300,
        help="Sleep interval between cycles when running in loop mode.",
    )
    parser.add_argument(
        "--output-dir",
        default="runtime",
        help="Directory for paper state and cycle reports.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    engine = build_engine(output_dir=output_dir)
    recorder = ReportRecorder(output_dir)
    try:
        while True:
            report = engine.run_cycle()
            recorder.record_cycle(report)
            print(json.dumps(asdict(report), ensure_ascii=True, indent=2))
            if not args.loop:
                return
            time.sleep(args.sleep_seconds)
    finally:
        engine.client.close()


if __name__ == "__main__":
    main()
