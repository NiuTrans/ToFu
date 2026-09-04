"""Guard: explicit health and push probes share the frontend buildId owner.

WHY
---
A browser tab keeps running the vite bundle it was loaded with indefinitely —
the ``vite:preloadError`` reload only fires when a lazy chunk 404s, so a tab
that never hits a missing chunk runs yesterday's JS forever (the "bug fixed
hours ago, user still sees it" class; e.g. the sidebar 今天→昨天→今天 date-
group interleave fix only reached tabs that happened to reload). The client
build watch now receives ``buildId`` on a low-rate push pong; ``/api/health``
retains the field for explicit diagnostics and update UI.

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
    vite_assets.validate_published_vite_artifact()

    body = _call_health()
    assert body['ok'] is True
    assert body.get('buildId') == want, (
        f"buildId={body.get('buildId')!r} != manifest entry basename {want!r}")


def test_get_vite_build_id_matches_manifest():
    from lib import vite_assets

    vite_assets.clear_vite_asset_cache()
    manifest = json.loads(open(vite_assets.VITE_MANIFEST, encoding='utf-8').read())
    want = manifest[vite_assets.VITE_ENTRIES['main']]['file'].rsplit('/', 1)[-1]
    vite_assets.validate_published_vite_artifact()
    assert vite_assets.get_vite_build_id('main') == want


def test_get_vite_build_id_request_path_never_reads_manifest(monkeypatch):
    """Health/push identity stays available when later filesystem I/O wedges."""
    from lib import vite_assets

    vite_assets.clear_vite_asset_cache()
    manifest = vite_assets.validate_published_vite_artifact()
    want = manifest[vite_assets.VITE_ENTRIES['main']]['file'].rsplit('/', 1)[-1]
    monkeypatch.setattr(vite_assets, 'VITE_MANIFEST', '/nonexistent/manifest.json')
    monkeypatch.setattr(
        vite_assets,
        '_load_manifest',
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError('request path reached manifest I/O')),
    )
    assert vite_assets.get_vite_build_id('main') == want
    assert _call_health().get('buildId') == want


def test_get_vite_build_id_is_fail_quiet_before_startup_validation():
    from lib import vite_assets

    vite_assets.clear_vite_asset_cache()
    assert vite_assets.get_vite_build_id('main') == ''


def test_background_build_refresh_submission_is_single_flight(monkeypatch):
    from lib import vite_assets

    class Future:
        def __init__(self):
            self.callback = None

        def add_done_callback(self, callback):
            self.callback = callback

        def result(self):
            return 'main-test.js'

        def settle(self):
            assert self.callback is not None
            self.callback(self)

    class Loop:
        def __init__(self):
            self.futures = []

        def run_in_executor(self, executor, callback):
            assert executor is None
            assert callback is vite_assets.refresh_vite_build_ids
            future = Future()
            self.futures.append(future)
            return future

    loop = Loop()
    monkeypatch.setattr(vite_assets.asyncio, 'get_running_loop', lambda: loop)
    monkeypatch.setattr(vite_assets, '_build_id_refresh_pending', False)

    assert vite_assets.request_vite_build_id_refresh() is True
    assert vite_assets.request_vite_build_id_refresh() is False
    assert len(loop.futures) == 1

    loop.futures[0].settle()
    assert vite_assets.request_vite_build_id_refresh() is True
    assert len(loop.futures) == 2
    loop.futures[1].settle()
