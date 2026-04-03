"""lib/trading/portfolio/ — Portfolio Manager.

Manages holdings, cash, trade execution, T+1 queue, and transaction history.

Re-exports key functions from existing modules for backward compatibility.
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# Holdings + cash from nav/info/backtest
from lib.trading.info import (  # noqa: F401
    calc_buy_fee,
    calc_sell_fee,
    fetch_asset_info,
    fetch_trading_fees,
    search_asset,
)
from lib.trading.nav import _prewarm_price_cache, get_latest_price, update_nav_cache  # noqa: F401

__all__.extend([
    'get_latest_price', 'update_nav_cache', '_prewarm_price_cache',
    'fetch_asset_info', 'search_asset', 'fetch_trading_fees',
    'calc_buy_fee', 'calc_sell_fee',
])

# Portfolio analytics from backtest module
try:
    from lib.trading.backtest import (  # noqa: F401
        calculate_portfolio_value,
        check_rebalance_alerts,
    )
    __all__.extend(['calculate_portfolio_value', 'check_rebalance_alerts'])
except Exception as _exc:
    _logger.warning('portfolio: backtest analytics failed to load: %s', _exc, exc_info=True)

# Morning orders queue
try:
    from .trade_queue import (  # noqa: F401
        generate_morning_summary,
        get_morning_orders,
    )
    __all__.extend(['get_morning_orders', 'generate_morning_summary'])
except Exception as _exc:
    _logger.warning('portfolio: trade_queue failed to load: %s', _exc, exc_info=True)
