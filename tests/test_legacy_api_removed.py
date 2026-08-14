"""End-state assertion for the legacy-API removal effort.

After the per-domain migrations, the only remaining ``/api/<x>`` routes
that aren't under a versioned JSON namespace should be documented carve-outs (SSE
streams, multipart uploads, browser-redirect OAuth flows, static-asset
serving, telemetry beacon, WebSocket multiplexer, liveness probe,
OpenAPI viewers).

This test enumerates every URL rule registered on ``server.app`` and
fails if any unversioned ``/api/`` route falls outside the allow-list. New
JSON routes should land under ``/api/v1/...`` or ``/api/v2/...``; if a new carve-out is
genuinely needed, add it to ``ALLOWED_NON_V1`` here AND document it in
``docs/legacy_api_migration.md`` §1.
"""

from __future__ import annotations

import os
import sys

import pytest

# Force open auth mode \u2014 ``server.app`` is instantiated without a token
# below, and the conftest.py default of ``private`` would 401 every
# request (we don't actually fire requests here, but we still want a
# clean import).
pytestmark = pytest.mark.auth_mode('open')


# ── Configuration ────────────────────────────────────────────────────

# Routes that intentionally stay under ``/api/<x>`` (NOT migrated to v1).
# Each entry includes the full URL rule string. Path parameters use Flask
# converter syntax (``<task_id>``, ``<int:hid>``, etc.) so the rule string
# matches what ``app.url_map.iter_rules()`` reports.
ALLOWED_NON_V1 = frozenset({
    # Liveness / OpenAPI
    '/api/health',
    '/api/openapi.json',
    '/api/openapi.yaml',
    '/api/openapi.refresh',
    '/api/docs',
    '/api/redoc',
    # Browser telemetry beacon
    '/api/client-error',
    # Real-time push (WebSocket)
    '/api/push',
    # DEBUG-only presence smoke emitter (gated by TOFU_PRESENCE_DEBUG=1; used
    # by debug/presence_smoke.py --live). Not a JSON-contract API route.
    '/api/push/debug/presence',
    # OAuth browser-redirect flows (POST and GET both registered on each)
    '/api/oauth/login',
    '/api/oauth/callback',
    '/api/oauth/logout',
    # Browser-side token exchange (B1 geo-block workaround): the browser
    # POSTs a token it exchanged itself. Browser-flow carve-out, like above.
    '/api/oauth/store-token',
    # SSE / long-poll streams
    '/api/chat/stream/<task_id>',
    '/api/paper/fetch-arxiv-stream',
    '/api/paper/chat',
    # Multipart uploads
    '/api/paper/upload',
    '/api/pdf/parse',
    '/api/pdf/vlm-parse',
    '/api/doc/parse',
    '/api/images/upload',
    '/api/translate/pptx',
    # Static asset serving
    '/api/paper/images/<phash>/<filename>',
    '/api/paper/pdf/<filename>',
    '/api/images/<filename>',
    '/api/translate/pptx/download/<filename>',
    # Artifact binary / sandboxed-HTML carve-outs
    '/api/artifacts/<artifact_id>/raw',
    '/api/artifacts/<artifact_id>/view',
    '/api/artifacts/<artifact_id>/export',
    # Bridge-Secret long-poll RPC (browser extension + desktop agent)
    '/api/browser/poll',
    '/api/browser/commands',
    '/api/browser/result',
    '/api/browser/download',
    '/api/desktop/poll',
    # Pairing-code exchange for the desktop agent (RWA P4a): the 6-digit
    # one-time code IS the credential (code + audit, no bearer), and the
    # SHIPPED TofuAgent-Setup-0.16.0 installer embeds this exact path —
    # moving it to /api/v1/* would break in-field agents.
    '/api/desktop/pair',
    # Static video-bytes serving (frontend playback / re-download) — same
    # static-asset carve-out family as /api/images/<filename>.
    '/api/videos/<filename>',
    # 308 redirect shim for stale browser tabs still on the pre-migration
    # /api/optimizer/* polling URL — see routes/legacy_redirects.py.
    '/api/optimizer/<path:rest>',
})


