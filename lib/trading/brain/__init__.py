"""lib/trading/brain/ — Unified Decision Center (Brain).

The Brain is the SINGLE decision-making entry point. It consolidates:
  - Screening (candidate discovery)
  - Signal computation
  - Risk assessment
  - KPI evaluation
  - Multi-agent debate (bull vs bear)
  - LLM reasoning
  - Strategy evolution
  - Trade order generation

All decision paths flow through `brain.pipeline.run_brain_analysis()`.
No other module should independently generate buy/sell recommendations.

Sub-modules:
  pipeline    — Unified 6-phase analysis pipeline (the core)

Architecture note (2026-04):
  This package is an organizational façade that re-exports symbols from
  ``lib.trading_autopilot``, ``lib.trading_signals``, ``lib.trading_risk``,
  and ``lib.trading.screening``. It allows callers to use a single
  ``from lib.trading.brain import X`` import path.  The pipeline module
  delegates core context gathering to ``lib.trading_autopilot.cycle._gather_context()``
  as the single source of truth.

  See also: ``lib/trading/strategy_interface.py`` for the unified strategy Protocol
  and ``lib/trading/WINRATE_DIAGNOSTIC.md`` for the backtest vs live gap analysis.
"""

from lib.log import get_logger

_logger = get_logger(__name__)

__all__: list[str] = []

# ── Re-export key functions from existing modules ──
# Signals (single source)
try:
    from lib.trading_signals import compute_signal_series, compute_signal_snapshot  # noqa: F401
    __all__.extend(['compute_signal_snapshot', 'compute_signal_series'])
except Exception as _exc:
    _logger.warning('brain: trading_signals failed to load: %s', _exc, exc_info=True)

# Risk
try:
    from lib.trading_risk import (  # noqa: F401
        kelly_fraction,
        risk_parity_weights,
        volatility_target_position,
    )
    __all__.extend(['kelly_fraction', 'volatility_target_position',
                    'risk_parity_weights'])
except Exception as _exc:
    _logger.warning('brain: trading_risk failed to load: %s', _exc, exc_info=True)

# KPI
try:
    from lib.trading_autopilot.kpi import calculate_kpis, pre_backtest_evaluate  # noqa: F401
    __all__.extend(['calculate_kpis', 'pre_backtest_evaluate'])
except Exception as _exc:
    _logger.warning('brain: kpi failed to load: %s', _exc, exc_info=True)

# Debate
try:
    from lib.trading_autopilot.debate import run_bull_bear_debate  # noqa: F401
    __all__.extend(['run_bull_bear_debate'])
except Exception as _exc:
    _logger.warning('brain: debate failed to load: %s', _exc, exc_info=True)

# Reasoning
try:
    from lib.trading_autopilot.reasoning import build_autopilot_prompt, parse_autopilot_result  # noqa: F401
    __all__.extend(['build_autopilot_prompt', 'parse_autopilot_result'])
except Exception as _exc:
    _logger.warning('brain: reasoning failed to load: %s', _exc, exc_info=True)

# Correlation
try:
    from lib.trading_autopilot.correlation import build_correlation_context, correlate_intel_items  # noqa: F401
    __all__.extend(['correlate_intel_items', 'build_correlation_context'])
except Exception as _exc:
    _logger.warning('brain: correlation failed to load: %s', _exc, exc_info=True)

# Strategy evolution
try:
    from lib.trading_autopilot.strategy_evolution import evolve_strategies  # noqa: F401
    __all__.extend(['evolve_strategies'])
except Exception as _exc:
    _logger.warning('brain: strategy_evolution failed to load: %s', _exc, exc_info=True)

# Outcome tracking
try:
    from lib.trading_autopilot.outcome import track_recommendation_outcomes  # noqa: F401
    __all__.extend(['track_recommendation_outcomes'])
except Exception as _exc:
    _logger.warning('brain: outcome failed to load: %s', _exc, exc_info=True)

# Screening
try:
    from lib.trading.screening import (  # noqa: F401
        fetch_asset_ranking,
        run_screening_pipeline,
        score_asset_candidate,
        screen_assets,
        screen_stocks,
        smart_select_assets,
    )
    __all__.extend(['screen_assets', 'screen_stocks', 'smart_select_assets',
                    'fetch_asset_ranking', 'run_screening_pipeline',
                    'score_asset_candidate'])
except Exception as _exc:
    _logger.warning('brain: screening failed to load: %s', _exc, exc_info=True)

# Pipeline (new)
try:
    from .pipeline import build_brain_streaming_body, run_brain_analysis  # noqa: F401
    __all__.extend(['run_brain_analysis', 'build_brain_streaming_body'])
except Exception as _exc:
    _logger.warning('brain: pipeline failed to load: %s', _exc, exc_info=True)
