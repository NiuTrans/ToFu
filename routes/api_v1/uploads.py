"""routes/api_v1/uploads.py — Image-generation + VLM-task JSON routes.

This blueprint defines ``api_v1_uploads_bp``. The actual handlers live
in :mod:`routes.upload`, which imports this blueprint and registers 4
of its 9 routes here:

  POST /api/v1/images/generate         — text-to-image generation
  GET  /api/v1/images/models           — available image models
  GET  /api/v1/pdf/vlm-parse/<task_id> — poll a VLM parse task
  GET  /api/v1/pdf/vlm-tasks           — list active VLM tasks

The other 5 routes stay on ``upload_bp`` because they're multipart
uploads or static-asset serving, not JSON REST verbs:
``/api/images/upload``, ``/api/images/<filename>``, ``/api/pdf/parse``,
``/api/pdf/vlm-parse`` (POST starts a parse with a multipart body),
``/api/doc/parse``.
"""

from __future__ import annotations

from quart import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_uploads_bp = Blueprint('api_v1_uploads', __name__)


__all__ = ['api_v1_uploads_bp']
