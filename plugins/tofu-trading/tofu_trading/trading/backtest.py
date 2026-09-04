"""lib/trading/backtest.py — Backward-compatible re-exports.

Portfolio analytics and cost dilution functions have been moved to
``lib/trading/portfolio_analytics.py``.

The old v1 backtesting functions (backtest_hold, backtest_dca,
backtest_portfolio, analyze_correlation) have been removed — they were
dead code never called from any route or frontend. Use the v2 backtest
engine in ``lib/trading_backtest_engine`` instead.

See also: ``lib/trading/WINRATE_DIAGNOSTIC.md`` for the architecture analysis.
"""

from lib.log import get_logger

# Re-export for backward compatibility (tofu_trading.trading.backtest.* still works)
from tofu_trading.trading.portfolio_analytics import (  # noqa: F401
    calculate_avg_cost_after_add,
    calculate_portfolio_value,
    check_rebalance_alerts,
)

logger = get_logger(__name__)

__all__ = [
    'calculate_portfolio_value',
    'check_rebalance_alerts',
    'calculate_avg_cost_after_add',
]
