from __future__ import annotations

import json
from statistics import mean
from typing import Dict, List, Sequence

from binance_ai.models import AiRiskAssessment, Candle, LlmAnalysis, NewsItem, TradeSignal


class MarketAnalyst:
    def __init__(self, client: object, model: str) -> None:
        self.client = client
        self.model = model

    def _provider(self) -> str:
        return str(getattr(self.client, "last_provider", "") or "openai_compat")

    def _model(self) -> str:
        return str(getattr(self.client, "last_model", "") or self.model)

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
                provider=self._provider(),
                model=self._model(),
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
                provider=self._provider(),
                model=self._model(),
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
                provider=self._provider(),
                model=self._model(),
                regime_cn="分析失败",
                summary_cn="大模型分析本轮未成功返回。",
                action_bias_cn="观望",
                confidence=0.0,
                risk_note_cn="暂时退回规则策略，不影响原有交易逻辑。",
                error=str(exc),
            )

    def assess_entry_risk(
        self,
        quote_asset: str,
        kline_interval: str,
        market_snapshots: List[Dict[str, object]],
        news_evidence: List[NewsItem],
    ) -> Dict[str, AiRiskAssessment]:
        if not market_snapshots:
            return {}

        messages = [
            {
                "role": "system",
                "content": (
                    "你是一个量化交易风险闸门，只能做否决或缩仓，不能强制开仓。"
                    "你只能基于给定的市场快照和新闻证据判断是否允许入场。"
                    "请输出严格 JSON，格式必须是"
                    ' {"decisions":[{"symbol":"...","allow_entry":true,"risk_score":0.0,"position_multiplier":1.0,"veto_reason":"..."}]}.'
                    "allow_entry 只能是 true 或 false。"
                    "risk_score 是 0 到 1 之间的小数，越高代表风险越大。"
                    "position_multiplier 只能在 0 到 1 之间。"
                    "veto_reason 用中文，控制在 40 个汉字以内；若 allow_entry 为 true，可以留空字符串。"
                    "你不能让 position_multiplier 大于 1。"
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
            decisions = payload.get("decisions", [])
            assessments: Dict[str, AiRiskAssessment] = {}
            for item in decisions:
                if not isinstance(item, dict):
                    continue
                symbol = str(item.get("symbol", "")).strip().upper()
                if not symbol:
                    continue
                allow_entry = bool(item.get("allow_entry", True))
                risk_score = max(0.0, min(1.0, float(item.get("risk_score", 0.0))))
                position_multiplier = max(0.0, min(1.0, float(item.get("position_multiplier", 1.0))))
                veto_reason = str(item.get("veto_reason", "")).strip()
                assessments[symbol] = AiRiskAssessment(
                    symbol=symbol,
                    status="READY",
                    allow_entry=allow_entry,
                    risk_score=risk_score,
                    position_multiplier=position_multiplier,
                    veto_reason=veto_reason,
                    raw_payload=raw_text,
                )
            for snapshot in market_snapshots:
                symbol = str(snapshot["symbol"]).upper()
                assessments.setdefault(
                    symbol,
                    AiRiskAssessment(
                        symbol=symbol,
                        status="FALLBACK",
                        allow_entry=True,
                        risk_score=0.0,
                        position_multiplier=1.0,
                        veto_reason="",
                        raw_payload=raw_text,
                    ),
                )
            return assessments
        except Exception as exc:
            return {
                str(snapshot["symbol"]).upper(): AiRiskAssessment(
                    symbol=str(snapshot["symbol"]).upper(),
                    status="ERROR",
                    allow_entry=True,
                    risk_score=0.0,
                    position_multiplier=1.0,
                    veto_reason=f"AI 风险闸门异常，回退规则风控：{exc}",
                )
                for snapshot in market_snapshots
            }

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
                    f"策略市场结构: {item.get('signal_regime', '')}",
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
                    f"{item['entry_interval_summary']['interval']} 概要: {item['entry_interval_summary']}",
                    f"{item['trend_interval_summary']['interval']} 概要: {item['trend_interval_summary']}",
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
    candles_by_interval: Dict[str, Sequence[Candle]],
    signal: TradeSignal,
    has_position: bool,
    main_interval: str,
    fast_window: int,
    slow_window: int,
    entry_interval: str,
    entry_fast_window: int,
    entry_slow_window: int,
    trend_interval: str,
    trend_fast_window: int,
    trend_slow_window: int,
) -> Dict[str, object]:
    candles = list(
        candles_by_interval[main_interval]
        if main_interval in candles_by_interval
        else next(iter(candles_by_interval.values()), ())
    )
    closes = [candle.close for candle in candles]
    recent_closes = closes[-12:]
    long_window = closes[-slow_window:] if len(closes) >= slow_window else closes[:]
    main_interval_bars = [
        {
            "symbol": symbol,
            "open_time": candle.open_time,
            "close_time": candle.close_time,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
            "volume": candle.volume,
        }
        for candle in candles[-160:]
    ]
    fast_now = mean(closes[-fast_window:]) if len(closes) >= fast_window else (closes[-1] if closes else 0.0)
    slow_now = mean(closes[-slow_window:]) if len(closes) >= slow_window else (closes[-1] if closes else 0.0)
    latest_price = closes[-1] if closes else 0.0
    price_24_base = closes[-24] if len(closes) >= 24 else closes[0] if closes else 0.0
    long_base = long_window[0] if long_window else 0.0

    def pct_change(current: float, base: float) -> float:
        if not base:
            return 0.0
        return round((current - base) / base * 100, 4)

    def summarize_interval(interval: str, interval_candles: Sequence[Candle], fast: int, slow: int) -> Dict[str, object]:
        interval_closes = [candle.close for candle in interval_candles]
        if len(interval_closes) < slow:
            return {
                "interval": interval,
                "last_price": interval_closes[-1] if interval_closes else 0.0,
                "fast_ma": 0.0,
                "slow_ma": 0.0,
                "state": "insufficient",
                "change_pct": 0.0,
            }
        fast_now = mean(interval_closes[-fast:])
        slow_now = mean(interval_closes[-slow:])
        state = "above" if fast_now > slow_now else "below" if fast_now < slow_now else "flat"
        return {
            "interval": interval,
            "last_price": interval_closes[-1],
            "fast_ma": round(fast_now, 4),
            "slow_ma": round(slow_now, 4),
            "state": state,
            "change_pct": pct_change(interval_closes[-1], interval_closes[0]),
        }

    entry_summary = summarize_interval(
        entry_interval,
        candles_by_interval.get(entry_interval, ()),
        entry_fast_window,
        entry_slow_window,
    )
    trend_summary = summarize_interval(
        trend_interval,
        candles_by_interval.get(trend_interval, ()),
        trend_fast_window,
        trend_slow_window,
    )

    return {
        "symbol": symbol,
        "last_price": latest_price,
        "signal_action": signal.action.value,
        "signal_reason": signal.reason,
        "signal_confidence": round(signal.confidence, 4),
        "signal_regime": signal.regime,
        "has_position": has_position,
        "recent_closes": recent_closes,
        "main_interval_bars": main_interval_bars,
        "long_window_size": len(long_window),
        "long_window_closes": long_window,
        "ma_fast": round(fast_now, 4),
        "ma_slow": round(slow_now, 4),
        "ma_gap_pct": pct_change(fast_now, slow_now),
        "change_24_pct": pct_change(latest_price, price_24_base),
        "change_long_pct": pct_change(latest_price, long_base),
        "high_long": round(max(long_window), 4) if long_window else 0.0,
        "low_long": round(min(long_window), 4) if long_window else 0.0,
        "entry_interval_summary": entry_summary,
        "trend_interval_summary": trend_summary,
    }
