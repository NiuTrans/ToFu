"""Request adapter for the single owner-scoped device credential chain."""

from quart import g, jsonify, request

from lib.bridge_auth import resolve_bridge_credential
from lib.log import audit_log, get_logger

logger = get_logger(__name__)


def resolve_bridge_caller(kind='browser'):
    """Resolve one poll caller to ``(ok, owner_user_id, key_id)``."""
    provided = request.headers.get('X-Bridge-Secret', '')
    context = getattr(g, 'bridge_auth_context', None)
    if context is None:
        context = resolve_bridge_credential(
            provided,
            allow_process_agent=(kind == 'desktop'),
        )
    if context is not None:
        return True, str(context.owner_user_id), context.key_id
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


def bridge_unauthorized():
    """Return a uniform 401 JSON envelope for bridge auth failures."""
    return jsonify({
        'error': 'bridge_auth_required',
        'hint': 'pair this device to obtain an agents:bridge credential',
    }), 401
