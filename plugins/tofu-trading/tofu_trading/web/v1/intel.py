"""routes/api_v1/trading/intel.py — v1 blueprint for trading_intel routes.

The handlers themselves live in routes/trading_intel.py; this module just
defines the blueprint they register on. Routes are mounted at
/api/v1/trading/intel/...
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_trading_intel_bp = Blueprint('api_v1_trading_intel_bp', __name__)

__all__ = ['api_v1_trading_intel_bp']
