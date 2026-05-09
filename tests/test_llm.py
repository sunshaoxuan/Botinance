import unittest

from binance_ai.llm.market_analyst import build_market_snapshot
from binance_ai.llm.market_analyst import MarketAnalyst
from binance_ai.llm.openai_compat import OpenAICompatibleChatClient
from binance_ai.models import Candle, SignalAction, TradeSignal


class OpenAICompatibleChatClientTests(unittest.TestCase):
    def test_build_chat_endpoint_from_base_host_v1(self) -> None:
        endpoint = OpenAICompatibleChatClient._build_chat_endpoint("http://example.com:1234/v1")
        self.assertEqual(endpoint, "http://example.com:1234/v1/chat/completions")

    def test_build_chat_endpoint_without_v1_suffix(self) -> None:
        endpoint = OpenAICompatibleChatClient._build_chat_endpoint("http://example.com:1234")
        self.assertEqual(endpoint, "http://example.com:1234/v1/chat/completions")


class MarketAnalystTests(unittest.TestCase):
    def test_parse_json_from_markdown_fence(self) -> None:
        payload = MarketAnalyst._parse_json(
            """```json
{"regime_cn":"震荡","summary_cn":"观望","action_bias_cn":"观望","confidence":0.5,"risk_note_cn":"等待确认"}
```"""
        )
        self.assertEqual(payload["regime_cn"], "震荡")
        self.assertEqual(payload["action_bias_cn"], "观望")

    def test_build_market_snapshot_contains_full_slow_window(self) -> None:
        candles = [
            Candle(
                open_time=index,
                open=float(index),
                high=float(index),
                low=float(index),
                close=float(index),
                volume=1.0,
                close_time=index + 1,
            )
            for index in range(1, 61)
        ]
        snapshot = build_market_snapshot(
            symbol="XRPJPY",
            candles_by_interval={
                "1h": candles,
                "15m": candles[-30:],
                "4h": candles,
            },
            signal=TradeSignal(symbol="XRPJPY", action=SignalAction.HOLD, confidence=0.5, reason="test"),
            has_position=False,
            main_interval="1h",
            fast_window=20,
            slow_window=50,
            entry_interval="15m",
            entry_fast_window=12,
            entry_slow_window=26,
            trend_interval="4h",
            trend_fast_window=20,
            trend_slow_window=50,
        )
        self.assertEqual(snapshot["long_window_size"], 50)
        self.assertEqual(snapshot["long_window_closes"][0], 11.0)
        self.assertEqual(snapshot["long_window_closes"][-1], 60.0)
        self.assertEqual(snapshot["entry_interval_summary"]["interval"], "15m")
        self.assertEqual(snapshot["trend_interval_summary"]["interval"], "4h")

    def test_assess_entry_risk_parses_decision_payload(self) -> None:
        class _ClientStub:
            def chat(self, messages):
                return """{"decisions":[{"symbol":"XRPJPY","allow_entry":false,"risk_score":0.82,"position_multiplier":0.25,"veto_reason":"新闻风险过高"}]}"""

        analyst = MarketAnalyst(client=_ClientStub(), model="gpt-5.5")
        assessment = analyst.assess_entry_risk(
            quote_asset="JPY",
            kline_interval="1h",
            market_snapshots=[
                {
                    "symbol": "XRPJPY",
                    "last_price": 224.0,
                    "signal_action": "BUY",
                    "signal_reason": "bullish_cross",
                    "signal_confidence": 0.8,
                    "has_position": False,
                    "recent_closes": [220.0, 221.0],
                    "long_window_size": 2,
                    "long_window_closes": [220.0, 221.0],
                    "ma_fast": 220.5,
                    "ma_slow": 220.0,
                    "ma_gap_pct": 0.2,
                    "change_24_pct": 1.0,
                    "change_long_pct": 1.0,
                    "high_long": 221.0,
                    "low_long": 220.0,
                    "entry_interval_summary": {
                        "interval": "15m",
                        "last_price": 221.0,
                        "fast_ma": 220.8,
                        "slow_ma": 220.4,
                        "state": "above",
                        "change_pct": 0.5,
                    },
                    "trend_interval_summary": {
                        "interval": "4h",
                        "last_price": 221.0,
                        "fast_ma": 220.7,
                        "slow_ma": 220.2,
                        "state": "above",
                        "change_pct": 1.2,
                    },
                }
            ],
            news_evidence=[],
        )
        self.assertFalse(assessment["XRPJPY"].allow_entry)
        self.assertAlmostEqual(assessment["XRPJPY"].position_multiplier, 0.25)
        self.assertEqual(assessment["XRPJPY"].veto_reason, "新闻风险过高")

    def test_build_market_snapshot_respects_configured_intervals_and_windows(self) -> None:
        main_candles = [
            Candle(
                open_time=index,
                open=float(index),
                high=float(index),
                low=float(index),
                close=float(index),
                volume=1.0,
                close_time=index + 1,
            )
            for index in range(1, 61)
        ]
        entry_candles = [
            Candle(open_time=index, open=value, high=value, low=value, close=value, volume=1.0, close_time=index + 1)
            for index, value in enumerate([1.0, 1.0, 1.0, 10.0, 10.0], start=1)
        ]
        trend_candles = [
            Candle(open_time=index, open=value, high=value, low=value, close=value, volume=1.0, close_time=index + 1)
            for index, value in enumerate([10.0, 9.0, 8.0, 7.0, 6.0], start=1)
        ]
        snapshot = build_market_snapshot(
            symbol="XRPJPY",
            candles_by_interval={
                "30m": main_candles,
                "5m": entry_candles,
                "1d": trend_candles,
            },
            signal=TradeSignal(symbol="XRPJPY", action=SignalAction.HOLD, confidence=0.5, reason="test"),
            has_position=False,
            main_interval="30m",
            fast_window=20,
            slow_window=50,
            entry_interval="5m",
            entry_fast_window=2,
            entry_slow_window=4,
            trend_interval="1d",
            trend_fast_window=2,
            trend_slow_window=4,
        )
        self.assertEqual(snapshot["entry_interval_summary"]["interval"], "5m")
        self.assertEqual(snapshot["entry_interval_summary"]["state"], "above")
        self.assertEqual(snapshot["trend_interval_summary"]["interval"], "1d")
        self.assertEqual(snapshot["trend_interval_summary"]["state"], "below")


if __name__ == "__main__":
    unittest.main()
