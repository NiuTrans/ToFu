"""B0 — browser bridge user scoping + mandatory non-loopback auth.

Behaviour guards for the security fix described in
``docs/UNIFIED_DEVICE_BRIDGE_DESIGN.md`` §3 / §5.3 / §5.4.

These are BEHAVIOUR guards (charter 2026-07-27): every assertion states an
OUTCOME ("tenant B's command never appears in tenant A's poll response"),
never an implementation detail like "function _deliverable exists". That way
the guard keeps biting after any reasonable rewrite of the queue internals.

Why this suite exists — the asymmetry it closes:
  * the DESKTOP bridge scopes every command to the authenticated bridge user
    (``lib/desktop/bridge.py`` — registration carries ``user_id``, and the
    delivery predicate rejects a mismatch before anything else);
  * the BROWSER bridge had NO user dimension at all, while the extension
    holds ``<all_urls>`` + ``cookies`` + ``debugger``. On a tunnelled or
    multi-tenant deployment that made ``/api/browser/poll`` a browser-session
    takeover primitive.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_browser_user_scope.py -v
"""
from __future__ import annotations

import time

import pytest


ALICE = 'user-alice'
BOB = 'user-bob'


@pytest.fixture(autouse=True)
def _clean_queue():
    """Isolate every test from the process-wide queue/registry singletons."""
    from lib.browser.queue import _state
    with _state._commands_lock:
        _state._commands.clear()
    with _state._clients_lock:
        _state._clients.clear()
    _state._last_poll_time = 0
    yield
    with _state._commands_lock:
        _state._commands.clear()
    with _state._clients_lock:
        _state._clients.clear()


def _register(client_id, user_id=''):
    """Register a polling extension client as some bridge user."""
    from lib.browser.queue import mark_poll
    mark_poll(client_id, user_id=user_id)


def _enqueue(cmd_type, client_id=None, user_id=''):
    """Put a command on the queue without blocking on its result."""
    from lib.browser.queue import _state
    import uuid
    import threading
    cmd_id = str(uuid.uuid4())
    with _state._commands_lock:
        _state._commands[cmd_id] = {
            'id': cmd_id, 'type': cmd_type, 'params': {},
            'event': threading.Event(), 'result': None, 'error': None,
            'created_at': time.time(), 'picked_up': False,
            'target_client': client_id, 'timeout': 30, 'cancelled': False,
            'user_id': user_id,
        }
    return cmd_id


# ═══════════════════════════════════════════════════════════
#  Cross-tenant delivery must be impossible (§5.3)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestCrossTenantDelivery:
    """The core outcome: one tenant's command never reaches another's poll."""

    def test_other_tenants_command_not_delivered(self):
        """Bob's command must NOT appear in Alice's poll response."""
        from lib.browser.queue import get_pending_commands
        _register('alice-client', user_id=ALICE)
        _register('bob-client', user_id=BOB)
        _enqueue('get_cookies', client_id='bob-client', user_id=BOB)

        got = get_pending_commands(client_id='alice-client', user_id=ALICE)
        assert got == [], (
            'cross-tenant leak: Alice received Bob\'s command %r' % got)

    def test_own_tenant_command_is_delivered(self):
        """The scoping must not break the legitimate same-tenant path."""
        from lib.browser.queue import get_pending_commands
        _register('alice-client', user_id=ALICE)
        _enqueue('list_tabs', client_id='alice-client', user_id=ALICE)

        got = get_pending_commands(client_id='alice-client', user_id=ALICE)
        assert [c['type'] for c in got] == ['list_tabs']

    def test_unaddressed_command_stays_within_tenant(self):
        """An unrouted command is still confined to its own tenant.

        Unaddressed commands are the dangerous case: without a user gate the
        'any client can pick it up' rule hands them to whoever polls first.
        """
        from lib.browser.queue import get_pending_commands
        _register('alice-client', user_id=ALICE)
        _register('bob-client', user_id=BOB)
        _enqueue('get_cookies', client_id=None, user_id=BOB)

        assert get_pending_commands(client_id='alice-client', user_id=ALICE) == []
        got = get_pending_commands(client_id='bob-client', user_id=BOB)
        assert [c['type'] for c in got] == ['get_cookies']

    def test_legacy_unscoped_world_is_byte_identical(self):
        """Both sides empty = single-user deployment: unchanged behaviour."""
        from lib.browser.queue import get_pending_commands
        _register('solo-client', user_id='')
        _enqueue('list_tabs', client_id='solo-client', user_id='')

        got = get_pending_commands(client_id='solo-client', user_id='')
        assert [c['type'] for c in got] == ['list_tabs']

    def test_user_id_never_crosses_the_wire(self):
        """The wire projection stays {id,type,params} — user_id is internal."""
        from lib.browser.queue import get_pending_commands
        _register('alice-client', user_id=ALICE)
        _enqueue('list_tabs', client_id='alice-client', user_id=ALICE)

        got = get_pending_commands(client_id='alice-client', user_id=ALICE)
        assert len(got) == 1
        assert set(got[0].keys()) == {'id', 'type', 'params'}, (
            'wire shape drifted / leaked internals: %r' % (got[0],))


