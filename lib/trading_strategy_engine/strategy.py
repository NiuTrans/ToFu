"""lib/trading_strategy_engine/strategy — Strategy Pattern abstractions.

Provides a Protocol-based Strategy pattern for portfolio allocation and
ensemble strategy selection.  Concrete implementations can be registered
at runtime and discovered by name.

Classes:
  AllocationStrategy (Protocol) — interface for portfolio allocation methods
  EqualAllocation, RiskParityAllocation, MinVolAllocation, RiskSignalAllocation
  StrategyRegistry — name → strategy lookup with auto-discovery

Functions:
  get_allocation_strategy — convenience lookup by method name
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = [
    'AllocationStrategy',
    'EqualAllocation',
    'RiskParityAllocation',
    'MinVolAllocation',
    'RiskSignalAllocation',
    'StrategyRegistry',
    'get_allocation_strategy',
]


# ═══════════════════════════════════════════════════════════
#  Protocol / ABC
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class AllocationStrategy(Protocol):
    """Protocol for portfolio allocation strategies.

    Each implementation takes asset analysis data and returns raw weight
    mappings.  Weight constraint enforcement is handled externally.
    """

    name: str

    def compute_weights(
        self,
        codes: list[str],
        asset_analysis: dict[str, dict],
    ) -> dict[str, float]:
        """Return raw (unconstrained) portfolio weights.

        Args:
            codes: list of asset codes to allocate across.
            asset_analysis: {code: {'volatility': float, 'composite_score': float,
                           'mtf_score': float, 'signal': str, ...}}

        Returns:
            {code: raw_weight} — need not sum to 1; will be normalised.
        """
        ...


# ═══════════════════════════════════════════════════════════
#  Concrete Implementations
# ═══════════════════════════════════════════════════════════

class EqualAllocation:
    """Equal-weight across all assets."""

    name = 'equal'

    def compute_weights(self, codes, asset_analysis):
        n = len(codes)
        if n == 0:
            return {}
        return {c: 1.0 / n for c in codes}


class RiskParityAllocation:
    """Inverse-volatility weighting (risk parity)."""

    name = 'risk_parity'

    def compute_weights(self, codes, asset_analysis):
        inv_vols = {}
        for c in codes:
            vol = asset_analysis[c]['volatility']
            inv_vols[c] = 1.0 / max(vol, 0.01)
        total = sum(inv_vols.values())
        if total <= 0:
            return {c: 1.0 / len(codes) for c in codes}
        return {c: iv / total for c, iv in inv_vols.items()}


class MinVolAllocation:
    """Minimize portfolio volatility (simplified inverse-variance)."""

    name = 'min_vol'

    def compute_weights(self, codes, asset_analysis):
        inv_vars = {}
        for c in codes:
            vol = asset_analysis[c]['volatility']
            inv_vars[c] = 1.0 / max(vol ** 2, 0.0001)
        total = sum(inv_vars.values())
        if total <= 0:
            return {c: 1.0 / len(codes) for c in codes}
        return {c: iv / total for c, iv in inv_vars.items()}


class RiskSignalAllocation:
    """Risk-parity base with signal-strength overlay.

    Boosts weights for assets with positive signals and reduces weights
    for assets with strong sell signals.
    """

    name = 'risk_signal'

    def compute_weights(self, codes, asset_analysis):
        # Base: inverse-volatility
        inv_vols = {}
        for c in codes:
            vol = asset_analysis[c]['volatility']
            inv_vols[c] = 1.0 / max(vol, 0.01)
        total_inv = sum(inv_vols.values())
        if total_inv <= 0:
            base_weights = {c: 1.0 / len(codes) for c in codes}
        else:
            base_weights = {c: iv / total_inv for c, iv in inv_vols.items()}

        # Signal adjustment
        adjusted = {}
        for c in codes:
            score = (asset_analysis[c].get('mtf_score', 0)
                     or asset_analysis[c].get('composite_score', 0))
            # Map score (-100..100) → multiplier (0.5..1.5)
            multiplier = 1.0 + score / 200.0
            multiplier = max(0.3, min(1.7, multiplier))
            # Strong sell signal → minimize exposure
            if score < -40:
                multiplier = 0.2
            adjusted[c] = base_weights[c] * multiplier

        total_adj = sum(adjusted.values())
        if total_adj > 0:
            return {c: v / total_adj for c, v in adjusted.items()}
        return base_weights


# ═══════════════════════════════════════════════════════════
#  Registry
# ═══════════════════════════════════════════════════════════

class StrategyRegistry:
    """Name-based lookup for AllocationStrategy implementations.

    Pre-populated with the four built-in strategies; call
    ``registry.register(strategy)`` to add custom ones at runtime.
    """

    def __init__(self):
        self._strategies: dict[str, AllocationStrategy] = {}

    # -- mutation -------------------------------------------------

    def register(self, strategy: AllocationStrategy) -> None:
        """Register a strategy instance, keyed by ``strategy.name``."""
        self._strategies[strategy.name] = strategy

    # -- lookup ---------------------------------------------------

    def get(self, name: str) -> AllocationStrategy | None:
        return self._strategies.get(name)

    def names(self) -> list[str]:
        return list(self._strategies.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._strategies

    def __getitem__(self, name: str) -> AllocationStrategy:
        return self._strategies[name]


# Singleton registry with built-in strategies pre-registered.
_default_registry = StrategyRegistry()
_default_registry.register(EqualAllocation())
_default_registry.register(RiskParityAllocation())
_default_registry.register(MinVolAllocation())
_default_registry.register(RiskSignalAllocation())


def get_allocation_strategy(method: str) -> AllocationStrategy:
    """Look up an allocation strategy by name.

    Falls back to equal-weight if the name is unknown.
    """
    strat = _default_registry.get(method)
    if strat is not None:
        return strat
    return _default_registry.get('equal') or EqualAllocation()
