"""lib/desktop_agent/_push_attach.py — browser-pushed zero-config attach.

WHY THIS EXISTS
---------------
The personalized one-file installer (a fresh bridge credential baked into
the downloaded artifact) is Windows-only by construction — it rewrites an
NSIS trailer (``lib/desktop_dist/agent_installer.py``). A macOS/Linux
visitor downloads a GENERIC mirrored DMG/tarball, so the credential has no
file to ride in.

This module is the macOS/Linux answer, and it needs no container surgery at
all: the agent already runs a loopback-only broker for the SSO browser
relay (``_browser_relay.py``), and the Local Control page the user just
downloaded from is, by construction, a signed-in tab on the very server the
agent should attach to. The page therefore PUSHES the attach bundle
(routes + fresh per-user token, minted by
``POST /api/v1/desktop/agent-attach-bundle``) to the broker's
``/v1/attach``; this module validates and persists it. Download → install →
launch → the open tab finishes the attach. No typing, no pairing code, no
copied credential — the same user-visible outcome as the Windows .exe.

TRUST MODEL (the pre-attachment bootstrap problem)
--------------------------------------------------
An attached agent gates its broker on the configured server origin. A
fresh agent knows no origin, so ``/v1/attach`` cannot be gated that way —
and an ungated credential sink would let ANY website the victim visits
point the agent at a third party. The gate here is therefore structural,
not secret-based:

  * the request's unforgeable ``Origin`` must own one of the bundle's own
    routes — a page can only attach the agent to THE SERVER THE PAGE CAME
    FROM. A drive-by page can point the agent at nothing but itself;
  * a LIVE saved attachment refuses the push outright (changing servers is
    done from that server's own page); only a DEAD one is re-pointed, and
    its address is kept as a trailing candidate (mirrors
    ``import_attach_bundle``, owner incident 2026-08-06);
  * shape caps keep the payload tiny and http(s)-only;
  * the broker serializes and throttles attempts; every refusal is logged.

Residual risk, deliberately accepted: a malicious page can attach a fresh
agent to ITS OWN server. The agent comes up deny-by-default with computer
control OFF (``_permissions.SAFE_DEFAULT`` — no write/exec/GUI/egress), the
attached address is displayed in the tray and role window, and quitting the
agent severs it. That is the same trust posture as the relay broker's
loopback boundary.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)

_MAX_URLS = 8
_MAX_URL_CHARS = 300
_MAX_TOKEN_CHARS = 512


def _urls(value) -> list[str]:
    """Normalise a route list: http(s) only, deduped, capped."""
    out: list[str] = []
    if not isinstance(value, list):
        return out
    for item in value[:_MAX_URLS]:
        u = str(item or '').strip().rstrip('/')
        if (u.startswith(('http://', 'https://'))
                and len(u) <= _MAX_URL_CHARS and u not in out):
            out.append(u)
    return out


def handle_pushed_attach(payload, origin: str, *, log=lambda _m: None):
    """Validate + persist a browser-pushed attach bundle.

    Args:
        payload: the decoded JSON body (``candidates`` /
            ``fallback_candidates`` / ``token``).
        origin: the request's ``Origin`` header (unforgeable in a browser).
        log: launcher diagnostic sink.

    Returns:
        ``(ok, reason, url, transport)`` — ``(True, 'attached', url,
        'direct')`` when a route probed alive (the agent polls it
        directly); ``(True, 'attached_optimistic', url, 'browser')`` when
        nothing answered directly (an SSO edge eats cookieless probes —
        polls must ride the page relay until one succeeds); otherwise
        ``(False, reason, '', '')`` with reason in ``bad_shape`` /
        ``no_routes`` / ``origin_mismatch`` / ``already_attached`` /
        ``save_failed``.
    """
    from lib.desktop_agent._browser_relay import origin_of
    from lib.desktop_agent._probe import probe_server
    from lib.desktop_agent.config import remote_server, save_attachment

    if not isinstance(payload, dict):
        return False, 'bad_shape', '', ''
    candidates = _urls(payload.get('candidates'))
    fallbacks = [u for u in _urls(payload.get('fallback_candidates'))
                 if u not in candidates]
    token = str(payload.get('token') or '').strip()[:_MAX_TOKEN_CHARS]
    if not candidates and not fallbacks:
        return False, 'no_routes', '', ''

    page_origin = origin_of(origin)
    route_origins = {origin_of(u) for u in candidates + fallbacks}
    route_origins.discard('')
    if not page_origin or page_origin not in route_origins:
        log('Pushed attach refused: origin %r owns none of the routes'
            % origin)
        logger.warning('[PushAttach] origin %r matched no bundle route',
                       origin)
        return False, 'origin_mismatch', '', ''

    existing, existing_secret = remote_server()
    if existing:
        alive, dead_reason = probe_server(existing, timeout=2.5)
        if alive:
            log('Pushed attach refused: already attached to live %s'
                % existing)
            return False, 'already_attached', '', ''
        log('Saved attachment %s is dead (%s) — the push is a re-point'
            % (existing, dead_reason))

    winner = ''
    for url in candidates:
        ok, reason = probe_server(url, timeout=2.5)
        if ok:
            winner = url
            break
        log('Push candidate %s not reachable: %s' % (url, reason))
    if not winner:
        for url in fallbacks:
            ok, reason = probe_server(url, timeout=2.5)
            if ok:
                winner = url
                break
            log('Push fallback %s not reachable: %s' % (url, reason))
    if winner:
        chosen = winner
    else:
        # Nothing answers a cookieless probe (the SSO case): the route the
        # page relay can actually carry is the page's OWN address — prefer
        # it over a blindly optimistic LAN candidate.
        chosen = next(
            (u for u in candidates + fallbacks
             if origin_of(u) == page_origin),
            candidates[0] if candidates else fallbacks[0])
    route_set = list(candidates) + list(fallbacks)
    if existing and existing != chosen and existing not in route_set:
        route_set.append(existing)  # a transient outage may recover — keep
    try:
        save_attachment(chosen, token or existing_secret,
                        attach_candidates=route_set)
    except Exception as e:
        log('Could not persist pushed attachment: %s' % e)
        logger.warning('Could not persist pushed attachment: %s', e)
        return False, 'save_failed', '', ''
    if winner:
        log('Pushed attach imported: polling %s (probed alive)' % chosen)
        return True, 'attached', chosen, 'direct'
    log('Pushed attach imported optimistically: %s — polls ride the '
        'browser relay until one succeeds' % chosen)
    return True, 'attached_optimistic', chosen, 'browser'


__all__ = ['handle_pushed_attach']
