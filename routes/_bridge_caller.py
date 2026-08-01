"""routes/_bridge_caller.py — Shared request-aware bridge caller resolution.

The browser and desktop poll routes resolve their caller through ONE
function, so the two bridges' identity layer is literally the same object
(B0 §5.3 / pt_3ba97339b4024fb4). The credential chain itself lives in
:func:`lib.bridge_auth.resolve_bridge_credential` (shared with the global
gate); this module adds the request-facing parts both routes need in
identical form:

  * reading the ``X-Bridge-Secret`` header;
  * the one-time startup audit entry when enforcement is configured;
  * the failure audit + warning log, parameterised by ``kind``;
  * the uniform 401 JSON envelope.
"""

import threading

from flask import jsonify, request

from lib.bridge_auth import resolve_bridge_credential
from lib.env_compat import getenv_compat
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


# ── One-time startup audit when bridge auth is configured ──
# Emits a single audit_log entry the first time enforcement runs, so
# operators have a trace that the §10.4-gated change is in effect. Shared
# across both bridges: the fact it records ("enforcement is on") is one
# fact, not two.
_AUDIT_LOCK = threading.Lock()
_AUDIT_LOGGED = False


def _maybe_audit_enforcement_on() -> None:
    global _AUDIT_LOGGED
    if _AUDIT_LOGGED:
        return
    with _AUDIT_LOCK:
        if _AUDIT_LOGGED:
            return
        _AUDIT_LOGGED = True
        try:
            audit_log('config_change',
                      param='bridge_auth_enforcement',
                      old='permissive (Phase A)',
                      new='enforcing (Phase C)',
                      reason='TOFU_BRIDGE_SECRET configured — bridge endpoints '
                             'now reject requests without a matching '
                             'X-Bridge-Secret or an agents:bridge-scoped API key',
                      approved_by='user')
        except Exception as e:
            logger.debug('[Bridge] startup audit_log failed: %s', e)


def resolve_bridge_caller(kind='browser'):
    """Resolve the poll caller → ``(ok, user_id, key_id)``.

    Reads the ``X-Bridge-Secret`` header of the current request and resolves
    it through the single credential chain (lib.bridge_auth). ``kind`` is the
    audit/log label (``'browser'`` / ``'desktop'``). On failure this audits
    and warns once, then returns ``(False, '', '')`` — the route must answer
    with :func:`bridge_unauthorized`.
    """
    if (getenv_compat('TOFU_BRIDGE_SECRET') or '').strip():
        _maybe_audit_enforcement_on()
    provided = request.headers.get('X-Bridge-Secret', '')
    ok, user_id, key_id = resolve_bridge_credential(provided, open_when_unset=True)
    if ok:
        return True, user_id, key_id
    try:
        audit_log('bridge_auth_fail',
                  kind=kind,
                  path=request.path,
                  ip=request.remote_addr,
                  has_header=bool(provided),
                  ua=(request.user_agent.string or '')[:120])
    except Exception as _aerr:
        logger.debug('[Bridge] audit_log bridge_auth_fail failed: %s', _aerr)
    logger.warning('[%s] bridge auth rejected from %s on %s (header=%s)',
                   kind.capitalize(), request.remote_addr, request.path,
                   'present' if provided else 'missing')
    return False, '', ''


def check_bridge_auth(kind='browser') -> bool:
    """Back-compat bool wrapper — see :func:`resolve_bridge_caller`."""
    ok, _user_id, _key_id = resolve_bridge_caller(kind)
    return ok


def bridge_unauthorized():
    """Return a uniform 401 JSON envelope for bridge auth failures."""
    return jsonify({
        'error': 'bridge_auth_required',
        'hint': 'set X-Bridge-Secret header to match TOFU_BRIDGE_SECRET',
    }), 401
