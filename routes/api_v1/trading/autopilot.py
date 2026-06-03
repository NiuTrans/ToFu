"""routes/api_v1/trading/autopilot.py — v1 blueprint for trading_autopilot routes.

The handlers themselves live in routes/trading_autopilot.py; this module just
defines the blueprint they register on. Routes are mounted at
/api/v1/trading/autopilot/...
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_trading_autopilot_bp = Blueprint('api_v1_trading_autopilot_bp', __name__)

__all__ = ['api_v1_trading_autopilot_bp']
