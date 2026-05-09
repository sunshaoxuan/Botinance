from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Sequence

from binance_ai.connectors.binance_spot import BinanceSpotClient
from binance_ai.models import Candle
from binance_ai.backtest.models import BacktestDataset, BacktestDatasetInfo


INTERVAL_MS: Dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def parse_date_from(value: str) -> datetime:
    return _parse_datetime(value, end_boundary=False)


def parse_date_to(value: str) -> datetime:
    return _parse_datetime(value, end_boundary=True)


def _parse_datetime(value: str, end_boundary: bool) -> datetime:
    raw = value.strip()
    if not raw:
        raise ValueError("date value cannot be empty")
    if len(raw) == 10:
        parsed_date = date.fromisoformat(raw)
        parsed = datetime.combine(parsed_date, time.min, tzinfo=timezone.utc)
        return parsed + timedelta(days=1) if end_boundary else parsed
    parsed_dt = datetime.fromisoformat(raw)
    if parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    return parsed_dt.astimezone(timezone.utc)


class HistoricalDatasetLoader:
    def __init__(self, client: BinanceSpotClient, cache_dir: Path) -> None:
        self.client = client
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def load(
        self,
        symbol: str,
        intervals: Sequence[str],
        date_from: str,
        date_to: str,
    ) -> BacktestDataset:
        start_dt = parse_date_from(date_from)
        end_dt = parse_date_to(date_to)
        if end_dt <= start_dt:
            raise ValueError("date_to must be later than date_from")

        candles_by_interval: Dict[str, List[Candle]] = {}
        infos: List[BacktestDatasetInfo] = []
        for interval in self._unique_intervals(intervals):
            candles, info = self._load_interval(
                symbol=symbol,
                interval=interval,
                start_ms=int(start_dt.timestamp() * 1000),
                end_ms=int(end_dt.timestamp() * 1000),
                cache_key=f"{symbol}_{interval}_{date_from}_{date_to}",
            )
            candles_by_interval[interval] = candles
            infos.append(info)
        return BacktestDataset(candles_by_interval=candles_by_interval, infos=infos)

    def _load_interval(
        self,
        *,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        cache_key: str,
    ) -> tuple[List[Candle], BacktestDatasetInfo]:
        cache_file = self.cache_dir / f"{cache_key.replace(':', '-').replace('/', '-')}.json"
        if cache_file.exists():
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            candles = [Candle(**item) for item in payload]
            return candles, BacktestDatasetInfo(
                symbol=symbol,
                interval=interval,
                candle_count=len(candles),
                cache_key=cache_key,
                cache_hit=True,
                path=str(cache_file),
            )

        candles = self._fetch_interval(symbol, interval, start_ms, end_ms)
        cache_file.write_text(
            json.dumps([asdict(candle) for candle in candles], ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
        return candles, BacktestDatasetInfo(
            symbol=symbol,
            interval=interval,
            candle_count=len(candles),
            cache_key=cache_key,
            cache_hit=False,
            path=str(cache_file),
        )

    def _fetch_interval(self, symbol: str, interval: str, start_ms: int, end_ms: int) -> List[Candle]:
        if interval not in INTERVAL_MS:
            raise ValueError(f"Unsupported interval for backtest loader: {interval}")

        current_start = start_ms
        rows: List[Candle] = []
        seen_open_times = set()
        while current_start < end_ms:
            batch = self.client.get_klines_range(
                symbol=symbol,
                interval=interval,
                start_time_ms=current_start,
                end_time_ms=end_ms - 1,
                limit=1000,
            )
            if not batch:
                break
            for candle in batch:
                if candle.open_time in seen_open_times:
                    continue
                seen_open_times.add(candle.open_time)
                rows.append(candle)
            if len(batch) < 1000 or batch[-1].close_time >= end_ms - 1:
                break
            next_start = batch[-1].close_time + 1
            if next_start <= current_start:
                next_start = batch[-1].open_time + INTERVAL_MS[interval]
            current_start = next_start
        return [candle for candle in rows if start_ms <= candle.open_time < end_ms]

    @staticmethod
    def _unique_intervals(intervals: Iterable[str]) -> List[str]:
        unique: List[str] = []
        seen = set()
        for interval in intervals:
            normalized = interval.strip()
            if not normalized or normalized in seen:
                continue
            unique.append(normalized)
            seen.add(normalized)
        return unique
