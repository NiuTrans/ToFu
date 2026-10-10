"""routes/api_v1/paper.py — Paper-reader v1 surface (JSON routes only).

This blueprint just *defines* ``api_v1_paper_bp``. The actual route
handlers live in :mod:`routes.paper`, which imports this blueprint and
registers 17 of its 22 routes here. The remaining 5 (``chat``,
``fetch-arxiv-stream``, ``upload``, ``images/<phash>/<file>``,
``pdf/<file>``) stay on the legacy ``paper_bp`` because they're SSE /
multipart / static asset endpoints, not JSON REST verbs.

Routes registered (paths owned by this blueprint, definitions in routes/paper.py):

  POST   /api/v1/paper/extract-images
  POST   /api/v1/paper/report/start
  GET    /api/v1/paper/report/poll
  POST   /api/v1/paper/report/abort
  POST   /api/v1/paper/report/lookup  (live task + opt-in cache resolution)
  GET    /api/v1/paper/report/export
  POST   /api/v1/paper/report/cache
  POST   /api/v1/paper/qa/start
  GET    /api/v1/paper/qa/poll
  POST   /api/v1/paper/qa/abort
  POST   /api/v1/paper/translate/start
  GET    /api/v1/paper/translate/poll
  POST   /api/v1/paper/translate/abort
  POST   /api/v1/paper/translate/lookup
  POST   /api/v1/paper/translate/cache
  POST   /api/v1/paper/fetch-arxiv
  POST   /api/v1/paper/reparse
  GET    /api/v1/paper/library
  GET    /api/v1/paper/library/summaries
  GET    /api/v1/paper/library/<paper_id>
  PUT    /api/v1/paper/library/<paper_id>
  DELETE /api/v1/paper/library/<paper_id>

The headless-SDK aliases at ``/api/v1/agents/paper/{report,translate,...}``
in :mod:`routes.api_v1.agents` continue to wrap the same engine.
"""

from __future__ import annotations

from quart import Blueprint

from lib.log import get_logger

logger = get_logger(__name__)

api_v1_paper_bp = Blueprint('api_v1_paper', __name__)


__all__ = ['api_v1_paper_bp']
