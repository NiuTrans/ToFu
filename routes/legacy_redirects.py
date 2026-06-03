"""routes/legacy_redirects.py — 308 redirects for retired legacy endpoints.

Background
----------
The legacy ``/api/optimizer/*`` surface was removed in favour of
``/api/v1/optimizer/*`` (see ``docs/legacy_api_migration.md``). The
frontend bundle was migrated in lockstep, but stale browser tabs still
running the pre-migration JS keep polling the old URL once a minute,
which spams ``logs/error.log`` with WARNING-level 404s indefinitely.

This module translates those calls instead of 404'ing them so that
stale tabs:

  * Keep working (the v1 endpoint is functionally identical for reads).
  * Stop emitting WARNING noise in the server logs.

308 (Permanent Redirect) — *not* 301/302 — is the right code here:
it preserves the request method and body, so a stale tab's
``POST /api/optimizer/proposals/<id>/approve`` re-issues correctly
against ``/api/v1/optimizer/proposals/<id>/approve``.

Scope
-----
Deliberately narrow: only the optimizer surface gets a shim, because
that's the one we've actually observed leaking 404s in production.
Other migrated endpoints stay 404 — adding shims for everything would
defeat the point of the migration.
"""

from __future__ import annotations

from flask import Blueprint, redirect, request

from lib.log import get_logger

logger = get_logger(__name__)

legacy_redirects_bp = Blueprint('legacy_redirects', __name__)


@legacy_redirects_bp.route(
    '/api/optimizer/<path:rest>',
    methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
)
def _redirect_optimizer(rest):
    target = '/api/v1/optimizer/' + rest
    qs = request.query_string.decode('latin-1') if request.query_string else ''
    if qs:
        target = target + '?' + qs
    logger.debug(
        '[LegacyRedirect] %s /api/optimizer/%s → %s (stale-tab compat)',
        request.method, rest, target,
    )
    return redirect(target, code=308)


__all__ = ['legacy_redirects_bp']
