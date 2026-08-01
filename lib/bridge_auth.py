"""lib/bridge_auth.py — Single bridge-caller credential chain (B0 §3.4/§5.3).

Every bridge endpoint resolves the presented ``X-Bridge-Secret`` credential
through :func:`resolve_bridge_credential` — exactly ONE chain, three
consumers:

  * ``routes/browser.py``   — via ``routes/_bridge_caller.py``;
  * ``routes/desktop.py``   — via ``routes/_bridge_caller.py``;
  * ``routes/api_v1/auth.py`` — the global ``before_request`` gate.

The regression this module exists to make structurally impossible
(pt_3ba97339b4024fb4): the browser route hand-rolled a bool-only check that
never resolved per-user tokens, so the client registry's ``user_id`` stayed
``''`` forever and the §5.3 fail-closed delivery gate compared ``'' == ''``
on every production poll — unreachable at the HTTP entry while the lib-level
guard suite (which passes ``user_id=…`` explicitly) stayed 12/12 green.
"""

import hmac

from lib.env_compat import getenv_compat
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['resolve_bridge_credential']


def resolve_bridge_credential(provided, *, loopback_token=None,
                              open_when_unset=False):
    """Resolve a presented bridge credential → ``(ok, user_id, key_id)``.

    Args:
        provided: Raw ``X-Bridge-Secret`` header value.
        loopback_token: When given, this process-local token is also
            accepted (the packaged tray agent's credential — see
            ``routes/api_v1/auth.py:loopback_agent_token``). Only the global
            gate passes it; the route layer deliberately does not, keeping
            byte-parity with the pre-convergence desktop route.
        open_when_unset: Routes pass ``True`` — with no TOFU_BRIDGE_SECRET
            configured, the route layer is the open legacy single-user world
            (the global gate has already demanded a credential before the
            request ever reaches a route). The gate itself passes ``False``:
            a bridge endpoint NEVER opens on a missing credential (§3.4).

    Resolution order (RWA P4a 约束②第三条, byte-parity with the desktop
    route's original resolver):

      * ``open_when_unset`` and no global secret configured → open legacy
        ``(True, '', '')``;
      * header matches the global secret → legacy super-user
        ``(True, '', '')`` — deliberately unscoped (sees every tenant);
      * header matches ``loopback_token`` → in-process agent
        ``(True, '', '')``;
      * else the header is tried as a per-user API key carrying the
        ``agents:bridge`` scope → ``(True, user_id, key_id)`` — the poll is
        then scoped to that user;
      * otherwise ``(False, '', '')`` — the caller must reject.

    Note: the scope check is LITERAL membership, not ``ctx.has_scope()`` —
    an admin-scope key without ``agents:bridge`` is NOT a bridge credential.
    That is the stricter (fail-closed) of the two checks the pre-convergence
    copies disagreed on, and preserves the observable end-to-end outcome on
    both bridges (such a key was always rejected one layer later).
    """
    provided = (provided or '').strip()
    expected = (getenv_compat('TOFU_BRIDGE_SECRET') or '').strip()
    if not expected:
        if open_when_unset:
            return True, '', ''
    elif provided and hmac.compare_digest(provided, expected):
        return True, '', ''
    if loopback_token and provided and hmac.compare_digest(provided, loopback_token):
        return True, '', ''
    if provided:
        try:
            from lib.api_keys import validate_token
            ctx = validate_token(provided)
        except Exception as e:
            logger.debug('[BridgeAuth] per-user token validation failed: %s', e)
            ctx = None
        if ctx is not None and 'agents:bridge' in getattr(ctx, 'scopes', ()):
            return True, (getattr(ctx, 'user_id', '') or ''), ctx.key_id
    return False, '', ''
