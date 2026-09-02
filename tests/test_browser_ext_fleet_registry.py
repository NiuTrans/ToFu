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

pytest_plugins = ('tests._credential_sidecar',)

import asyncio
import json
import os
import time
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OWNER = '101'


def _mark_poll(client_id, *, ext_version=''):
    from lib.browser.queue import mark_poll
    mark_poll(
        client_id,
        owner_user_id=OWNER,
        protocol_version=2,
        capabilities=[],
        ext_version=ext_version,
    )


@pytest.fixture()
def _clean_fleet():
    """Isolate the process-global registries from the rest of the suite."""
    from lib.browser.queue._state import (
        _clients, _clients_lock, _locked_out, _locked_out_lock,
        _incompatible_clients, _incompatible_clients_lock,
    )
    with _clients_lock:
        _clients.clear()
    with _locked_out_lock:
        _locked_out.clear()
    with _incompatible_clients_lock:
        _incompatible_clients.clear()
    yield
    with _clients_lock:
        _clients.clear()
    with _locked_out_lock:
        _locked_out.clear()
    with _incompatible_clients_lock:
        _incompatible_clients.clear()


# ── 1. The registry itself ─────────────────────────────────────────────

def test_mark_poll_stores_and_reports_the_extension_version(_clean_fleet):
    from lib.browser.queue import get_connected_clients, mark_poll
    mark_poll(
        'client-a', chrome_major=140, owner_user_id=OWNER,
        protocol_version=2, capabilities=[], ext_version='4.7.0')
    rows = get_connected_clients(owner_user_id=OWNER)
    assert len(rows) == 1
    assert rows[0]['ext_version'] == '4.7.0', (
        'the connected-client payload must carry ext_version — without it '
        'the panel cannot tell an outdated-but-working install')
    _mark_poll('client-a', ext_version='4.7.1')
    assert get_connected_clients(
        owner_user_id=OWNER)[0]['ext_version'] == '4.7.1'


def test_locked_out_record_and_read(_clean_fleet):
    from lib.browser.queue import get_locked_out_clients, mark_locked_out
    mark_locked_out(
        'dead-client', owner_user_id=OWNER, ext_version='4.3.0')
    rows = get_locked_out_clients(owner_user_id=OWNER)
    assert len(rows) == 1
    row = rows[0]
    assert row['client_id'] == 'dead-client'
    assert row['ext_version'] == '4.3.0'
    assert row['fail_count'] == 1
    assert get_locked_out_clients(owner_user_id='202') == []
    mark_locked_out(
        'dead-client', owner_user_id=OWNER, ext_version='4.3.0')
    assert get_locked_out_clients(
        owner_user_id=OWNER)[0]['fail_count'] == 2, (
        'repeated knocks from the same stranded client must count, not '
        'duplicate')


def test_locked_out_anonymous_knocks_are_not_recorded(_clean_fleet):
    from lib.browser.queue import get_locked_out_clients, mark_locked_out
    mark_locked_out(None, owner_user_id=OWNER, ext_version='')
    assert get_locked_out_clients(owner_user_id=OWNER) == [], (
        'a knock with no clientId cannot be attributed — recording it '
        'would fabricate a phantom stranded install')


def test_locked_out_ttl_expires_stale_notes(_clean_fleet, monkeypatch):
    from lib.browser.queue import get_locked_out_clients, mark_locked_out
    from lib.browser.queue import _registry
    mark_locked_out(
        'dead-client', owner_user_id=OWNER, ext_version='4.3.0')
    assert len(get_locked_out_clients(owner_user_id=OWNER)) == 1
    monkeypatch.setattr(_registry, '_LOCKED_OUT_TTL_S', 0)
    assert get_locked_out_clients(owner_user_id=OWNER) == [], (
        'a note whose stranded client stopped knocking must expire — an '
        'immortal note would cry wolf forever after the user moved on')


def test_locked_out_registry_is_capacity_capped(_clean_fleet):
    from lib.browser.queue import get_locked_out_clients, mark_locked_out
    from lib.browser.queue import _registry
    for i in range(_registry._LOCKED_OUT_MAX + 8):
        mark_locked_out('flood-%02d' % i, owner_user_id=OWNER)
        time.sleep(0.001)   # distinct last_seen ordering
    assert len(get_locked_out_clients(
        owner_user_id=OWNER)) <= _registry._LOCKED_OUT_MAX, (
        'the registry must never grow without bound — a credential-scan '
        'flood must not become a memory leak')


def test_a_successful_poll_clears_the_locked_out_note(_clean_fleet):
    """THE self-heal: the re-downloaded (preseeded) extension polls OK, and
    the stranded note disappears on its own — no panel bookkeeping."""
    from lib.browser.queue import get_locked_out_clients, mark_locked_out
    mark_locked_out(
        'dead-client', owner_user_id=OWNER, ext_version='4.3.0')
    assert len(get_locked_out_clients(owner_user_id=OWNER)) == 1
    _mark_poll('dead-client', ext_version='4.7.0')
    assert get_locked_out_clients(owner_user_id=OWNER) == [], (
        'a client that polls successfully is no longer stranded — the note '
        'must clear itself')


