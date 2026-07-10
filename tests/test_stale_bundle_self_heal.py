"""Stale content-hashed bundle self-heal (server.py `_handle_404`).

A client holding an old ``index.html`` (bfcache / long-lived tab / caching
proxy defeating ``no-cache``) requests a ``bundle-<hash>.js`` whose hash was
deleted by ``_clean_old_bundles`` on the last rebuild → 404 → LoadGuard banner.
The 404 handler redirects such a request to the CURRENT bundle so the stale
page self-heals with zero user action.

These tests pin the four correctness properties from the design:
  1. A fabricated stale ``bundle-``/``feature-`` hash → 302 to the current one.
  2. A genuinely-unknown ``/static/js/`` path still 404s (miss handler is NOT a
     catch-all masking real 404s).
  3. The current-hash request still gets ``immutable`` long-cache.
  4. The stale-redirect response is ``no-store`` (never frozen immutable — the
     mapping changes on every rebuild).
"""
import pytest

from lib.js_bundler import (
    build_bundle,
    get_feature_bundle_filename,
    resolve_stale_bundle,
)

pytestmark = pytest.mark.unit


def _current_core():
    name = build_bundle()
    assert name, 'core bundle must build in the test env'
    return name


def test_stale_core_hash_redirects_to_current(flask_client):
    current = _current_core()
    # Fabricate a plausibly-old hash that is NOT the current one.
    stale = 'bundle-95e8203d.js'
    assert stale != current
    resp = flask_client.get('/static/js/' + stale)
    assert resp.status_code == 302
    loc = resp.headers.get('Location', '')
    assert loc.endswith('/static/js/' + current), loc


def test_stale_feature_hash_redirects_to_current(flask_client):
    _current_core()
    current_feat = get_feature_bundle_filename()
    if not current_feat:
        # No deferred bundle in this build — nothing to self-heal; skip.
        pytest.skip('no deferred feature bundle in this env')
    stale = 'feature-00000000.js'
    assert stale != current_feat
    resp = flask_client.get('/static/js/' + stale)
    assert resp.status_code == 302
    assert resp.headers.get('Location', '').endswith('/static/js/' + current_feat)


def test_unknown_static_js_still_404s(flask_client):
    """A non-bundle miss must NOT be masked by the self-heal path."""
    _current_core()
    resp = flask_client.get('/static/js/definitely-not-a-real-file.js')
    assert resp.status_code == 404


def test_non_hashed_bundle_name_still_404s(flask_client):
    """A name that isn't a built <8hex> bundle must not redirect."""
    _current_core()
    # 'bundle-loader.js' looks bundle-ish but is not the <8hex> built pattern.
    resp = flask_client.get('/static/js/bundle-loader.js')
    assert resp.status_code == 404


def test_current_bundle_stays_immutable(flask_client):
    current = _current_core()
    resp = flask_client.get('/static/js/' + current)
    assert resp.status_code == 200
    cc = resp.headers.get('Cache-Control', '')
    assert 'immutable' in cc and 'max-age=31536000' in cc, cc


def test_stale_redirect_is_not_cached(flask_client):
    current = _current_core()
    stale = 'bundle-95e8203d.js'
    assert stale != current
    resp = flask_client.get('/static/js/' + stale)
    assert resp.status_code == 302
    cc = resp.headers.get('Cache-Control', '')
    assert 'no-store' in cc, cc
    assert 'immutable' not in cc, cc


def test_resolver_is_pure_and_precise():
    """Unit-level guard on resolve_stale_bundle's classification."""
    current = _current_core()
    # Current file → None (serve normally, don't redirect).
    assert resolve_stale_bundle(current) is None
    # Stale but well-formed built name → current.
    assert resolve_stale_bundle('bundle-95e8203d.js') == current
    # Not a built-bundle name → None (real 404).
    assert resolve_stale_bundle('core.js') is None
    assert resolve_stale_bundle('bundle-loader.js') is None
    assert resolve_stale_bundle('') is None
    assert resolve_stale_bundle(None) is None
