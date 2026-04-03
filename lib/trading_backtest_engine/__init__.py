"""lib/trading_backtest_engine — Event-Driven Backtesting Engine v2

Professional-grade backtesting package with strict no-future-data leakage,
T+1 order execution, transaction costs, stop-loss / take-profit mechanics,
drawdown circuit breakers, and multiple strategy modes.

Sub-modules
-----------
config       — Default configuration constants
state        — Portfolio state management (BacktestState, StopLossManager, DrawdownProtector)
strategies   — Strategy implementations (StrategyMixin)
reporting    — Metrics computation & reporting
engine       — Core simulation engine (BacktestEngine)
validation   — Walk-forward & multi-period validation
comparison   — Multi-strategy comparison & ranking
analysis     — Bias verification, cost analysis, walk-forward proxy
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Core modules (always required) ───────────────────────
from . import config, engine, reporting, state, strategies  # noqa: E402
from .config import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
from .reporting import *  # noqa: F401,F403
from .state import *  # noqa: F401,F403
from .strategies import *  # noqa: F401,F403

build_facade(__all__, config, state, strategies, reporting, engine)

# ── Non-critical modules (graceful degradation) ─────────
try:
    from . import validation  # noqa: E402
    from .validation import *  # noqa: F401,F403
    build_facade(__all__, validation)
except Exception as _exc:
    _logger.warning('lib.trading_backtest_engine.validation failed to load: %s', _exc, exc_info=True)

try:
    from . import comparison  # noqa: E402
    from .comparison import *  # noqa: F401,F403
    build_facade(__all__, comparison)
except Exception as _exc:
    _logger.warning('lib.trading_backtest_engine.comparison failed to load: %s', _exc, exc_info=True)

try:
    from . import analysis  # noqa: E402
    from .analysis import *  # noqa: F401,F403
    build_facade(__all__, analysis)
except Exception as _exc:
    _logger.warning('lib.trading_backtest_engine.analysis failed to load: %s', _exc, exc_info=True)

try:
    from . import intel_backtest  # noqa: E402
    from .intel_backtest import *  # noqa: F401,F403
    build_facade(__all__, intel_backtest)
except Exception as _exc:
    _logger.warning('lib.trading_backtest_engine.intel_backtest failed to load: %s', _exc, exc_info=True)
