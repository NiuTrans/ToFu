"""tests/test_browser_ext_fleet_registry.py — the stranded-fleet contract.

WHY
---
2026-08-04 owner review: the zero-config re-pair batch shipped its self-heal
INSIDE the new extension — but the already-installed fleet (the owner's own
v4.3, parked at 401 ×279) has no update channel and cannot poll, so nothing
reached it. The panel could not even tell "installed but locked out" from
"never installed": both rendered as 尚未安装, a lie of omission.

The chain pinned here:

  * the extension reports its manifest version on every poll
    (``extVersion``) — pinned structurally in
    tests/test_browser_bridge_auto_repair.py;
  * ``mark_poll`` stores it and ``get_connected_clients`` carries it, so
    the panel can diff each client against the version the server would
    serve (``servedExtVersion``, read from the on-disk manifest);
  * a poll that DIES at the bridge-auth gate records the client into the
    locked-out registry (small, TTL-bound, capacity-capped) — Tofu's own
    401 can only mean a stale credential, i.e. the stranded fleet's
    distress signal;
  * a SUCCESSFUL poll clears the note (the preseeded re-download arrived);
  * ``GET /api/v1/browser/status`` exposes both fleet inputs.
"""

import asyncio
import json
import os
import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ROUTE_TOKEN = '_fleet_test_token__'


@pytest.fixture()
def _clean_fleet():
    """Isolate the process-global registries from the rest of the suite."""
    from lib.browser.queue._state import (
        _clients, _clients_lock, _locked_out, _locked_out_lock,
    )
    with _clients_lock:
        _clients.clear()
    with _locked_out_lock:
        _locked_out.clear()
    yield
    with _clients_lock:
        _clients.clear()
    with _locked_out_lock:
        _locked_out.clear()


# ── 1. The registry itself ─────────────────────────────────────────────

def test_mark_poll_stores_and_reports_the_extension_version(_clean_fleet):
    from lib.browser import get_connected_clients, mark_poll
    mark_poll('client-a', chrome_major=140, user_id='u1', ext_version='4.7.0')
    rows = get_connected_clients()
    assert len(rows) == 1
    assert rows[0]['ext_version'] == '4.7.0', (
        'the connected-client payload must carry ext_version — without it '
        'the panel cannot tell an outdated-but-working install')
    mark_poll('client-a', ext_version='4.7.1')   # a later poll may refresh it
    assert get_connected_clients()[0]['ext_version'] == '4.7.1'


def test_locked_out_record_and_read(_clean_fleet):
    from lib.browser import get_locked_out_clients, mark_locked_out
    mark_locked_out('dead-client', ext_version='4.3.0')
    rows = get_locked_out_clients()
    assert len(rows) == 1
    row = rows[0]
    assert row['client_id'] == 'dead-client'
    assert row['ext_version'] == '4.3.0'
    assert row['fail_count'] == 1
    mark_locked_out('dead-client', ext_version='4.3.0')
    assert get_locked_out_clients()[0]['fail_count'] == 2, (
        'repeated knocks from the same stranded client must count, not '
        'duplicate')


def test_locked_out_anonymous_knocks_are_not_recorded(_clean_fleet):
    from lib.browser import get_locked_out_clients, mark_locked_out
    mark_locked_out(None, ext_version='')
    assert get_locked_out_clients() == [], (
        'a knock with no clientId cannot be attributed — recording it '
        'would fabricate a phantom stranded install')


def test_locked_out_ttl_expires_stale_notes(_clean_fleet, monkeypatch):
    from lib.browser import get_locked_out_clients, mark_locked_out
    from lib.browser.queue import _registry
    mark_locked_out('dead-client', ext_version='4.3.0')
    assert len(get_locked_out_clients()) == 1
    monkeypatch.setattr(_registry, '_LOCKED_OUT_TTL_S', 0)
    assert get_locked_out_clients() == [], (
        'a note whose stranded client stopped knocking must expire — an '
        'immortal note would cry wolf forever after the user moved on')


