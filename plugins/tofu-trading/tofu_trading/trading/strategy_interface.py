"""lib/trading/strategy_interface.py — Unified Strategy Interface.

Defines the ``TradingStrategy`` Protocol that both the backtest engine's
hardcoded strategies AND the live autopilot's LLM-based decisions should
implement.  This is the bridge that closes the gap between:

  - ``lib/trading_backtest_engine/strategies.py`` (deterministic quant)
  - ``lib/trading_autopilot/cycle.py`` (LLM-based reasoning)
  - ``lib/trading/llm_simulator.py`` (LLM simulation)

Usage::

    from tofu_trading.trading.strategy_interface import TradingStrategy, TradeOrder, SignalContext

    class MyQuantStrategy:
        name = 'mean_reversion'
        description = 'Bollinger Band + RSI contrarian'

        def make_decisions(self, context: SignalContext) -> list[TradeOrder]:
            if context.signals.get('rsi', {}).get('value', 50) < 25:
                return [TradeOrder(symbol='510300', action='buy', ...)]
            return []

See also:
  - ``lib/trading/WINRATE_DIAGNOSTIC.md`` for the architecture gap analysis
  - ``lib/protocols.py`` for other Protocol definitions in the project
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'TradingStrategy',
    'TradeOrder',
    'SignalContext',
    'StrategyResult',
]


# ═══════════════════════════════════════════════════════════
#  Data Classes
# ═══════════════════════════════════════════════════════════

@dataclass
class TradeOrder:
    """A single trade recommendation from a strategy.

    This is the universal output format for both backtest strategies
    and live LLM-based decisions.

    Attributes:
        symbol: Asset symbol/code (e.g. '510300').
        asset_name: Human-readable name (e.g. '沪深300ETF').
        action: One of 'buy', 'sell', 'hold', 'add', 'reduce'.
        amount: Trade amount in currency (¥). 0 if using fraction.
        fraction: Fraction of position to sell (0.0-1.0). 0 if using amount.
        confidence: Strategy confidence in this trade (0-100).
        reason: Human-readable explanation.
        stop_loss_pct: Stop-loss threshold as percentage (e.g. -8.0).
        take_profit_pct: Take-profit threshold as percentage (e.g. 15.0).
        priority: Execution priority (higher = execute first). Default 0.
        metadata: Strategy-specific metadata dict.
    """

    symbol: str
    asset_name: str = ''
    action: str = 'hold'
    amount: float = 0.0
    fraction: float = 0.0
    confidence: int = 50
    reason: str = ''
    stop_loss_pct: float | None = None
    take_profit_pct: float | None = None
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization and DB storage."""
        return {
            'symbol': self.symbol,
            'asset_name': self.asset_name,
            'action': self.action,
            'amount': self.amount,
            'fraction': self.fraction,
            'confidence': self.confidence,
            'reason': self.reason,
            'stop_loss_pct': self.stop_loss_pct,
            'take_profit_pct': self.take_profit_pct,
            'priority': self.priority,
            'metadata': self.metadata,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TradeOrder:
        """Create from dict (e.g. from LLM JSON output)."""
        return cls(
            symbol=d.get('symbol', ''),
            asset_name=d.get('asset_name', ''),
            action=d.get('action', 'hold'),
            amount=float(d.get('amount', 0) or 0),
            fraction=float(d.get('fraction', 0) or 0),
            confidence=int(d.get('confidence', 50) or 50),
            reason=d.get('reason', ''),
            stop_loss_pct=d.get('stop_loss_pct'),
            take_profit_pct=d.get('take_profit_pct'),
            priority=int(d.get('priority', 0) or 0),
            metadata=d.get('metadata', {}),
        )


@dataclass
class SignalContext:
    """All data a strategy needs to make decisions.

    This is the universal INPUT format. Both backtest and live systems
    should construct this before calling ``make_decisions()``.

    Attributes:
        date: Decision date (ISO format string, e.g. '2026-04-03').
        holdings: List of current holdings dicts with keys:
            symbol, shares, buy_price, buy_date, current_nav, pnl_pct.
        cash: Available cash (¥).
        signals: Dict mapping symbol → signal snapshot from
            ``compute_signal_snapshot()``. Each contains RSI, MACD,
            EMA, Bollinger, composite score, regime.
        intel_summary: Text summary of market intelligence (news, macro).
        market_regime: Current market regime string
            ('bullish', 'bearish', 'sideways', 'volatile').
        strategies_ctx: Active strategy descriptions (text).
        kpi_evaluations: Dict mapping symbol → KPI evaluation dict.
        fee_info: Dict mapping symbol → fee rate info.
        metadata: Additional context (correlations, debate output, etc.).
    """

    date: str = ''
    holdings: list[dict[str, Any]] = field(default_factory=list)
    cash: float = 0.0
    signals: dict[str, dict[str, Any]] = field(default_factory=dict)
    intel_summary: str = ''
    market_regime: str = 'unknown'
    strategies_ctx: str = ''
    kpi_evaluations: dict[str, dict[str, Any]] = field(default_factory=dict)
    fee_info: dict[str, dict[str, Any]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyResult:
    """Result wrapper from a strategy execution.

    Attributes:
        orders: List of trade orders.
        confidence: Overall strategy confidence (0-100).
        market_outlook: One of 'bullish', 'bearish', 'neutral', 'cautious'.
        risk_factors: List of identified risk factor dicts.
        strategy_updates: List of strategy update proposals.
        reasoning: Free-text explanation of decision rationale.
        metadata: Strategy-specific metadata.
    """

    orders: list[TradeOrder] = field(default_factory=list)
    confidence: int = 50
    market_outlook: str = 'neutral'
    risk_factors: list[dict[str, Any]] = field(default_factory=list)
    strategy_updates: list[dict[str, Any]] = field(default_factory=list)
    reasoning: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            'orders': [o.to_dict() for o in self.orders],
            'confidence_score': self.confidence,
            'market_outlook': self.market_outlook,
            'risk_factors': self.risk_factors,
            'strategy_updates': self.strategy_updates,
            'reasoning': self.reasoning,
            'position_recommendations': [o.to_dict() for o in self.orders],
        }


# ═══════════════════════════════════════════════════════════
#  Strategy Protocol
# ═══════════════════════════════════════════════════════════

@runtime_checkable
class TradingStrategy(Protocol):
    """Protocol for unified trading strategies.

    Any class satisfying this protocol can be used by both the backtest engine
    and the live autopilot, ensuring consistent decision logic across both.

    Implementations:
      - Quant strategies (mean_reversion, trend_following, etc.)
        → implement using ``signals`` from ``SignalContext``
      - LLM-based strategies
        → call LLM with context, parse output into ``TradeOrder`` list
      - Hybrid strategies
        → combine quant signals with LLM reasoning

    Example::

        class MeanReversionStrategy:
            name = 'mean_reversion'
            description = 'Buy oversold (RSI<25, below lower BB), sell overbought'

            def make_decisions(self, context):
                orders = []
                for symbol, sig in context.signals.items():
                    rsi = sig.get('rsi', {}).get('value', 50)
                    if rsi < 25:
                        orders.append(TradeOrder(
                            symbol=symbol, action='buy',
                            amount=context.cash * 0.1,
                            confidence=70,
                            reason=f'RSI={rsi:.0f} oversold',
                        ))
                return StrategyResult(orders=orders, confidence=65)
    """

    @property
    def name(self) -> str:
        """Short identifier for the strategy (e.g. 'signal_driven')."""
        ...

    @property
    def description(self) -> str:
        """Human-readable description of what the strategy does."""
        ...

    def make_decisions(self, context: SignalContext) -> StrategyResult:
        """Make trading decisions based on current context.

        This is the core method. Both backtest and live systems call this
        with appropriate ``SignalContext``.

        Args:
            context: All available data for decision making.

        Returns:
            StrategyResult with orders and metadata.
        """
        ...
