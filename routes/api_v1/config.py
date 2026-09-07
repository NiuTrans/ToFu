"""routes/api_v1/config.py — Miscellaneous server-settings surface.

This blueprint defines ``api_v1_config_bp``. The actual handlers live
in :mod:`routes.config`, which imports this blueprint as the alias
``config_bp`` and registers the settings routes here:

  GET    /api/v1/server-config             — full server config (sensitive)
  POST   /api/v1/server-config             — save + hot-reload
  GET    /api/v1/feishu/status             — Feishu bot status
  POST   /api/v1/network/proxy-test        — test one unsaved proxy row

Provider discovery and owner-scoped model routing live exclusively in
:mod:`routes.api_v1.providers`.
"""

from __future__ import annotations

from quart import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_config_bp = Blueprint('api_v1_config', __name__)


__all__ = ['api_v1_config_bp']
