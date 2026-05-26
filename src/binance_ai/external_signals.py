from __future__ import annotations

import time
from dataclasses import replace
from typing import Any, Dict, Iterable, List, Tuple

from binance_ai.config import Settings
from binance_ai.connectors.external_public import BinanceFuturesPublicClient, BybitPublicClient, OkxPublicClient
from binance_ai.models import ExternalConsensus, ExternalSignalSnapshot, MarketSignalVote, ScenarioDecision


SOURCE_WEIGHTS = {"binance_futures": 0.40, "okx": 0.30, "bybit": 0.30}
BULLISH_SCENARIOS = {"UPTREND_PROBE_ENTRY", "UPTREND_PULLBACK_ENTRY", "UPTREND_HOLD_EXPANSION", "RECOVERY_AFTER_DROP"}
BUY_SCENARIOS = BULLISH_SCENARIOS | {"RANGE_MARKET_MAKING"}


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first_data(payload: Dict[str, Any]) -> Dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else {}
    return {}


def _result_list(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = payload.get("result") if isinstance(payload, dict) else None
    rows = result.get("list") if isinstance(result, dict) else None
    return [item for item in rows if isinstance(item, dict)] if isinstance(rows, list) else []


class ExternalMarketSignalEngine:
    def __init__(
        self,
        settings: Settings,
        *,
        binance_client: BinanceFuturesPublicClient | None = None,
        okx_client: OkxPublicClient | None = None,
        bybit_client: BybitPublicClient | None = None,
    ) -> None:
        self.settings = settings
        timeout_seconds = max(1, int(settings.external_signal_timeout_seconds))
        self.clients = {
            "binance_futures": binance_client or BinanceFuturesPublicClient(timeout_seconds=timeout_seconds),
            "okx": okx_client or OkxPublicClient(timeout_seconds=timeout_seconds),
            "bybit": bybit_client or BybitPublicClient(timeout_seconds=timeout_seconds),
        }
        self._cache: Dict[str, Tuple[int, ExternalConsensus]] = {}

    def evaluate(
        self,
        *,
        symbol: str,
        price: float,
        local_scenario: ScenarioDecision,
        timestamp_ms: int,
    ) -> Tuple[ExternalConsensus, List[ExternalSignalSnapshot], List[MarketSignalVote], ScenarioDecision]:
        cache_key = symbol.upper()
        cached = self._cache.get(cache_key)
        refresh_ms = max(1, self.settings.external_signal_refresh_seconds) * 1000
        if cached and timestamp_ms - cached[0] < refresh_ms:
            consensus = cached[1]
            blended = self._blend(local_scenario, consensus)
            return consensus, list(consensus.snapshots), list(consensus.votes), blended

        snapshots = [self._fetch_source(source, symbol, timestamp_ms) for source in self._sources()]
        votes = [self._vote(snapshot, price) for snapshot in snapshots]
        consensus = self._build_consensus(symbol=symbol, snapshots=snapshots, votes=votes)
        blended = self._blend(local_scenario, consensus)
        self._cache[cache_key] = (timestamp_ms, consensus)
        return consensus, snapshots, votes, blended

    def _sources(self) -> List[str]:
        sources = [item.strip().lower() for item in self.settings.external_signal_sources.split(",") if item.strip()]
        return [source for source in sources if source in self.clients]

    def _external_symbol(self, source: str, local_symbol: str) -> str:
        normalized = local_symbol.upper().replace("/", "")
        if normalized == "XRPJPY":
            if source == "binance_futures":
                return self.settings.external_symbol_binance_futures_xrpjpy
            if source == "okx":
                return self.settings.external_symbol_okx_xrpjpy
            if source == "bybit":
                return self.settings.external_symbol_bybit_xrpjpy
        return normalized

    def _fetch_source(self, source: str, local_symbol: str, timestamp_ms: int) -> ExternalSignalSnapshot:
        started = time.monotonic()
        symbol = self._external_symbol(source, local_symbol)
        try:
            if source == "binance_futures":
                return self._fetch_binance(symbol, timestamp_ms, started)
            if source == "okx":
                return self._fetch_okx(symbol, timestamp_ms, started)
            if source == "bybit":
                return self._fetch_bybit(symbol, timestamp_ms, started)
            raise RuntimeError("unsupported_source")
        except Exception as exc:
            return ExternalSignalSnapshot(
                source=source,
                symbol=symbol,
                fetched_at_ms=timestamp_ms,
                source_latency_ms=int((time.monotonic() - started) * 1000),
                stale=True,
                error=str(exc)[:180],
            )

    def _fetch_binance(self, symbol: str, timestamp_ms: int, started: float) -> ExternalSignalSnapshot:
        client: BinanceFuturesPublicClient = self.clients["binance_futures"]
        premium = client.premium_index(symbol)
        oi = client.open_interest(symbol)
        oi_hist = client.open_interest_history(symbol)
        ls_rows = client.long_short_ratio(symbol)
        taker_rows = client.taker_buy_sell_ratio(symbol)
        mark = _float(premium.get("markPrice"))
        index = _float(premium.get("indexPrice"))
        oi_value = _float(oi.get("openInterest"))
        oi_change = self._change_pct([_float(row.get("sumOpenInterest")) for row in oi_hist])
        taker = self._latest_ratio(taker_rows, "buySellRatio")
        return ExternalSignalSnapshot(
            source="binance_futures",
            symbol=symbol,
            fetched_at_ms=timestamp_ms,
            mark_price=mark,
            index_price=index,
            last_price=mark,
            open_interest=oi_value,
            open_interest_change_pct=oi_change,
            funding_rate=_float(premium.get("lastFundingRate")),
            long_short_ratio=self._latest_ratio(ls_rows, "longShortRatio"),
            taker_buy_sell_ratio=taker,
            price_change_pct=self._basis_pct(mark, index),
            source_latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _fetch_okx(self, symbol: str, timestamp_ms: int, started: float) -> ExternalSignalSnapshot:
        client: OkxPublicClient = self.clients["okx"]
        mark_row = _first_data(client.mark_price(symbol))
        oi_row = _first_data(client.open_interest(symbol))
        funding_row = _first_data(client.funding_rate(symbol))
        ls_rows = client.long_short_ratio().get("data", [])
        taker_rows = client.taker_volume().get("data", [])
        mark = _float(mark_row.get("markPx"))
        index = _float(mark_row.get("idxPx"), mark)
        return ExternalSignalSnapshot(
            source="okx",
            symbol=symbol,
            fetched_at_ms=timestamp_ms,
            mark_price=mark,
            index_price=index,
            last_price=mark,
            open_interest=_float(oi_row.get("oi")),
            open_interest_change_pct=0.0,
            funding_rate=_float(funding_row.get("fundingRate")),
            long_short_ratio=self._okx_latest_ratio(ls_rows),
            taker_buy_sell_ratio=self._okx_taker_ratio(taker_rows),
            price_change_pct=self._basis_pct(mark, index),
            source_latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _fetch_bybit(self, symbol: str, timestamp_ms: int, started: float) -> ExternalSignalSnapshot:
        client: BybitPublicClient = self.clients["bybit"]
        ticker_rows = _result_list(client.ticker(symbol))
        ticker = ticker_rows[0] if ticker_rows else {}
        oi_rows = _result_list(client.open_interest(symbol))
        funding_rows = _result_list(client.funding_history(symbol))
        ratio_rows = _result_list(client.long_short_ratio(symbol))
        mark = _float(ticker.get("markPrice"))
        index = _float(ticker.get("indexPrice"), mark)
        return ExternalSignalSnapshot(
            source="bybit",
            symbol=symbol,
            fetched_at_ms=timestamp_ms,
            mark_price=mark,
            index_price=index,
            last_price=_float(ticker.get("lastPrice"), mark),
            open_interest=_float(oi_rows[0].get("openInterest")) if oi_rows else _float(ticker.get("openInterest")),
            open_interest_change_pct=self._change_pct([_float(row.get("openInterest")) for row in reversed(oi_rows[:2])]),
            funding_rate=_float(funding_rows[0].get("fundingRate")) if funding_rows else _float(ticker.get("fundingRate")),
            long_short_ratio=self._latest_ratio(ratio_rows, "buyRatio") / max(self._latest_ratio(ratio_rows, "sellRatio"), 1e-9),
            taker_buy_sell_ratio=0.0,
            price_change_pct=self._basis_pct(mark, index),
            source_latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _vote(self, snapshot: ExternalSignalSnapshot, local_price: float) -> MarketSignalVote:
        if snapshot.stale or snapshot.error:
            return MarketSignalVote(
                source=snapshot.source,
                symbol=snapshot.symbol,
                direction_vote="NEUTRAL",
                confidence=0.0,
                stale=True,
                risk_flags=["stale_or_unavailable"],
                source_latency_ms=snapshot.source_latency_ms,
                reason_cn=f"{snapshot.source} 数据不可用，已从票选权重中剔除",
            )
        reference_price = snapshot.mark_price or snapshot.last_price or local_price
        price_delta = self._basis_pct(reference_price, snapshot.index_price or local_price)
        oi_up = snapshot.open_interest_change_pct > 0.001
        long_bias = snapshot.long_short_ratio >= 1.08
        short_bias = 0 < snapshot.long_short_ratio <= 0.92
        taker_buy = snapshot.taker_buy_sell_ratio >= 1.05
        taker_sell = 0 < snapshot.taker_buy_sell_ratio <= 0.95
        funding_hot = abs(snapshot.funding_rate) >= 0.0015
        bullish_points = int(price_delta > 0.0008) + int(oi_up) + int(long_bias) + int(taker_buy)
        bearish_points = int(price_delta < -0.0008) + int(oi_up) + int(short_bias) + int(taker_sell)
        risk_flags: List[str] = []
        if funding_hot:
            risk_flags.append("funding_extreme")
        if oi_up and funding_hot and ((price_delta < -0.0008 and snapshot.funding_rate > 0) or (price_delta > 0.0008 and snapshot.funding_rate < 0)):
            risk_flags.append("crowded_reversal")
        direction = "NEUTRAL"
        if risk_flags and "crowded_reversal" in risk_flags:
            direction = "RISK_OFF"
        elif bullish_points >= 3 and bullish_points > bearish_points:
            direction = "BULLISH"
        elif bearish_points >= 3 and bearish_points > bullish_points:
            direction = "BEARISH"
        confidence = min(1.0, max(bullish_points, bearish_points) / 4.0 + (0.10 if oi_up else 0.0))
        reason = self._vote_reason(direction, bullish_points, bearish_points, snapshot, risk_flags)
        return MarketSignalVote(
            source=snapshot.source,
            symbol=snapshot.symbol,
            direction_vote=direction,
            confidence=confidence,
            risk_flags=risk_flags,
            source_latency_ms=snapshot.source_latency_ms,
            reason_cn=reason,
            score_breakdown={
                "price_deviation_pct": price_delta,
                "oi_change_pct": snapshot.open_interest_change_pct,
                "funding_rate": snapshot.funding_rate,
                "long_short_ratio": snapshot.long_short_ratio,
                "taker_buy_sell_ratio": snapshot.taker_buy_sell_ratio,
                "bullish_points": float(bullish_points),
                "bearish_points": float(bearish_points),
            },
        )

    def _build_consensus(
        self,
        *,
        symbol: str,
        snapshots: List[ExternalSignalSnapshot],
        votes: List[MarketSignalVote],
    ) -> ExternalConsensus:
        available_votes = [vote for vote in votes if not vote.stale]
        required = max(1, self.settings.external_signal_min_sources)
        risk_flags = sorted({flag for vote in votes for flag in vote.risk_flags})
        health = {
            "sources": [{"source": vote.source, "stale": vote.stale, "latency_ms": vote.source_latency_ms} for vote in votes],
            "available_sources": len(available_votes),
            "required_sources": required,
        }
        if len(available_votes) < required:
            return ExternalConsensus(
                symbol=symbol,
                direction_vote="NEUTRAL",
                confidence=0.0,
                local_weight=self.settings.external_signal_local_weight,
                external_weight=self.settings.external_signal_external_weight,
                available_sources=len(available_votes),
                required_sources=required,
                reason_cn="外部可用数据源不足，沿用本地场景",
                risk_flags=risk_flags,
                votes=votes,
                snapshots=snapshots,
                health=health,
            )
        weight_sum = sum(SOURCE_WEIGHTS.get(vote.source, 0.0) for vote in available_votes) or 1.0
        bullish = 0.0
        bearish = 0.0
        risk = 0.0
        confidence = 0.0
        for vote in available_votes:
            weight = SOURCE_WEIGHTS.get(vote.source, 0.0) / weight_sum
            confidence += weight * vote.confidence
            if vote.direction_vote == "BULLISH":
                bullish += weight * vote.confidence
            elif vote.direction_vote == "BEARISH":
                bearish += weight * vote.confidence
            elif vote.direction_vote == "RISK_OFF":
                risk += weight * max(vote.confidence, 0.65)
        direction = "NEUTRAL"
        if self.settings.external_signal_can_trigger_risk_off and risk >= 0.45:
            direction = "RISK_OFF"
        elif bullish >= bearish + 0.15 and bullish >= 0.35:
            direction = "BULLISH"
        elif bearish >= bullish + 0.15 and bearish >= 0.35:
            direction = "BEARISH"
        reason = f"外部票选 {direction}，多头 {bullish:.2f}，空头 {bearish:.2f}，风险 {risk:.2f}"
        return ExternalConsensus(
            symbol=symbol,
            direction_vote=direction,
            confidence=confidence,
            local_weight=self.settings.external_signal_local_weight,
            external_weight=self.settings.external_signal_external_weight,
            available_sources=len(available_votes),
            required_sources=required,
            bullish_score=bullish,
            bearish_score=bearish,
            risk_score=risk,
            reason_cn=reason,
            risk_flags=risk_flags,
            votes=votes,
            snapshots=snapshots,
            health=health,
        )

    def _blend(self, local: ScenarioDecision, consensus: ExternalConsensus) -> ScenarioDecision:
        indicators = dict(local.indicators)
        indicators["external_consensus"] = {
            "direction_vote": consensus.direction_vote,
            "confidence": consensus.confidence,
            "bullish_score": consensus.bullish_score,
            "bearish_score": consensus.bearish_score,
            "risk_score": consensus.risk_score,
        }
        templates = list(local.order_templates)
        templates.append({"name": "external_signal_vote", "direction": consensus.direction_vote, "confidence": consensus.confidence})
        reason = f"{local.reason_cn}；外部共识：{consensus.reason_cn}"
        blocked = list(local.blocked_actions)
        allowed = list(local.allowed_actions)
        buy_fraction = local.buy_size_fraction
        sell_fraction = local.sell_size_fraction
        generate = local.generate_new_orders
        if consensus.direction_vote == "RISK_OFF":
            blocked.append("EXTERNAL_RISK_OFF_BUY")
            allowed = [item for item in allowed if item != "BUY"]
            generate = False if local.scenario_state in BUY_SCENARIOS else generate
            reason += "；普通新买单暂停，已有保护锁和订单过滤器继续生效"
        elif consensus.direction_vote == "BULLISH":
            if local.scenario_state in BUY_SCENARIOS:
                buy_fraction = min(1.0, buy_fraction * 1.2)
                reason += "；外部方向同向，买入模板尺寸上限提高"
            elif self.settings.external_signal_can_change_direction and "BUY" in allowed:
                buy_fraction = min(1.0, buy_fraction * 1.1)
        elif consensus.direction_vote == "BEARISH":
            buy_fraction = max(0.0, buy_fraction * 0.5)
            if local.scenario_state in BUY_SCENARIOS:
                blocked.append("EXTERNAL_BEARISH_REDUCE_BUY")
                reason += "；外部方向反向，买入模板尺寸降低"
            sell_fraction = min(1.0, sell_fraction * 1.1)
        return replace(
            local,
            reason_cn=reason,
            indicators=indicators,
            order_templates=templates,
            allowed_actions=allowed,
            blocked_actions=blocked,
            buy_size_fraction=buy_fraction,
            sell_size_fraction=sell_fraction,
            generate_new_orders=generate,
        )

    @staticmethod
    def _change_pct(values: Iterable[float]) -> float:
        clean = [value for value in values if value > 0]
        if len(clean) < 2 or clean[0] <= 0:
            return 0.0
        return (clean[-1] - clean[0]) / clean[0]

    @staticmethod
    def _basis_pct(price: float, reference: float) -> float:
        if price <= 0 or reference <= 0:
            return 0.0
        return (price - reference) / reference

    @staticmethod
    def _latest_ratio(rows: List[Dict[str, Any]], key: str) -> float:
        if not rows:
            return 0.0
        return _float(rows[-1].get(key))

    @staticmethod
    def _okx_latest_ratio(rows: Any) -> float:
        if not isinstance(rows, list) or not rows:
            return 0.0
        row = rows[-1]
        if isinstance(row, list) and len(row) >= 2:
            return _float(row[1])
        if isinstance(row, dict):
            return _float(row.get("ratio") or row.get("longShortRatio"))
        return 0.0

    @staticmethod
    def _okx_taker_ratio(rows: Any) -> float:
        if not isinstance(rows, list) or not rows:
            return 0.0
        row = rows[-1]
        if isinstance(row, list) and len(row) >= 3:
            buy = _float(row[1])
            sell = _float(row[2])
            return buy / sell if sell > 0 else 0.0
        if isinstance(row, dict):
            buy = _float(row.get("buyVol"))
            sell = _float(row.get("sellVol"))
            return buy / sell if sell > 0 else 0.0
        return 0.0

    @staticmethod
    def _vote_reason(
        direction: str,
        bullish_points: int,
        bearish_points: int,
        snapshot: ExternalSignalSnapshot,
        risk_flags: List[str],
    ) -> str:
        if direction == "RISK_OFF":
            return f"{snapshot.source} 杠杆拥挤风险，标记 {','.join(risk_flags)}"
        if direction == "BULLISH":
            return f"{snapshot.source} 多头因素 {bullish_points} 项，空头因素 {bearish_points} 项"
        if direction == "BEARISH":
            return f"{snapshot.source} 空头因素 {bearish_points} 项，多头因素 {bullish_points} 项"
        return f"{snapshot.source} 信号分歧或置信度不足"
