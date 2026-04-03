"""lib/trading_strategy_engine — Meta-Strategy Engine package.

The brain that sits above backtest_engine, signals, and risk to produce
PRODUCTION-GRADE investment decisions you can actually put money behind.

Sub-modules:
  strategy     — Strategy Protocol/ABC, concrete allocation strategies, registry
  signals      — Multi-timeframe signal confirmation & smoothing
  risk_metrics — Advanced risk-adjusted performance metrics
  ensemble     — Ensemble strategy backtesting
  monte_carlo  — Monte Carlo simulation
  optimization — Rolling walk-forward optimization
  portfolio    — Portfolio construction optimizer (uses Strategy pattern)
  pipeline     — Full production analysis pipeline

NO LLM dependency — pure computation. Every result is deterministic.
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Core modules (always required) ───────────────────────
from . import risk_metrics, signals, strategy  # noqa: E402
from .risk_metrics import *  # noqa: F401,F403
from .signals import *  # noqa: F401,F403
from .strategy import *  # noqa: F401,F403

build_facade(__all__, strategy, signals, risk_metrics)

# ── Non-critical modules (graceful degradation) ─────────
try:
    from . import ensemble  # noqa: E402
    from .ensemble import *  # noqa: F401,F403
    build_facade(__all__, ensemble)
except Exception as _exc:
    _logger.warning('lib.trading_strategy_engine.ensemble failed to load: %s', _exc, exc_info=True)

try:
    from . import monte_carlo  # noqa: E402
    from .monte_carlo import *  # noqa: F401,F403
    build_facade(__all__, monte_carlo)
except Exception as _exc:
    _logger.warning('lib.trading_strategy_engine.monte_carlo failed to load: %s', _exc, exc_info=True)

try:
    from . import optimization  # noqa: E402
    from .optimization import *  # noqa: F401,F403
    build_facade(__all__, optimization)
except Exception as _exc:
    _logger.warning('lib.trading_strategy_engine.optimization failed to load: %s', _exc, exc_info=True)

try:
    from . import portfolio  # noqa: E402
    from .portfolio import *  # noqa: F401,F403
    build_facade(__all__, portfolio)
except Exception as _exc:
    _logger.warning('lib.trading_strategy_engine.portfolio failed to load: %s', _exc, exc_info=True)

try:
    from . import pipeline  # noqa: E402
    from .pipeline import *  # noqa: F401,F403
    build_facade(__all__, pipeline)
except Exception as _exc:
    _logger.warning('lib.trading_strategy_engine.pipeline failed to load: %s', _exc, exc_info=True)
