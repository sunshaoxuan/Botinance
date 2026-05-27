from __future__ import annotations

import unittest

from binance_ai.config import Settings
from binance_ai.storage.runtime import NullRuntimeStore, SafeRuntimeStore, month_suffix


def _settings() -> Settings:
    return Settings(
        api_key="",
        api_secret="",
        base_url="https://api.binance.com",
        recv_window=5000,
        trading_symbols=["XRPJPY"],
        max_active_symbols=1,
        quote_asset="JPY",
        kline_interval="1m",
        kline_limit=10,
        fast_window=3,
        slow_window=6,
        risk_per_trade=0.1,
        min_order_notional=25,
        trading_fee_rate=0.001,
        paper_quote_balance=1000,
        dry_run=True,
        llm_base_url="",
        llm_api_key="",
        llm_model="",
        llm_timeout_seconds=20,
        news_refresh_seconds=120,
        stop_loss_pct=0.01,
        take_profit_pct=0.02,
        trailing_stop_pct=0.0075,
        max_hold_bars=24,
        db_enabled=False,
        db_write_mode="file",
        db_read_mode="file",
    )


class StorageRuntimeTests(unittest.TestCase):
    def test_month_suffix_uses_utc_month(self) -> None:
        self.assertEqual(month_suffix(1714521600000), "2024_05")

    def test_safe_runtime_store_degrades_without_postgres(self) -> None:
        store = SafeRuntimeStore(None, _settings())
        store.write_cycle_report({"timestamp_ms": 1714521600000})
        store.write_portfolio_snapshot({"fills": []})
        status = store.storage_status()
        self.assertFalse(status["ok"])
        self.assertEqual(status["write_mode"], "file")

    def test_null_runtime_store_reports_disabled(self) -> None:
        status = NullRuntimeStore().storage_status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["ok"])


if __name__ == "__main__":
    unittest.main()
