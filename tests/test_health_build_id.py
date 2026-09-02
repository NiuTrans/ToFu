"""Guard: /api/health carries the frontend buildId (long-lived-tab handshake).

WHY
---
A browser tab keeps running the vite bundle it was loaded with indefinitely —
the ``vite:preloadError`` reload only fires when a lazy chunk 404s, so a tab
that never hits a missing chunk runs yesterday's JS forever (the "bug fixed
hours ago, user still sees it" class; e.g. the sidebar 今天→昨天→今天 date-
group interleave fix only reached tabs that happened to reload). The client
build watch polls ``/api/health`` and hard-reloads (idle-gated, loop-guarded)
when ``buildId`` differs from the bundle it is running.

This test locks the server half: ``buildId`` equals the basename of the
``main`` entry file in ``static/vite/manifest.json``, and the field survives
the route's other best-effort sections. Pure-logic; ``unit`` marker.
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _call_health():
    import asyncio
    import quart
    import routes.common as common

    app = quart.Quart('health-build-id-test')

    async def _run():
        async with app.test_request_context('/api/health'):
            resp, _status = common.health_check()
            return await resp.get_json()

    return asyncio.run(_run())


def test_health_reports_current_build_id():
    from lib import vite_assets

    vite_assets.clear_vite_asset_cache()
    manifest = json.loads(open(vite_assets.VITE_MANIFEST, encoding='utf-8').read())
    entry_file = manifest[vite_assets.VITE_ENTRIES['main']]['file']
    want = entry_file.rsplit('/', 1)[-1]

    body = _call_health()
    assert body['ok'] is True
    assert body.get('buildId') == want, (
        f"buildId={body.get('buildId')!r} != manifest entry basename {want!r}")


def test_get_vite_build_id_matches_manifest():
    from lib import vite_assets

    vite_assets.clear_vite_asset_cache()
    manifest = json.loads(open(vite_assets.VITE_MANIFEST, encoding='utf-8').read())
    want = manifest[vite_assets.VITE_ENTRIES['main']]['file'].rsplit('/', 1)[-1]
    assert vite_assets.get_vite_build_id('main') == want


def test_get_vite_build_id_fail_quiet(monkeypatch):
    """Missing manifest → '' (the client then never reloads — a missing build
    id must never cause a reload loop)."""
    from lib import vite_assets

    monkeypatch.setattr(vite_assets, 'VITE_MANIFEST', '/nonexistent/manifest.json')
    monkeypatch.setattr(vite_assets, '_build_id_cache', {})
    assert vite_assets.get_vite_build_id('main') == ''
