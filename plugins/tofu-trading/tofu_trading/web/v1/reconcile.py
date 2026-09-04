"""tofu_trading/web/v1/reconcile.py — v1 blueprint for the reconcile routes.

The handlers live in tofu_trading/web/handlers/trading_reconcile.py; this
module only defines the blueprint they register on, matching the pattern used
by the other v1 shells.
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_trading_reconcile_bp = Blueprint('api_v1_trading_reconcile_bp', __name__)

__all__ = ['api_v1_trading_reconcile_bp']
