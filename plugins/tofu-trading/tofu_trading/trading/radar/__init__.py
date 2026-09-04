"""lib/trading/radar/ — Radar Engine: 7×24 data acquisition & alert detection.

Consolidates all data-layer modules:
  market   — real-time indices, sectors, breadth, northbound flow
  intel    — intelligence crawling, backfill, context building
  sources  — multi-source news fetchers (Google News, CLS, DDG)
  nav      — NAV fetching & multi-layer caching
  info     — asset info, search, fee calculation
  alert    — breaking event detection & urgency scoring

Architecture note (2026-04):
  This package is an organizational façade that re-exports symbols from
  ``tofu_trading.trading.market``, ``tofu_trading.trading.nav``, ``tofu_trading.trading.info``,
  ``tofu_trading.trading.sources``, and ``tofu_trading.trading.intel``. Callers can use
  ``from tofu_trading.trading.radar import X`` as a unified data-layer import path.
"""

from lib._pkg_utils import build_facade
from lib.log import get_logger

_logger = get_logger(__name__)

__all__: list[str] = []

# ── Re-export from existing modules ─────────────────────
from tofu_trading.trading import market
from tofu_trading.trading.market import *  # noqa: F401,F403

build_facade(__all__, market)

from tofu_trading.trading import nav
from tofu_trading.trading.nav import *  # noqa: F401,F403

build_facade(__all__, nav)

from tofu_trading.trading import info
from tofu_trading.trading.info import *  # noqa: F401,F403

build_facade(__all__, info)

try:
    from tofu_trading.trading import sources
    from tofu_trading.trading.sources import *  # noqa: F401,F403
    build_facade(__all__, sources)
except Exception as _exc:
    _logger.warning('radar.sources failed to load: %s', _exc, exc_info=True)

try:
    from tofu_trading.trading import intel
    from tofu_trading.trading.intel import *  # noqa: F401,F403
    build_facade(__all__, intel)
except Exception as _exc:
    _logger.warning('radar.intel failed to load: %s', _exc, exc_info=True)

# ── New alert module ──
try:
    from . import alert
    from .alert import *  # noqa: F401,F403
    build_facade(__all__, alert)
except Exception as _exc:
    _logger.warning('radar.alert failed to load: %s', _exc, exc_info=True)
