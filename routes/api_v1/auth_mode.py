"""routes/api_v1/auth_mode.py — Read / change the auth gate's mode.

Exposes :mod:`lib.auth_mode` over HTTP so the Settings UI (and any
admin SDK client) can flip ``open ↔ private ↔ multi-user`` without
restarting the server.

Routes
------

  ``GET  /api/v1/auth/mode``   — public read; lets the UI know whether
                                  to render the "Auth disabled" banner
                                  or the key-management table.
  ``PUT  /api/v1/auth/mode``   — admin-scoped (in private/multi-user)
                                  or unrestricted (in open) write.

The asymmetry on PUT is intentional: when the gate is already open,
the synthetic local-admin context still carries the ``admin`` scope,
so ``@require_scope('admin')`` permits the change. When the gate is
private, only a real admin key may flip it back to open. There is no
hidden "anyone can disable auth" path.

If ``TOFU_AUTH_MODE`` is set the env var locks the mode; PUT returns
409 (Conflict) explaining how to override.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_ok, api_forbidden
from lib.auth_mode import (
    ALL_MODES, MODE_OPEN, MODE_PRIVATE, MODE_MULTI_USER,
    env_overrides_file, get_state, set_mode,
)
from lib.api_keys import has_any_key
from lib.log import audit_log, get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body, require_str

from .auth import current_auth, require_scope

logger = get_logger(__name__)

api_v1_auth_mode_bp = Blueprint('api_v1_auth_mode', __name__)


def _state_payload() -> dict:
    state = get_state(refresh=True)
    return {
        'mode': state.mode,
        'modes': sorted(ALL_MODES),
        'set_by': state.set_by,
        'since': state.since,
        'source': state.source,
        'env_locked': env_overrides_file(),
        'has_any_key': has_any_key(),
        'descriptions': {
            MODE_OPEN: ('No credential required. Best for local '
                         'personal installs and frontend-only deployments.'),
            MODE_PRIVATE: ('Bearer token / cookie required on every '
                            'non-public route. First-boot bootstrap mints '
                            'an admin key.'),
            MODE_MULTI_USER: ('Bearer token / cookie required. Use the '
                               'API Keys tab to issue per-user tokens.'),
        },
    }


@api_v1_auth_mode_bp.route('/api/v1/auth/mode', methods=['GET'])
@api_meta(summary='Get the current auth-gate mode',
          description='Returns the active auth mode and the closed enum '
                       'of supported modes. Public so the UI can render '
                       'the right settings panel before the user has a '
                       'token.',
          tags=['auth'], public=True)
def get_auth_mode():
    return api_ok(**_state_payload())


@api_v1_auth_mode_bp.route('/api/v1/auth/mode', methods=['PUT'])
@require_scope('admin')
@api_meta(summary='Change the auth-gate mode',
          description='Switch the gate between open / private / '
                       'multi-user. Requires the ``admin`` scope; in '
                       'open mode the synthetic local-admin context '
                       'satisfies that. Returns 409 if the env var '
                       '``TOFU_AUTH_MODE`` is locking the mode.',
          tags=['auth'], scope='admin',
          request_body={'required': True, 'content': {'application/json': {
              'schema': {'type': 'object',
                          'required': ['mode'],
                          'properties': {
                              'mode': {'type': 'string',
                                        'enum': sorted(ALL_MODES)}}}}}})
def put_auth_mode():
    body = parse_body()
    new_mode = require_str(body, 'mode', max_len=20).strip().lower()
    if new_mode not in ALL_MODES:
        return api_bad_request(
            f'Unknown mode: {new_mode!r}. Valid: {sorted(ALL_MODES)}',
            field='mode', allowed=sorted(ALL_MODES))
    if env_overrides_file():
        return api_forbidden(
            'TOFU_AUTH_MODE environment variable is set; remove it '
            'before changing the mode at runtime.',
            error_kind='env_locked')
    ctx = current_auth()
    set_by = ''
    if ctx is not None:
        set_by = ctx.name or ctx.key_id or ''
    try:
        new_state = set_mode(new_mode, set_by=set_by)
    except ValueError as e:
        return api_bad_request(str(e), field='mode')
    audit_log('auth_mode_route_changed', mode=new_state.mode,
              set_by=set_by, prev_source=get_state().source)
    logger.warning('[AuthMode] route changed mode → %s by %r',
                   new_state.mode, set_by or '<unspecified>')
    return api_ok(**_state_payload())


__all__ = ['api_v1_auth_mode_bp']
