"""B0 — browser bridge user scoping + mandatory non-loopback auth.

Behaviour guards for the security fix described in
``docs/UNIFIED_DEVICE_BRIDGE_DESIGN.md`` §3 / §5.3 / §5.4.

These are BEHAVIOUR guards (charter 2026-07-27): every assertion states an
OUTCOME ("tenant B's command never appears in tenant A's poll response"),
never an implementation detail like "function _deliverable exists". That way
the guard keeps biting after any reasonable rewrite of the queue internals.

Two layers are covered, deliberately:
  * lib level (TestCrossTenantDelivery / TestRegistryTenantIsolation) — the
    queue semantics, driven with EXPLICIT user_id=… arguments;
  * HTTP entry level (TestPollRouteThreadsCallerIdentity) — the real
    /api/browser/poll + /api/browser/commands routes with real per-user
    tokens. Added after pt_3ba97339b4024fb4: the lib-level suite stayed
    12/12 green while the route never resolved nor passed any identity, so
    the gate it "proved" never fired in production.

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

import threading
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



# ═══════════════════════════════════════════════════════════
#  The HTTP ENTRY must thread the resolved caller (§5.3's other half)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestPollRouteThreadsCallerIdentity:
    """Drive the REAL HTTP route — the layer the lib-level classes cannot see.

    Every class above passes ``user_id=…`` to the queue functions EXPLICITLY,
    so they stay green even when the route never resolves nor passes an
    identity — which is exactly what shipped: the route's auth returned a
    bare ``bool``, the registry's ``user_id`` stayed ``''`` forever, and the
    fail-closed first gate compared ``'' == ''`` on every production poll
    (pt_3ba97339b4024fb4). These tests therefore never touch the queue API
    for the ACT under test: registration, polling and result resolution all
    go through ``/api/browser/poll`` (or the legacy GET variant) with real
    per-user tokens, so the entry wiring itself is what passes or fails.

    ⚠️ ``scope_base`` is passed explicitly everywhere (§3.2c): the default
    ``'<local>'`` peer would make any "no credential" assertion a false green.
    """

    SECRET = 'unit-bridge-secret-entry-0123456789'
    PEER = {'client': ('203.0.113.7', 5555)}

    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        # wait_for_commands_async reads POLL_WAIT_TIMEOUT from its own module
        # namespace at call time; shrink the long-poll window for tests.
        monkeypatch.setattr('lib.browser.queue._dispatch.POLL_WAIT_TIMEOUT', 0.3)

    def _make_token(self, scopes=('agents:bridge',), user_id='u-alice',
                    name='bridge-e2e'):
        from lib.api_keys import create_key
        _row, token = create_key(name=name, scopes=list(scopes), user_id=user_id)
        return token

    def _poll(self, client, credential, client_id=None, results=None):
        headers = {'X-Bridge-Secret': credential} if credential else {}
        return client.post('/api/browser/poll',
                           json={'clientId': client_id, 'results': results or []},
                           headers=headers, scope_base=self.PEER)

    def _get_commands(self, client, credential, client_id=None):
        headers = {'X-Bridge-Secret': credential} if credential else {}
        qs = '?clientId=%s' % client_id if client_id else ''
        return client.get('/api/browser/commands' + qs,
                          headers=headers, scope_base=self.PEER)

    def test_per_user_token_accepted_and_registers_caller_identity(
            self, flask_client, monkeypatch):
        """Ticket consequence ②: an agents:bridge token must be ACCEPTED by
        the browser bridge (previously 401 at the route even though the
        global gate had already approved it) and its user_id must reach the
        client registry."""
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', self.SECRET)
        token = self._make_token(user_id=ALICE, name='e2e-register')
        resp = self._poll(flask_client, token, 'alice-client')
        assert resp.status_code == 200, (
            'per-user bridge token rejected at the browser route: %s'
            % resp.get_json())
        from lib.browser.queue._registry import client_user_id
        assert client_user_id('alice-client') == ALICE, (
            'route authenticated the token but dropped its identity')

    def test_global_secret_registers_unscoped(self, flask_client, monkeypatch):
        """Parity guard: the legacy super-user path is byte-unchanged."""
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', self.SECRET)
        resp = self._poll(flask_client, self.SECRET, 'ops-client')
        assert resp.status_code == 200
        from lib.browser.queue._registry import client_user_id
        assert client_user_id('ops-client') == ''

    def test_bobs_command_never_reaches_alices_http_poll(
            self, flask_client, monkeypatch):
        """THE §5.3 acceptance, end-to-end: a command the server aimed at
        Bob's registered browser must never appear in ANY poll response
        authenticated as Alice — including the anonymous-shaped poll where
        the target-client check is vacuous and the user gate is the ONLY
        thing standing between her and Bob's command."""
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', self.SECRET)
        alice = self._make_token(user_id=ALICE, name='e2e-alice')
        bob = self._make_token(user_id=BOB, name='e2e-bob')
        # Both extensions register over the real route with their own tokens.
        assert self._poll(flask_client, alice, 'alice-client').status_code == 200
        assert self._poll(flask_client, bob, 'bob-client').status_code == 200

        from lib.browser.queue import send_browser_command
        box = {}

        def _send():
            # The REAL enqueue entry an LLM tool call takes; its user_id is
            # derived from the registry — the chain under test.
            box['out'] = send_browser_command(
                'get_cookies', {'url': 'https://example.test'},
                timeout=5, client_id='bob-client')

        t = threading.Thread(target=_send, daemon=True)
        t.start()
        time.sleep(0.2)  # let the enqueue land before Alice polls

        # Alice's addressed poll: nothing for her.
        ra = self._poll(flask_client, alice, 'alice-client')
        assert ra.status_code == 200
        assert ra.get_json()['commands'] == [], (
            'cross-tenant leak to an addressed poll: %r' % ra.get_json())
        # Alice's anonymous-shaped poll (legacy client, no clientId). With
        # client_id=None the target check is vacuous — this is the leg that
        # ONLY the user gate can stop, and the one a stripped user_id turns
        # into a live cross-tenant delivery.
        ra2 = self._poll(flask_client, alice, None)
        assert ra2.get_json()['commands'] == [], (
            'cross-tenant leak to an anonymous poll — the user gate never '
            'fired: %r' % ra2.get_json())

        # Bob's own poll receives the command…
        rb = self._poll(flask_client, bob, 'bob-client')
        cmds = rb.get_json()['commands']
        assert [c['type'] for c in cmds] == ['get_cookies'], (
            'same-tenant delivery broken: %r' % cmds)
        assert set(cmds[0].keys()) == {'id', 'type', 'params'}, (
            'user_id leaked onto the wire: %r' % (cmds[0],))
        # …and his extension's result unblocks the sender thread.
        rb2 = self._poll(flask_client, bob, 'bob-client',
                         results=[{'id': cmds[0]['id'],
                                   'result': {'ok': True}, 'error': None}])
        assert rb2.status_code == 200
        t.join(timeout=6)
        assert not t.is_alive(), 'sender thread never unblocked'
        assert box.get('out') == ({'ok': True}, None)

    def test_legacy_get_commands_route_is_scoped(self, flask_client, monkeypatch):
        """The GET variant is a SECOND poll entry — it must carry the same
        identity or the gate has exactly the hole this ticket closed."""
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', self.SECRET)
        alice = self._make_token(user_id=ALICE, name='e2e-get-alice')
        bob = self._make_token(user_id=BOB, name='e2e-get-bob')
        assert self._poll(flask_client, bob, 'bob-client').status_code == 200
        _enqueue('get_cookies', client_id='bob-client', user_id=BOB)

        ra = self._get_commands(flask_client, alice, None)
        assert ra.status_code == 200
        assert ra.get_json()['commands'] == [], (
            'legacy GET route leaked Bob\'s command to Alice: %r' % ra.get_json())
        rb = self._get_commands(flask_client, bob, 'bob-client')
        assert [c['type'] for c in rb.get_json()['commands']] == ['get_cookies']

    def test_token_without_bridge_scope_rejected_over_http(
            self, flask_client, monkeypatch):
        """Invariant pin: a valid but scope-less key never reaches the bridge."""
        monkeypatch.setenv('TOFU_BRIDGE_SECRET', self.SECRET)
        token = self._make_token(scopes=('chat',), user_id=ALICE, name='e2e-chat-only')
        resp = self._poll(flask_client, token, 'alice-client')
        assert resp.status_code == 401


