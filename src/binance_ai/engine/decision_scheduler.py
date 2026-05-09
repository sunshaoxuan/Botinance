from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

from binance_ai.models import SchedulingDiagnostic


@dataclass(frozen=True)
class DecisionState:
    last_decision_candle_close_time: int = 0
    last_decision_price: float = 0.0
    last_decision_timestamp_ms: int = 0


class DecisionScheduler:
    def __init__(self, state_path: Path, price_move_threshold_pct: float) -> None:
        self.state_path = state_path
        self.price_move_threshold_pct = max(0.0, price_move_threshold_pct)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load_state()

    def evaluate(
        self,
        *,
        symbol: str,
        latest_closed_candle_close_time: int,
        current_price: float,
        has_position: bool,
        exit_reason: str | None,
    ) -> SchedulingDiagnostic:
        previous = self._state.get(symbol, DecisionState())
        new_candle_available = (
            previous.last_decision_candle_close_time <= 0
            or latest_closed_candle_close_time > previous.last_decision_candle_close_time
        )
        price_move_pct = 0.0
        if previous.last_decision_price > 0:
            price_move_pct = abs(current_price - previous.last_decision_price) / previous.last_decision_price
        threshold_triggered = (
            previous.last_decision_price > 0
            and self.price_move_threshold_pct > 0
            and price_move_pct >= self.price_move_threshold_pct
        )
        exit_triggered = bool(exit_reason and has_position)

        if previous.last_decision_candle_close_time <= 0:
            should_run_decision = True
            decision_reason = "首次启动，建立基准决策状态"
        elif exit_triggered:
            should_run_decision = True
            decision_reason = f"触发持仓退出条件：{exit_reason}"
        elif new_candle_available:
            should_run_decision = True
            decision_reason = "检测到新的已收盘 K 线"
        elif threshold_triggered:
            should_run_decision = True
            decision_reason = f"价格偏离上次决策价达到阈值 {self.price_move_threshold_pct:.2%}"
        else:
            should_run_decision = False
            decision_reason = "当前无新 K 线且未触发关键阈值事件"

        return SchedulingDiagnostic(
            symbol=symbol,
            should_run_decision=should_run_decision,
            decision_reason=decision_reason,
            latest_closed_candle_close_time=latest_closed_candle_close_time,
            last_decision_candle_close_time=previous.last_decision_candle_close_time,
            current_price=current_price,
            last_decision_price=previous.last_decision_price,
            price_move_pct=price_move_pct,
            new_candle_available=new_candle_available,
            threshold_triggered=threshold_triggered,
            exit_triggered=exit_triggered,
            has_position=has_position,
        )

    def record_decision(
        self,
        *,
        symbol: str,
        latest_closed_candle_close_time: int,
        current_price: float,
        timestamp_ms: int,
    ) -> None:
        self._state[symbol] = DecisionState(
            last_decision_candle_close_time=latest_closed_candle_close_time,
            last_decision_price=current_price,
            last_decision_timestamp_ms=timestamp_ms,
        )

    def save(self) -> None:
        payload = {
            "symbols": {
                symbol: asdict(state)
                for symbol, state in sorted(self._state.items())
            }
        }
        self.state_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    @staticmethod
    def summarize_cycle(diagnostics: Iterable[SchedulingDiagnostic]) -> Tuple[str, str]:
        diagnostics = list(diagnostics)
        if not diagnostics:
            return "DECISION", "当前无交易对，默认视为决策轮。"

        decision_items: List[SchedulingDiagnostic] = [item for item in diagnostics if item.should_run_decision]
        if len(decision_items) == len(diagnostics):
            primary = decision_items[0]
            return "DECISION", f"{primary.symbol}：{primary.decision_reason}"
        if decision_items:
            primary = decision_items[0]
            return "MIXED", f"{primary.symbol} 进入决策，其余交易对维持刷新轮。"
        return "REFRESH", "所有交易对均无新 K 线且未触发关键阈值事件。"

    def _load_state(self) -> Dict[str, DecisionState]:
        if not self.state_path.exists():
            return {}

        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        state: Dict[str, DecisionState] = {}
        for symbol, item in payload.get("symbols", {}).items():
            state[symbol] = DecisionState(
                last_decision_candle_close_time=int(item.get("last_decision_candle_close_time", 0)),
                last_decision_price=float(item.get("last_decision_price", 0.0)),
                last_decision_timestamp_ms=int(item.get("last_decision_timestamp_ms", 0)),
            )
        return state
