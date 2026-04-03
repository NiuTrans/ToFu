"""lib/trading/radar/ — Radar Engine: 7×24 data acquisition & alert detection.

Consolidates all data-layer modules:
  market   — real-time indices, sectors, breadth, northbound flow
  intel    — intelligence crawling, backfill, context building
  sources  — multi-source news fetchers (Google News, CLS, DDG)
  nav      — NAV fetching & multi-layer caching
  info     — asset info, search, fee calculation
  alert    — breaking event detection & urgency scoring
"""

import logging

from lib._pkg_utils import build_facade

_logger = logging.getLogger(__name__)

__all__: list[str] = []

# ── Re-export from existing modules ─────────────────────
from lib.trading import market
from lib.trading.market import *  # noqa: F401,F403

build_facade(__all__, market)

from lib.trading import nav
from lib.trading.nav import *  # noqa: F401,F403

build_facade(__all__, nav)

from lib.trading import info
from lib.trading.info import *  # noqa: F401,F403

build_facade(__all__, info)

try:
    from lib.trading import sources
    from lib.trading.sources import *  # noqa: F401,F403
    build_facade(__all__, sources)
except Exception as _exc:
    _logger.warning('radar.sources failed to load: %s', _exc, exc_info=True)

try:
    from lib.trading import intel
    from lib.trading.intel import *  # noqa: F401,F403
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
