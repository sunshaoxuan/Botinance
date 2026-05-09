from __future__ import annotations

import json
from statistics import mean
from typing import Dict, List, Sequence

from binance_ai.llm.openai_compat import OpenAICompatibleChatClient
from binance_ai.models import Candle, LlmAnalysis, NewsItem, TradeSignal


class MarketAnalyst:
    def __init__(self, client: OpenAICompatibleChatClient, model: str) -> None:
        self.client = client
        self.model = model

    def analyze(
        self,
        quote_asset: str,
        kline_interval: str,
        market_snapshots: List[Dict[str, object]],
        news_evidence: List[NewsItem],
    ) -> LlmAnalysis:
        if not market_snapshots:
            return LlmAnalysis(
                status="DISABLED",
                provider="openai_compat",
                model=self.model,
                regime_cn="无数据",
                summary_cn="当前没有可分析的市场快照。",
                action_bias_cn="观望",
                confidence=0.0,
                risk_note_cn="暂无风险结论。",
            )

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个谨慎的量化交易分析助手。"
                    "你只能基于给定的市场快照和新闻证据做中文分析，不能编造外部信息。"
                    "请输出严格 JSON，字段必须是"
                    " regime_cn, summary_cn, action_bias_cn, confidence, risk_note_cn。"
                    "action_bias_cn 只能是 买入偏向、卖出偏向、观望。"
                    "confidence 是 0 到 1 之间的小数。"
                    "summary_cn 和 risk_note_cn 控制在 60 个汉字以内。"
                ),
            },
            {
                "role": "user",
                "content": self._build_prompt(
                    quote_asset=quote_asset,
                    kline_interval=kline_interval,
                    market_snapshots=market_snapshots,
                    news_evidence=news_evidence,
                ),
            },
        ]
        try:
            raw_text = self.client.chat(messages)
            payload = self._parse_json(raw_text)
            return LlmAnalysis(
                status="READY",
                provider="openai_compat",
                model=self.model,
                regime_cn=str(payload.get("regime_cn", "震荡")),
                summary_cn=str(payload.get("summary_cn", "模型未返回摘要。")),
                action_bias_cn=str(payload.get("action_bias_cn", "观望")),
                confidence=float(payload.get("confidence", 0.0)),
                risk_note_cn=str(payload.get("risk_note_cn", "模型未返回风险提示。")),
                raw_text=raw_text,
            )
        except Exception as exc:
            return LlmAnalysis(
                status="ERROR",
                provider="openai_compat",
                model=self.model,
                regime_cn="分析失败",
                summary_cn="大模型分析本轮未成功返回。",
                action_bias_cn="观望",
                confidence=0.0,
                risk_note_cn="暂时退回规则策略，不影响原有交易逻辑。",
                error=str(exc),
            )

    def _build_prompt(
        self,
        quote_asset: str,
        kline_interval: str,
        market_snapshots: List[Dict[str, object]],
        news_evidence: List[NewsItem],
    ) -> str:
        lines = [
            f"计价资产: {quote_asset}",
            f"K线周期: {kline_interval}",
            "请基于下面的交易对快照做中文市场判断：",
        ]
        for item in market_snapshots:
            closes = item.get("recent_closes", [])
            long_window_closes = item.get("long_window_closes", [])
            close_text = ", ".join(f"{value:.4f}" for value in closes)
            long_window_text = ", ".join(f"{value:.4f}" for value in long_window_closes)
            lines.extend(
                [
                    f"交易对: {item['symbol']}",
                    f"最新价格: {item['last_price']}",
                    f"策略动作: {item['signal_action']}",
                    f"策略原因: {item['signal_reason']}",
                    f"策略置信度: {item['signal_confidence']}",
                    f"是否有持仓: {item['has_position']}",
                    f"短周期收盘价(最近12根): [{close_text}]",
                    f"长周期窗口长度: {item['long_window_size']}",
                    f"长周期完整收盘价: [{long_window_text}]",
                    f"快均线: {item['ma_fast']}",
                    f"慢均线: {item['ma_slow']}",
                    f"均线偏离百分比: {item['ma_gap_pct']}",
                    f"24根涨跌幅: {item['change_24_pct']}",
                    f"长周期涨跌幅: {item['change_long_pct']}",
                    f"长周期最高价: {item['high_long']}",
                    f"长周期最低价: {item['low_long']}",
                ]
            )
        lines.append("新闻与公告证据如下：")
        if news_evidence:
            for item in news_evidence[:10]:
                lines.extend(
                    [
                        f"来源: {item.source}",
                        f"分类: {item.category}",
                        f"标题: {item.title}",
                        f"匹配关键词: {', '.join(item.matched_keywords) if item.matched_keywords else '-'}",
                        f"链接: {item.url}",
                    ]
                )
        else:
            lines.append("无相关新闻证据。")
        return "\n".join(lines)

    @staticmethod
    def _parse_json(raw_text: str) -> Dict[str, object]:
        text = raw_text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            for part in parts:
                candidate = part.strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    return json.loads(candidate)
                if candidate.startswith("json"):
                    candidate = candidate[4:].strip()
                    if candidate.startswith("{") and candidate.endswith("}"):
                        return json.loads(candidate)
        return json.loads(text)


def build_market_snapshot(
    symbol: str,
    candles: Sequence[Candle],
    signal: TradeSignal,
    has_position: bool,
    fast_window: int,
    slow_window: int,
) -> Dict[str, object]:
    closes = [candle.close for candle in candles]
    recent_closes = closes[-12:]
    long_window = closes[-slow_window:] if len(closes) >= slow_window else closes[:]
    fast_now = mean(closes[-fast_window:]) if len(closes) >= fast_window else (closes[-1] if closes else 0.0)
    slow_now = mean(closes[-slow_window:]) if len(closes) >= slow_window else (closes[-1] if closes else 0.0)
    latest_price = closes[-1] if closes else 0.0
    price_24_base = closes[-24] if len(closes) >= 24 else closes[0] if closes else 0.0
    long_base = long_window[0] if long_window else 0.0

    def pct_change(current: float, base: float) -> float:
        if not base:
            return 0.0
        return round((current - base) / base * 100, 4)

    return {
        "symbol": symbol,
        "last_price": latest_price,
        "signal_action": signal.action.value,
        "signal_reason": signal.reason,
        "signal_confidence": round(signal.confidence, 4),
        "has_position": has_position,
        "recent_closes": recent_closes,
        "long_window_size": len(long_window),
        "long_window_closes": long_window,
        "ma_fast": round(fast_now, 4),
        "ma_slow": round(slow_now, 4),
        "ma_gap_pct": pct_change(fast_now, slow_now),
        "change_24_pct": pct_change(latest_price, price_24_base),
        "change_long_pct": pct_change(latest_price, long_base),
        "high_long": round(max(long_window), 4) if long_window else 0.0,
        "low_long": round(min(long_window), 4) if long_window else 0.0,
    }
