"""lib/trading_autopilot/ — Autonomous Trading Analyst Robot (超级分析师) (Package Façade)

This ``__init__`` re-exports every public symbol so that downstream code
can keep doing ``from lib.trading_autopilot import run_autopilot_cycle`` etc.
without knowing which sub-module provides it.

Sub-modules:
  _constants          — shared thresholds & timing constants
  correlation         — intelligence cross-correlation engine
  strategy_evolution  — strategy performance review & self-improvement
  kpi                 — pre-backtest KPI calculator & asset scoring
  reasoning           — LLM mega-prompt builder & result parser
  cycle               — full autopilot cycle runner & streaming body builder
  scheduler           — enable/disable toggle & periodic scheduler tick
  outcome             — recommendation outcome tracker & feedback loop
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Core module (always required) ────────────────────────
from . import _constants  # noqa: E402
from ._constants import *  # noqa: F401,F403

build_facade(__all__, _constants)

# ── Feature modules (graceful degradation) ───────────────
try:
    from . import correlation  # noqa: E402
    from .correlation import *  # noqa: F401,F403
    build_facade(__all__, correlation)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.correlation failed to load: %s', _exc, exc_info=True)

try:
    from . import strategy_evolution  # noqa: E402
    from .strategy_evolution import *  # noqa: F401,F403
    build_facade(__all__, strategy_evolution)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.strategy_evolution failed to load: %s', _exc, exc_info=True)

try:
    from . import kpi  # noqa: E402
    from .kpi import *  # noqa: F401,F403
    build_facade(__all__, kpi)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.kpi failed to load: %s', _exc, exc_info=True)

try:
    from . import debate  # noqa: E402
    from .debate import *  # noqa: F401,F403
    build_facade(__all__, debate)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.debate failed to load: %s', _exc, exc_info=True)

try:
    from . import reasoning  # noqa: E402
    from .reasoning import *  # noqa: F401,F403
    build_facade(__all__, reasoning)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.reasoning failed to load: %s', _exc, exc_info=True)

try:
    from . import cycle  # noqa: E402
    from .cycle import *  # noqa: F401,F403
    build_facade(__all__, cycle)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.cycle failed to load: %s', _exc, exc_info=True)

try:
    from . import scheduler  # noqa: E402
    from .scheduler import *  # noqa: F401,F403
    build_facade(__all__, scheduler)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.scheduler failed to load: %s', _exc, exc_info=True)

try:
    from . import outcome  # noqa: E402
    from .outcome import *  # noqa: F401,F403
    build_facade(__all__, outcome)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.outcome failed to load: %s', _exc, exc_info=True)

try:
    from . import meta_strategy  # noqa: E402
    from .meta_strategy import *  # noqa: F401,F403
    build_facade(__all__, meta_strategy)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.meta_strategy failed to load: %s', _exc, exc_info=True)

try:
    from . import strategy_learner  # noqa: E402
    from .strategy_learner import *  # noqa: F401,F403
    build_facade(__all__, strategy_learner)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.strategy_learner failed to load: %s', _exc, exc_info=True)

try:
    from . import adaptive_decision_engine  # noqa: E402
    from .adaptive_decision_engine import *  # noqa: F401,F403
    build_facade(__all__, adaptive_decision_engine)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.adaptive_decision_engine failed to load: %s', _exc, exc_info=True)

try:
    from . import backtest_learner  # noqa: E402
    from .backtest_learner import *  # noqa: F401,F403
    build_facade(__all__, backtest_learner)
except Exception as _exc:
    _logger.warning('lib.trading_autopilot.backtest_learner failed to load: %s', _exc, exc_info=True)