# ═══════════════════════════════════════════════════════════
#  Registry carries the tenant (§5.3)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRegistryTenantIsolation:

    def test_connected_clients_filtered_by_user(self):
        """A tenant must never see another tenant's devices."""
        from lib.browser.queue import get_connected_clients
        _register('alice-client', user_id=ALICE)
        _register('bob-client', user_id=BOB)

        alice_ids = {c['client_id'] for c in get_connected_clients(user_id=ALICE)}
        assert alice_ids == {'alice-client'}, (
            'tenant isolation broken, Alice sees: %r' % alice_ids)

    def test_operator_view_sees_all(self):
        """user_id=None is the unfiltered operator view."""
        from lib.browser.queue import get_connected_clients
        _register('alice-client', user_id=ALICE)
        _register('bob-client', user_id=BOB)

        all_ids = {c['client_id'] for c in get_connected_clients()}
        assert all_ids == {'alice-client', 'bob-client'}


# ═══════════════════════════════════════════════════════════
#  Auth is mandatory off-loopback (§3.4 / §5.4)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestBridgeAuthIsCredentialNotAddress:
    """The bridge must gate on a CREDENTIAL, never on how the peer's IP looks.

    ⚠️ Every test here passes ``scope_base={'client': (...)}`` explicitly.
    That is MANDATORY, not stylistic: Quart's in-process test client reports
    the peer as the literal ``'<local>'``, which ``_remote_is_loopback()``
    treats as loopback — so a request with NO scope_base silently takes the
    exemption path and any "no credential → 200" assertion here would be a
    FALSE GREEN. See docs/UNIFIED_DEVICE_BRIDGE_DESIGN.md §3.2c.

    Why address-based exemption is wrong (§3.2b): the standard tunnel
    deployment is nginx / ngrok / cloudflared on the SAME host reverse-proxying
    to 127.0.0.1, so ``remote_addr`` is 127.0.0.1 for every request that
    arrives from the public internet. ProxyFix is never installed
    (TOFU_TRUST_PROXY_HOPS is documentation-only), so the server cannot see
    the real client address at all.
    """

    def _poll(self, client, peer, headers=None):
        return client.post('/api/browser/poll', json={}, headers=headers or {},
                           scope_base={'client': (peer, 5555)})

    def test_public_peer_rejected_without_credential(self, flask_client, monkeypatch):
        monkeypatch.delenv('TOFU_BRIDGE_SECRET', raising=False)
        resp = self._poll(flask_client, '203.0.113.7')
        assert resp.status_code == 401, (
            'unauthenticated remote poll ACCEPTED — session-takeover exposure')

    def test_loopback_peer_without_credential_is_also_rejected(
            self, flask_client, monkeypatch):
        """The decisive case: a loopback-LOOKING peer earns nothing.

        Under a same-host reverse proxy this is exactly what a public
        attacker's request looks like, so granting it the exemption is
        equivalent to granting the whole internet.
        """
        monkeypatch.delenv('TOFU_BRIDGE_SECRET', raising=False)
        resp = self._poll(flask_client, '127.0.0.1')
        assert resp.status_code == 401, (
            'loopback-shaped peer got in WITHOUT a credential — under a '
            'same-host reverse proxy this is the public internet (§3.2b)')

    def test_ipv6_loopback_peer_without_credential_is_also_rejected(
            self, flask_client, monkeypatch):
        monkeypatch.delenv('TOFU_BRIDGE_SECRET', raising=False)
        resp = self._poll(flask_client, '::1')
        assert resp.status_code == 401

    def test_remote_peer_with_shared_secret_accepted(self, flask_client, monkeypatch):
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', 'unit-secret-abcdef0123456789')
        resp = self._poll(flask_client, '203.0.113.7',
                          headers={'X-Bridge-Secret': 'unit-secret-abcdef0123456789'})
        assert resp.status_code == 200, (
            'a correct credential must be accepted regardless of peer address')

    def test_allow_remote_env_cannot_downgrade_the_bridge(
            self, flask_client, monkeypatch):
        """Invariant (§3.4b): TOFU_OPEN_MODE_ALLOW_REMOTE opens the plain UI
        for remote access — it must NEVER hand out the bridge, which can read
        cookies, attach CDP, write files and run shell commands.
        """
        monkeypatch.delenv('TOFU_BRIDGE_SECRET', raising=False)
        monkeypatch.setenv('TOFU_OPEN_MODE_ALLOW_REMOTE', '1')
        resp = self._poll(flask_client, '203.0.113.7')
        assert resp.status_code == 401, (
            'TOFU_OPEN_MODE_ALLOW_REMOTE downgraded the BRIDGE to '
            'credential-free — that variable must only affect the plain UI')