def test_incompatible_record_is_owner_scoped_bounded_and_self_clearing(
        _clean_fleet):
    from lib.browser.queue import (
        get_incompatible_clients,
        mark_incompatible_client,
    )
    first = mark_incompatible_client(
        'old-protocol', owner_user_id=OWNER, ext_version='5.0.0',
        protocol_version=1, reason='Browser protocol 2 is required')
    duplicate = mark_incompatible_client(
        'old-protocol', owner_user_id=OWNER, ext_version='5.0.0',
        protocol_version=1, reason='Browser protocol 2 is required')
    assert first is True and duplicate is False, (
        'only the first identical rejection should request a durable warning')
    rows = get_incompatible_clients(owner_user_id=OWNER)
    assert len(rows) == 1
    assert rows[0]['client_id'] == 'old-protocol'
    assert rows[0]['ext_version'] == '5.0.0'
    assert rows[0]['protocol_version'] == 1
    assert rows[0]['reason'] == 'Browser protocol 2 is required'
    assert rows[0]['fail_count'] == 2
    assert rows[0]['seconds_ago'] >= 0
    assert get_incompatible_clients(owner_user_id='202') == []
    _mark_poll('old-protocol', ext_version='5.3.0')
    assert get_incompatible_clients(owner_user_id=OWNER) == [], (
        'a current successful handshake must clear the upgrade recovery note')


# ── 2. The poll route records stranded knocks ──────────────────────────

def _post_poll(secret, body, *, include_body=False):
    """Drive the REAL /api/browser/poll in a bare Quart app (no global gate)."""
    from quart import Quart
    from routes.browser import browser_bp

    app = Quart(__name__)
    app.register_blueprint(browser_bp)

    async def _go():
        client = app.test_client()
        resp = await client.post('/api/browser/poll', json=body,
                                 headers={'X-Bridge-Secret': secret})
        if include_body:
            return resp.status_code, await resp.get_json()
        return resp.status_code

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_go())
    finally:
        loop.close()


@pytest.fixture()
def _isolated_key_store(tmp_path):
    """Credential authority is isolated by the module Sidecar fixture."""
    yield


def test_a_401_poll_records_who_knocked(_clean_fleet, _isolated_key_store,
                                        monkeypatch):
    """The stranded fleet's distress signal: Tofu's own 401 can only mean a
    stale credential, and the body already carries the client's identity."""
    from lib.api_keys import create_key, revoke_key
    from lib.browser.queue import get_locked_out_clients
    row, token = create_key(
        name='revoked-browser', scopes=['agents:bridge'],
        owner_user_id=int(OWNER))
    assert revoke_key(row['id'], owner_user_id=int(OWNER))
    status = _post_poll(token,
                        {'clientId': 'old-ext-1', 'extVersion': '4.3.0',
                         'results': []})
    assert status == 401
    rows = get_locked_out_clients(owner_user_id=OWNER)
    assert [r['client_id'] for r in rows] == ['old-ext-1'], (
        f'the 401 poll must record the stranded client, got {rows!r}')
    assert rows[0]['ext_version'] == '4.3.0'


def test_a_401_poll_without_a_body_never_crashes(_clean_fleet,
                                                 _isolated_key_store,
                                                 monkeypatch):
    from lib.browser.queue import get_locked_out_clients
    status = _post_poll('stale', {})
    assert status == 401
    assert get_locked_out_clients(owner_user_id=OWNER) == [], (
        'an anonymous rejected poll records nothing (and must not 500)')


def test_an_authenticated_old_protocol_poll_returns_426_and_is_recoverable(
        _clean_fleet, _isolated_key_store):
    from lib.api_keys import create_key
    from lib.browser.queue import (
        get_connected_clients,
        get_incompatible_clients,
    )
    _row, token = create_key(
        name='old-protocol-browser', scopes=['agents:bridge'],
        owner_user_id=int(OWNER))
    status, payload = _post_poll(
        token,
        {
            'clientId': 'old-protocol',
            'extVersion': '5.0.0',
            'protocolVersion': 1,
            'capabilities': [],
            'results': [],
        },
        include_body=True,
    )
    assert status == 426
    assert payload['code'] == 'browser_protocol_upgrade_required'
    assert payload['requiredProtocolVersion'] == 2
    assert get_connected_clients(owner_user_id=OWNER) == [], (
        'a rejected protocol must never enter the command authority registry')
    rows = get_incompatible_clients(owner_user_id=OWNER)
    assert [row['client_id'] for row in rows] == ['old-protocol']
    assert rows[0]['protocol_version'] == 1


# ── 3. The status endpoint exposes only its owner's diagnostics ─────────


def test_status_exposes_only_owner_scoped_locked_out_clients(
        flask_client, monkeypatch, _clean_fleet):
    from lib.browser.queue import mark_incompatible_client, mark_locked_out
    mark_locked_out(
        'dead-client', owner_user_id='1', ext_version='4.3.0')
    mark_locked_out(
        'other-users-client', owner_user_id='2', ext_version='4.3.0')
    mark_incompatible_client(
        'upgrade-me', owner_user_id='1', ext_version='5.0.0',
        protocol_version=1, reason='Browser protocol 2 is required')
    mark_incompatible_client(
        'other-users-old-protocol', owner_user_id='2', ext_version='5.0.0',
        protocol_version=1, reason='Browser protocol 2 is required')
    resp = flask_client.get(
        '/api/v1/browser/status',
        scope_base={'client': ('127.0.0.1', 5555)})
    assert resp.status_code == 200
    body = resp.get_json(silent=True) or {}
    with open(os.path.join(REPO, 'browser_extension', 'manifest.json'),
              encoding='utf-8') as f:
        served = json.load(f)['version']
    assert body.get('servedExtVersion') == served, (
        f"the panel needs the version a fresh download carries — got "
        f"{body.get('servedExtVersion')!r}, manifest says {served!r}")
    locked_ids = {
        row.get('client_id')
        for row in body.get('lockedOutClients') or []
    }
    assert locked_ids == {'dead-client'}, (
        'the authenticated owner must see its recoverable rejected poll '
        'without learning another owner\'s browser identity')
    incompatible_ids = {
        row.get('client_id')
        for row in body.get('incompatibleClients') or []
    }
    assert incompatible_ids == {'upgrade-me'}, (
        'protocol recovery diagnostics must be visible only to their owner')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
