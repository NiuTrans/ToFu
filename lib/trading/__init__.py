"""lib/trading/ — Trading Portfolio Advisor v6 (Package Façade)

This ``__init__`` re-exports every public symbol so that downstream code
can keep doing ``from lib.trading import get_latest_price`` etc. without
knowing which sub-module provides it.

Each sub-module defines ``__all__`` to control its public API.
Non-critical sub-modules (intel, backtest) are wrapped in resilient
imports so a broken dependency does not prevent core NAV/info lookups.

Sub-modules (legacy — still functional):
  _common        — shared HTTP session, proxy config, network-state
  nav            — NAV fetching / caching / history
  info           — asset info, search, fee calculation
  strategy_data  — built-in strategy definitions, seeding, performance tracking
  intel          — intelligence CRUD, crawling, back-fill
  backtest       — backtesting (hold / DCA / portfolio) & correlation

Sub-packages (v2 unified architecture):
  radar/         — Radar Engine: 7×24 data acquisition (market, intel, sources, nav, info, alert)
  brain/         — Brain: Unified decision center (screening, signals, debate, reasoning, strategy)
  portfolio/     — Portfolio Manager: holdings, cash, T+1 trade queue, transactions
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Core modules (always required) ───────────────────────
from . import _common, info, nav, strategy_data  # noqa: E402
from ._common import *  # noqa: F401,F403
from .info import *  # noqa: F401,F403
from .nav import *  # noqa: F401,F403
from .strategy_data import *  # noqa: F401,F403

build_facade(__all__, _common, nav, info, strategy_data)

# ── Non-critical modules (graceful degradation) ─────────
try:
    from . import sources  # noqa: E402
    from .sources import *  # noqa: F401,F403
    build_facade(__all__, sources)
except Exception as _exc:
    _logger.warning('lib.trading.sources failed to load — multi-source fetchers disabled: %s', _exc, exc_info=True)

try:
    from . import intel  # noqa: E402
    from .intel import *  # noqa: F401,F403
    build_facade(__all__, intel)
except Exception as _exc:
    _logger.warning('lib.trading.intel failed to load — intelligence crawling disabled: %s', _exc, exc_info=True)

try:
    from . import backtest  # noqa: E402
    from .backtest import *  # noqa: F401,F403
    build_facade(__all__, backtest)
except Exception as _exc:
    _logger.warning('lib.trading.backtest failed to load — backtesting disabled: %s', _exc, exc_info=True)

try:
    from . import market  # noqa: E402
    from .market import *  # noqa: F401,F403
    build_facade(__all__, market)
except Exception as _exc:
    _logger.warning('lib.trading.market failed to load — market data disabled: %s', _exc, exc_info=True)

try:
    from . import screening  # noqa: E402
    from .screening import *  # noqa: F401,F403
    build_facade(__all__, screening)
except Exception as _exc:
    _logger.warning('lib.trading.screening failed to load — ETF/stock screening disabled: %s', _exc, exc_info=True)

try:
    from . import intel_timeline  # noqa: E402
    from .intel_timeline import *  # noqa: F401,F403
    build_facade(__all__, intel_timeline)
except Exception as _exc:
    _logger.warning('lib.trading.intel_timeline failed to load — time-locked intel disabled: %s', _exc, exc_info=True)
