from __future__ import annotations

from dataclasses import dataclass

from binance_ai.config import Settings


@dataclass(frozen=True)
class ProfitabilityGuardResult:
    allowed: bool
    reason: str
    net_edge_pct: float
    required_edge_pct: float
    release_price: float
    buyback_price: float


class TradeProfitabilityGuard:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def required_edge_pct(self) -> float:
        return max(0.0, self.settings.trading_fee_rate * 2.0 + self.settings.min_net_edge_pct)

    def inspect_release(self, release_price: float, expected_buyback_price: float) -> ProfitabilityGuardResult:
        return self._inspect(release_price, expected_buyback_price)

    def inspect_buyback(self, release_price: float, buyback_price: float) -> ProfitabilityGuardResult:
        return self._inspect(release_price, buyback_price)

    def _inspect(self, release_price: float, buyback_price: float) -> ProfitabilityGuardResult:
        if release_price <= 0 or buyback_price <= 0:
            return ProfitabilityGuardResult(False, "profitability_price_missing", 0.0, self.required_edge_pct, release_price, buyback_price)
        net_edge_pct = (release_price - buyback_price) / release_price
        allowed = net_edge_pct >= self.required_edge_pct
        return ProfitabilityGuardResult(
            allowed=allowed,
            reason="profitability_guard_passed" if allowed else "net_edge_too_small",
            net_edge_pct=net_edge_pct,
            required_edge_pct=self.required_edge_pct,
            release_price=release_price,
            buyback_price=buyback_price,
        )
