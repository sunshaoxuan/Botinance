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
            candles=candles,
            signal=TradeSignal(symbol="XRPJPY", action=SignalAction.HOLD, confidence=0.5, reason="test"),
            has_position=False,
            fast_window=20,
            slow_window=50,
        )
        self.assertEqual(snapshot["long_window_size"], 50)
        self.assertEqual(snapshot["long_window_closes"][0], 11.0)
        self.assertEqual(snapshot["long_window_closes"][-1], 60.0)


if __name__ == "__main__":
    unittest.main()
