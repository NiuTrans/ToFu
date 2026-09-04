"""lib/trading_autopilot/_constants.py — Shared constants for the autopilot package."""

__all__ = [
    'CONFIDENCE_THRESHOLD',
    'CORRELATION_WINDOW_DAYS',
    'MAX_REASONING_DEPTH',
    'STRATEGY_EVOLUTION_LOOKBACK',
    'AUTOPILOT_CYCLE_MINUTES',
]

CONFIDENCE_THRESHOLD = 0.6       # Minimum confidence to issue a recommendation
CORRELATION_WINDOW_DAYS = 30     # Window for cross-correlation analysis
MAX_REASONING_DEPTH = 3          # Layers of reasoning chain
STRATEGY_EVOLUTION_LOOKBACK = 90 # Days to look back for strategy performance
AUTOPILOT_CYCLE_MINUTES = 120    # How often the autopilot runs a full cycle