def test_locked_out_registry_is_capacity_capped(_clean_fleet):
    from lib.browser import get_locked_out_clients, mark_locked_out
    from lib.browser.queue import _registry
    for i in range(_registry._LOCKED_OUT_MAX + 8):
        mark_locked_out('flood-%02d' % i)
        time.sleep(0.001)   # distinct last_seen ordering
    assert len(get_locked_out_clients()) <= _registry._LOCKED_OUT_MAX, (
        'the registry must never grow without bound — a credential-scan '
        'flood must not become a memory leak')


def test_a_successful_poll_clears_the_locked_out_note(_clean_fleet):
    """THE self-heal: the re-downloaded (preseeded) extension polls OK, and
    the stranded note disappears on its own — no panel bookkeeping."""
    from lib.browser import (
        get_locked_out_clients, mark_locked_out, mark_poll,
    )
    mark_locked_out('dead-client', ext_version='4.3.0')
    assert len(get_locked_out_clients()) == 1
    mark_poll('dead-client', ext_version='4.7.0')
    assert get_locked_out_clients() == [], (
        'a client that polls successfully is no longer stranded — the note '
        'must clear itself')


# ── 2. The poll route records stranded knocks ──────────────────────────

def _post_poll(secret, body):
    """Drive the REAL /api/browser/poll in a bare Quart app (no global gate)."""
    from quart import Quart
    from routes.browser import browser_bp

    app = Quart(__name__)
    app.register_blueprint(browser_bp)

    async def _go():
        client = app.test_client()
        resp = await client.post('/api/browser/poll', json=body,
                                 headers={'X-Bridge-Secret': secret})
        return resp.status_code

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_go())
    finally:
        loop.close()


@pytest.fixture()
def _isolated_key_store(tmp_path):
    with patch('lib.api_keys._STORE_PATH',
               os.path.join(str(tmp_path), 'api_keys.json')):
        from lib import api_keys
        api_keys._cache.clear()
        api_keys._cache_loaded = False
        yield
        api_keys._cache.clear()
        api_keys._cache_loaded = False


def test_a_401_poll_records_who_knocked(_clean_fleet, _isolated_key_store,
                                        monkeypatch):
    """The stranded fleet's distress signal: Tofu's own 401 can only mean a
    stale credential, and the body already carries the client's identity."""
    from lib.browser import get_locked_out_clients
    monkeypatch.setenv('TOFU_BRIDGE_SECRET', 'the-real-secret')
    status = _post_poll('stale-or-revoked-key',
                        {'clientId': 'old-ext-1', 'extVersion': '4.3.0',
                         'results': []})
    assert status == 401
    rows = get_locked_out_clients()
    assert [r['client_id'] for r in rows] == ['old-ext-1'], (
        f'the 401 poll must record the stranded client, got {rows!r}')
    assert rows[0]['ext_version'] == '4.3.0'


def test_a_401_poll_without_a_body_never_crashes(_clean_fleet,
                                                 _isolated_key_store,
                                                 monkeypatch):
    from lib.browser import get_locked_out_clients
    monkeypatch.setenv('TOFU_BRIDGE_SECRET', 'the-real-secret')
    status = _post_poll('stale', {})
    assert status == 401
    assert get_locked_out_clients() == [], (
        'an anonymous rejected poll records nothing (and must not 500)')


# ── 3. The status endpoint exposes both fleet inputs ───────────────────

def test_status_exposes_served_version_and_locked_out_list(flask_client,
                                                           monkeypatch,
                                                           _clean_fleet):
    from lib.browser import mark_locked_out
    mark_locked_out('dead-client', ext_version='4.3.0')
    monkeypatch.setenv('TUNNEL_TOKEN', _ROUTE_TOKEN)
    resp = flask_client.get(
        '/api/v1/browser/status',
        headers={'X-Tunnel-Token': _ROUTE_TOKEN},
        scope_base={'client': ('127.0.0.1', 5555)})
    assert resp.status_code == 200
    body = resp.get_json(silent=True) or {}
    with open(os.path.join(REPO, 'browser_extension', 'manifest.json'),
              encoding='utf-8') as f:
        served = json.load(f)['version']
    assert body.get('servedExtVersion') == served, (
        f"the panel needs the version a fresh download carries — got "
        f"{body.get('servedExtVersion')!r}, manifest says {served!r}")
    locked = body.get('lockedOutClients') or []
    assert any(r.get('client_id') == 'dead-client' for r in locked), (
        f'the stranded client must surface in the status payload: {locked!r}')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
