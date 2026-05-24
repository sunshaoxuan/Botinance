import unittest

from binance_ai.config import Settings
from binance_ai.external_signals import ExternalMarketSignalEngine
from binance_ai.models import ScenarioDecision


def _settings(**updates):
    base = Settings(
        api_key="",
        api_secret="",
        base_url="https://api.binance.com",
        recv_window=5000,
        trading_symbols=["XRPJPY"],
        max_active_symbols=3,
        quote_asset="JPY",
        kline_interval="1m",
        kline_limit=250,
        fast_window=20,
        slow_window=50,
        risk_per_trade=0.10,
        min_order_notional=50.0,
        trading_fee_rate=0.001,
        paper_quote_balance=10000.0,
        dry_run=True,
        llm_base_url="",
        llm_api_key="",
        llm_model="gpt-5.5",
        llm_timeout_seconds=20,
        news_refresh_seconds=120,
        stop_loss_pct=0.01,
        take_profit_pct=0.02,
        trailing_stop_pct=0.0075,
        max_hold_bars=24,
    )
    if not updates:
        return base
    from dataclasses import replace

    return replace(base, **updates)


def _scenario(**updates):
    payload = dict(
        symbol="XRPJPY",
        scenario_state="UPTREND_PROBE_ENTRY",
        reason_cn="本地上行扩散",
        allowed_actions=["BUY", "SELL"],
        buy_size_fraction=0.25,
        sell_size_fraction=1.0,
    )
    payload.update(updates)
    return ScenarioDecision(**payload)


class FakeBinance:
    def premium_index(self, symbol):
        return {"markPrice": "1.010", "indexPrice": "1.000", "lastFundingRate": "0.0001"}

    def open_interest(self, symbol):
        return {"openInterest": "1000"}

    def open_interest_history(self, symbol, period="5m", limit=2):
        return [{"sumOpenInterest": "950"}, {"sumOpenInterest": "1000"}]

    def long_short_ratio(self, symbol, period="5m", limit=2):
        return [{"longShortRatio": "1.20"}]

    def taker_buy_sell_ratio(self, symbol, period="5m", limit=2):
        return [{"buySellRatio": "1.12"}]


class FakeOkx:
    def mark_price(self, symbol):
        return {"data": [{"markPx": "1.008", "idxPx": "1.000"}]}

    def open_interest(self, symbol):
        return {"data": [{"oi": "1000"}]}

    def funding_rate(self, symbol):
        return {"data": [{"fundingRate": "0.0001"}]}

    def long_short_ratio(self, currency="XRP", period="5m"):
        return {"data": [["1", "1.15"]]}

    def taker_volume(self, currency="XRP", period="5m"):
        return {"data": [["1", "115", "100"]]}


class FakeBybit:
    def ticker(self, symbol):
        return {"result": {"list": [{"markPrice": "1.009", "indexPrice": "1.000", "lastPrice": "1.009", "openInterest": "1000", "fundingRate": "0.0001"}]}}

    def open_interest(self, symbol, interval="5min", limit=2):
        return {"result": {"list": [{"openInterest": "950"}, {"openInterest": "1000"}]}}

    def funding_history(self, symbol, limit=1):
        return {"result": {"list": [{"fundingRate": "0.0001"}]}}

    def long_short_ratio(self, symbol, period="5min", limit=1):
        return {"result": {"list": [{"buyRatio": "0.54", "sellRatio": "0.46"}]}}


class FailingSource:
    def __getattr__(self, name):
        raise RuntimeError("source_down")


class ExternalMarketSignalEngineTests(unittest.TestCase):
    def test_three_sources_bullish_raise_buy_size(self):
        engine = ExternalMarketSignalEngine(
            _settings(),
            binance_client=FakeBinance(),
            okx_client=FakeOkx(),
            bybit_client=FakeBybit(),
        )

        consensus, snapshots, votes, blended = engine.evaluate(
            symbol="XRPJPY",
            price=150.0,
            local_scenario=_scenario(),
            timestamp_ms=1_000_000,
        )

        self.assertEqual(consensus.direction_vote, "BULLISH")
        self.assertEqual(len(snapshots), 3)
        self.assertEqual(len(votes), 3)
        self.assertGreater(blended.buy_size_fraction, 0.25)
        self.assertIn("外部共识", blended.reason_cn)

    def test_missing_source_reweights_available_sources(self):
        engine = ExternalMarketSignalEngine(
            _settings(external_signal_min_sources=2),
            binance_client=FakeBinance(),
            okx_client=FailingSource(),
            bybit_client=FakeBybit(),
        )

        consensus, _, votes, _ = engine.evaluate(
            symbol="XRPJPY",
            price=150.0,
            local_scenario=_scenario(),
            timestamp_ms=1_000_000,
        )

        self.assertEqual(consensus.available_sources, 2)
        self.assertEqual(consensus.direction_vote, "BULLISH")
        self.assertTrue(any(vote.stale for vote in votes))

    def test_risk_off_blocks_new_buy_orders(self):
        class RiskBinance(FakeBinance):
            def premium_index(self, symbol):
                return {"markPrice": "0.990", "indexPrice": "1.000", "lastFundingRate": "0.0020"}

        engine = ExternalMarketSignalEngine(
            _settings(external_signal_min_sources=1),
            binance_client=RiskBinance(),
            okx_client=FailingSource(),
            bybit_client=FailingSource(),
        )

        consensus, _, _, blended = engine.evaluate(
            symbol="XRPJPY",
            price=150.0,
            local_scenario=_scenario(),
            timestamp_ms=1_000_000,
        )

        self.assertEqual(consensus.direction_vote, "RISK_OFF")
        self.assertIn("EXTERNAL_RISK_OFF_BUY", blended.blocked_actions)
        self.assertFalse(blended.generate_new_orders)


if __name__ == "__main__":
    unittest.main()
