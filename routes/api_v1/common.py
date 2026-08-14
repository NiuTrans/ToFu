"""routes/api_v1/common.py — Shared JSON routes (pricing / dispatch / features / log-compress).

This blueprint defines ``api_v1_common_bp``. The actual handlers live
in :mod:`routes.common`, which imports this blueprint and registers
10 routes here:

  POST /api/v1/logs/compress              — LLM-powered log compaction
  GET  /api/v1/pricing                    — model price card
  GET  /api/v1/pricing/data               — alias of /pricing
  POST /api/v1/pricing/refresh            — admin: refresh upstream
  GET  /api/v1/dispatch/quota             — 5-h rolling request stats
  GET  /api/v1/dispatch/endpoint-metrics  — per-endpoint live metrics
  GET  /api/v1/dispatch/key-stats         — per-key success rate
  POST /api/v1/dispatch/key-override      — admin: toggle key on/off
  GET  /api/v1/features                   — feature-flag snapshot
  POST /api/v1/features                   — admin: toggle feature flags

Removed entirely (legacy stubs superseded by ``/api/v1/users/*``):
  GET    /api/me        \u2014 use ``GET /api/v1/users/me``
  POST   /api/login     \u2014 use ``POST /api/v1/users/login``
  POST   /api/logout    \u2014 use ``POST /api/v1/users/logout``
  POST   /api/register  \u2014 use ``POST /api/v1/users/signup``

Stays on legacy ``common_bp`` (carve-outs):
  GET    /, /trading.html, /login, /signup, /dashboard \u2014 HTML page serving
  POST   /api/client-error    \u2014 browser telemetry beacon
  GET    /api/health          \u2014 liveness probe
  GET    /favicon.{ico,svg}   \u2014 static assets
"""

from __future__ import annotations

from quart import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_common_bp = Blueprint('api_v1_common', __name__)


__all__ = ['api_v1_common_bp']
