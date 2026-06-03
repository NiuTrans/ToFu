"""routes/api_v1/config.py — Server-config + provider templates surface.

This blueprint defines ``api_v1_config_bp``. The actual handlers live
in :mod:`routes.config`, which imports this blueprint as the alias
``config_bp`` and registers all 9 routes here:

  GET    /api/v1/server-config             — full server config (sensitive)
  POST   /api/v1/server-config             — save + hot-reload
  GET    /api/v1/feishu/status             — Feishu bot status
  POST   /api/v1/providers/balance         — fetch upstream wallet balance
  POST   /api/v1/providers/discover-models — re-discover model list
  PUT    /api/v1/providers/templates/update — update a provider template
  POST   /api/v1/providers/probe           — single-provider probe
  POST   /api/v1/providers/probe-bulk      — bulk probe
  GET    /api/v1/providers/templates       — list available templates

These provider-template / probe routes complement the CRUD surface in
:mod:`routes.api_v1.providers` (``GET/POST /api/v1/providers``,
``GET/PATCH/DELETE /api/v1/providers/{id}``, ``POST /probe``).
"""

from __future__ import annotations

from flask import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_config_bp = Blueprint('api_v1_config', __name__)


__all__ = ['api_v1_config_bp']
