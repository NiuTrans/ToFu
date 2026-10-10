"""routes/api_v1/trading/simulator.py — v1 blueprint for trading_simulator routes.

The handlers themselves live in routes/trading_simulator.py; this module just
defines the blueprint they register on. Routes are mounted at
/api/v1/trading/simulator/...
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_trading_simulator_bp = Blueprint('api_v1_trading_simulator_bp', __name__)

__all__ = ['api_v1_trading_simulator_bp']