def _build_app_open_mode():
    """Build a fresh ``server.app`` under TOFU_AUTH_MODE=open.

    Importing ``server`` once per session would be cheaper, but the
    routes/api_v1/auth.py auth gate caches the mode at first use; we
    want a deterministic open-mode app for the URL-map walk regardless
    of how the test session was invoked.
    """
    import importlib.util

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        'server',
        os.path.join(os.path.dirname(os.path.dirname(__file__)), 'server.py'),
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__name__ = 'server'
    spec.loader.exec_module(mod)
    return mod.app


def test_no_legacy_api_routes_remain():
    """Every ``/api/<x>`` route must be either ``/api/v1/<x>`` or in the
    carve-out allow-list.

    Failure mode: a new route was added at ``/api/foo`` instead of
    ``/api/v1/foo``. Either (a) move it to v1, or (b) if it's
    legitimately a non-JSON route (multipart upload, SSE stream,
    browser-redirect, static asset, etc.), add it to ``ALLOWED_NON_V1``
    in this file AND to ``docs/legacy_api_migration.md`` §1.
    """
    app = _build_app_open_mode()

    # The migration policy governs IN-TREE (core) routes only. External
    # plugin blueprints (mounted via the tofu.blueprints entry-point group,
    # e.g. a private knowledge plugin) legitimately own their own
    # ``/api/<plugin>/`` namespace and are environment-dependent (present
    # only when that plugin is installed), so they're out of scope here.
    from routes import ALL_BLUEPRINTS
    core_bp_names = {bp.name for bp in ALL_BLUEPRINTS}

    legacy = []
    for rule in app.url_map.iter_rules():
        path = rule.rule
        if not path.startswith('/api/'):
            continue
        if path.startswith(('/api/v1/', '/api/v2/')):
            continue
        if path in ALLOWED_NON_V1:
            continue
        bp_name = rule.endpoint.split('.')[0] if '.' in rule.endpoint else ''
        if bp_name and bp_name not in core_bp_names:
            continue  # external plugin route — not subject to v1 migration
        legacy.append(path)

    assert not legacy, (
        'Legacy /api/* routes remain outside the carve-out allow-list:\n'
        + '\n'.join(f'  - {p}' for p in sorted(set(legacy)))
        + '\n\nIf any of these are legitimately non-JSON (multipart/SSE/'
          'static/redirect/beacon), add them to ALLOWED_NON_V1 in '
          'tests/test_legacy_api_removed.py and document in '
          'docs/legacy_api_migration.md §1. Otherwise migrate them to '
          'a versioned /api/v1/<...> or /api/v2/<...> namespace.'
    )


def test_carve_out_list_is_exhaustive():
    """Every entry in ``ALLOWED_NON_V1`` must actually be registered.

    Catches drift in the other direction: an allow-listed path that no
    longer exists in the route map indicates either (a) a stale
    carve-out entry (delete from ALLOWED_NON_V1), or (b) a route that
    was renamed without updating this test.

    Trading routes are conditional on ``lib.TRADING_ENABLED``; this
    test boots the app in whatever mode the env dictates. We don't
    require trading-specific carve-outs because there are none today
    (all trading routes migrated to /api/v1/).
    """
    app = _build_app_open_mode()
    registered = {rule.rule for rule in app.url_map.iter_rules()}
    stale = [p for p in ALLOWED_NON_V1 if p not in registered]
    assert not stale, (
        'ALLOWED_NON_V1 contains paths that are NOT registered on '
        'the app:\n' + '\n'.join(f'  - {p}' for p in sorted(stale))
        + '\n\nEither restore the route or remove the entry from '
          'ALLOWED_NON_V1.'
    )
