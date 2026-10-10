"""routes/api_v1/trading/decision.py — v1 blueprint for trading_decision routes.

The handlers themselves live in routes/trading_decision.py; this module just
defines the blueprint they register on. Routes are mounted at
/api/v1/trading/decision/...
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_trading_decision_bp = Blueprint('api_v1_trading_decision_bp', __name__)

__all__ = ['api_v1_trading_decision_bp']
