"""routes/api_v1/update.py — Self-update surface for the topbar button.

Routes (mounted under ``/api/v1``):

  GET  /api/v1/update/check    — compare installed VERSION vs. the newest
                                 GitHub release tag; report git availability
                                 and whether the working tree is safe to pull.
  POST /api/v1/update/apply    — admin: ``git pull --ff-only`` from the
                                 official remote. Refuses on a dirty tree.
  POST /api/v1/update/restart  — admin: re-exec the server process so pulled
                                 ``.py`` changes take effect. Explicit only —
                                 ``apply`` never auto-restarts.

The heavy lifting lives in :mod:`lib.self_update`; this layer is a thin,
fully-logged HTTP wrapper.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from flask import Blueprint

from lib.api_response import api_error, api_internal_error, api_ok
from lib.log import audit_log, get_logger
from lib.openapi import api_meta

from .auth import require_auth, require_scope

logger = get_logger(__name__)

api_v1_update_bp = Blueprint('api_v1_update', __name__)


@api_v1_update_bp.route('/api/v1/update/check', methods=['GET'])
@require_auth
@api_meta(
    summary='Check for an available update',
    description=(
        'Compares the installed version against the newest release tag on '
        'the official GitHub repository. Also reports whether this is a git '
        'checkout and whether the working tree is safe to fast-forward '
        '(runtime-state churn under .tofu/ is tolerated; tracked-source '
        'edits block the update). Read-only.'
    ),
    tags=['system'],
)
def update_check():
    from lib.self_update import check_for_update
    try:
        payload = check_for_update()
    except Exception as e:
        logger.error('[Update] check failed: %s', e, exc_info=True)
        return api_internal_error(e, context='update_check',
                                  source='api_v1.update.check')
    return api_ok(payload)


@api_v1_update_bp.route('/api/v1/update/apply', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Apply the available update',
    description=(
        'Runs git fetch + git pull --ff-only against the official remote. '
        'Refuses (without mutating anything) if git is unavailable or the '
        'working tree has tracked-source changes — it never auto-stashes '
        'or force-resets. User settings live outside tracked code and are '
        'never touched. If the pull touches requirements.txt, runs pip '
        'install -r requirements.txt against the running interpreter so '
        'the update is self-contained. Returns needs_restart=true when '
        'files changed; the caller must POST /api/v1/update/restart.'
    ),
    tags=['system'],
)
def update_apply():
    from lib.self_update import apply_update
    try:
        result = apply_update()
    except Exception as e:
        logger.error('[Update] apply failed: %s', e, exc_info=True)
        return api_internal_error(e, context='update_apply',
                                  source='api_v1.update.apply')
    if not result.get('ok'):
        # Two failure shapes, both client-actionable → 409 Conflict:
        #   * refused (dirty tree / no git): changed=False, nothing happened.
        #   * pulled but deps install failed: changed=True, code IS updated;
        #     the body's deps_* fields let the UI tell the user to fix deps.
        return api_error(result.get('error') or 'Update could not be applied.',
                         status=409, detail=result.get('detail', ''),
                         **{k: result[k] for k in
                            ('old_version', 'new_version', 'changed',
                             'needs_restart', 'deps_changed',
                             'deps_installed', 'deps_detail') if k in result})
    return api_ok(result)


def _deferred_reexec(delay: float = 0.6):
    """Re-exec the current process after a short delay.

    Runs in a daemon thread so the HTTP response flushes first. Mirrors
    server.py's launch contract: same interpreter, same argv. The instance
    lock fd is close-on-exec, so it releases before the new image re-acquires.
    """
    time.sleep(delay)
    logger.info('[Update] Re-execing server: %s %s',
                sys.executable, ' '.join(sys.argv))
    try:
        # Let the env-reexec guard run again from a clean slate.
        os.environ.pop('_TOFU_ENV_REEXEC', None)
        os.execv(sys.executable, [sys.executable, *sys.argv])
    except OSError as e:
        # execv only returns on failure — log loudly; the process keeps running.
        logger.critical('[Update] Re-exec failed, server NOT restarted: %s',
                        e, exc_info=True)


@api_v1_update_bp.route('/api/v1/update/restart', methods=['POST'])
@require_scope('admin')
@api_meta(
    summary='Restart the server',
    description=(
        'Re-execs the server process so freshly-pulled code takes effect. '
        'Explicit and admin-only — there is no silent auto-restart. The '
        'response is sent before the process restarts; clients should wait '
        'a few seconds and reconnect.'
    ),
    tags=['system'],
)
def update_restart():
    audit_log('self_update_restart', pid=os.getpid())
    logger.warning('[Update] Restart requested — re-exec scheduled (pid=%d)',
                   os.getpid())
    threading.Thread(target=_deferred_reexec, name='tofu-restart',
                     daemon=True).start()
    return api_ok({'restarting': True})


__all__ = ['api_v1_update_bp']
