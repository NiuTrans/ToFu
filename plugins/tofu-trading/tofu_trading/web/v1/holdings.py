"""routes/api_v1/trading/holdings.py — v1 blueprint for trading_holdings routes.

The handlers themselves live in routes/trading_holdings.py; this module just
defines the blueprint they register on. Routes are mounted at
/api/v1/trading/holdings/...
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_trading_holdings_bp = Blueprint('api_v1_trading_holdings_bp', __name__)

__all__ = ['api_v1_trading_holdings_bp']