# ═══════════════════════════════════════════════════════════
#  Anti-drift ratchet (supplements — never replaces — the outcome guards)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEntryWiringRatchet:
    """The 12-green lib suite above was structurally blind to the route never
    passing user_id. These ratchets pin the WIRING so a future fourth call
    site (or a re-copied resolver) turns red instead of silently reopening
    the gate. Behaviour stays pinned by the e2e class above."""

    def _queue_call_sites(self):
        import ast
        import inspect

        import routes.browser as rb
        tree = ast.parse(inspect.getsource(rb))
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = (fn.attr if isinstance(fn, ast.Attribute)
                        else getattr(fn, 'id', ''))
                if name in ('mark_poll', 'wait_for_commands',
                            'wait_for_commands_async'):
                    calls.append((name, {k.arg for k in node.keywords}))
        return calls

    def test_every_queue_call_site_carries_user_id(self):
        calls = self._queue_call_sites()
        assert calls, 'ratchet blind: no queue call sites found in routes/browser.py'
        missing = [name for name, kws in calls if 'user_id' not in kws]
        assert not missing, (
            'route call sites dropped the resolved caller identity: %r' % missing)

    def test_both_bridges_share_one_caller_resolver(self):
        """「两条桥的身份层真正是同一个东西」made testable: both routes must
        resolve the caller through the SAME function object, so the browser
        bridge can never again drift back to a bool-only hand copy."""
        import routes.browser as rb
        import routes.desktop as rd
        assert rb._resolve_bridge_caller is rd._resolve_bridge_caller, (
            'the browser and desktop routes resolve bridge identity through '
            'different implementations again')
