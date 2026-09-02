"""Browser bridge owner/device isolation and mandatory bridge auth.

Behaviour guards for the security fix described in
``docs/modules/integrations_api.md`` §3 / §5.3 / §5.4.

These are BEHAVIOUR guards (charter 2026-07-27): every assertion states an
OUTCOME ("tenant B's command never appears in tenant A's poll response"),
never an implementation detail like "function _deliverable exists". That way
the guard keeps biting after any reasonable rewrite of the queue internals.

Two layers are covered: owner/device-addressed queue behavior and the single
current ``POST /api/browser/poll`` transport with real per-owner tokens.

The extension holds powerful browser permissions, so anonymous devices,
unaddressed commands, and implicit owner fallback are intentionally invalid.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_browser_user_scope.py -v
"""
from __future__ import annotations

pytest_plugins = ('tests._credential_sidecar',)

import threading
import time

import pytest


ALICE = '101'
BOB = '202'


@pytest.fixture(autouse=True)
def _clean_queue():
    """Isolate every test from the process-wide queue/registry singletons."""
    from lib.browser.queue import _state
    with _state._commands_lock:
        _state._commands.clear()
    with _state._clients_lock:
        _state._clients.clear()
    yield
    with _state._commands_lock:
        _state._commands.clear()
    with _state._clients_lock:
        _state._clients.clear()


def _register(client_id, owner_user_id=ALICE):
    """Register a current-protocol extension for one authenticated owner."""
    from lib.browser.queue import mark_poll
    mark_poll(
        client_id,
        owner_user_id=owner_user_id,
        protocol_version=2,
        capabilities=[],
    )


def _enqueue(cmd_type, *, client_id, owner_user_id):
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
            'claimed_client_id': '', 'claimed_owner_user_id': '',
            'owner_user_id': owner_user_id,
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
        _register('alice-client', owner_user_id=ALICE)
        _register('bob-client', owner_user_id=BOB)
        _enqueue('get_cookies', client_id='bob-client', owner_user_id=BOB)

        got = get_pending_commands(
            client_id='alice-client', owner_user_id=ALICE)
        assert got == [], (
            'cross-tenant leak: Alice received Bob\'s command %r' % got)

    def test_own_tenant_command_is_delivered(self):
        """The scoping must not break the legitimate same-tenant path."""
        from lib.browser.queue import get_pending_commands
        _register('alice-client', owner_user_id=ALICE)
        _enqueue('list_tabs', client_id='alice-client', owner_user_id=ALICE)

        got = get_pending_commands(
            client_id='alice-client', owner_user_id=ALICE)
        assert [c['type'] for c in got] == ['list_tabs']

    def test_anonymous_and_unaddressed_queue_operations_are_rejected(self):
        """Every queue claim must name a positive owner and stable device."""
        from lib.browser.queue import get_pending_commands, mark_poll
        with pytest.raises(ValueError, match='owner_user_id'):
            mark_poll(
                'anonymous', owner_user_id='', protocol_version=2,
                capabilities=[])
        with pytest.raises(ValueError, match='client_id'):
            get_pending_commands(client_id='', owner_user_id=ALICE)

    def test_user_id_never_crosses_the_wire(self):
        """The wire projection stays {id,type,params} — user_id is internal."""
        from lib.browser.queue import get_pending_commands
        _register('alice-client', owner_user_id=ALICE)
        _enqueue('list_tabs', client_id='alice-client', owner_user_id=ALICE)

        got = get_pending_commands(
            client_id='alice-client', owner_user_id=ALICE)
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
        _register('alice-client', owner_user_id=ALICE)
        _register('bob-client', owner_user_id=BOB)

        alice_ids = {
            c['client_id']
            for c in get_connected_clients(owner_user_id=ALICE)
        }
        assert alice_ids == {'alice-client'}, (
            'tenant isolation broken, Alice sees: %r' % alice_ids)

    def test_operator_view_sees_all(self):
        """owner_user_id=None is the explicit unfiltered operator view."""
        from lib.browser.queue import get_connected_clients
        _register('alice-client', owner_user_id=ALICE)
        _register('bob-client', owner_user_id=BOB)

        all_ids = {
            c['client_id']
            for c in get_connected_clients(owner_user_id=None)
        }
        assert all_ids == {'alice-client', 'bob-client'}


# ═══════════════════════════════════════════════════════════
#  The HTTP ENTRY must thread the resolved caller (§5.3's other half)
# ═══════════════════════════════════════════════════════════

@pytest.mark.api
class TestPollRouteThreadsCallerIdentity:
    """Drive the REAL HTTP route — the layer the lib-level classes cannot see.

    Every class above passes ``owner_user_id=…`` to queue functions explicitly.
    These tests therefore never touch the queue API for the ACT under test:
    registration, polling, and result resolution all go through the single
    current ``POST /api/browser/poll`` transport with real
    per-owner tokens, so the entry wiring itself is what passes or fails.

    ⚠️ ``scope_base`` is passed explicitly everywhere (§3.2c): the default
    ``'<local>'`` peer would make any "no credential" assertion a false green.
    """

    PEER = {'client': ('203.0.113.7', 5555)}

    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        # wait_for_commands_async reads POLL_WAIT_TIMEOUT from its own module
        # namespace at call time; shrink the long-poll window for tests.
        monkeypatch.setattr('lib.browser.queue._dispatch.POLL_WAIT_TIMEOUT', 0.3)

    def _make_token(self, scopes=('agents:bridge',), user_id=ALICE,
                    name='bridge-e2e'):
        from lib.api_keys import create_key
        _row, token = create_key(
            owner_user_id=int(user_id), name=name, scopes=list(scopes))
        return token

    def _poll(self, client, credential, client_id=None, results=None):
        headers = {'X-Bridge-Secret': credential} if credential else {}
        return client.post('/api/browser/poll',
                           json={
                               'clientId': client_id,
                               'protocolVersion': 2,
                               'capabilities': [],
                               'results': results or [],
                           },
                           headers=headers, scope_base=self.PEER)

    def test_per_user_token_accepted_and_registers_caller_identity(
            self, flask_client):
        """Ticket consequence ②: an agents:bridge token must be ACCEPTED by
        the browser bridge (previously 401 at the route even though the
        global gate had already approved it) and its user_id must reach the
        client registry."""
        token = self._make_token(user_id=ALICE, name='e2e-register')
        resp = self._poll(flask_client, token, 'alice-client')
        assert resp.status_code == 200, (
            'per-user bridge token rejected at the browser route: %s'
            % resp.get_json())
        from lib.browser.queue._registry import client_owner_user_id
        assert client_owner_user_id('alice-client') == ALICE, (
            'route authenticated the token but dropped its identity')

    def test_bobs_command_never_reaches_alices_http_poll(
            self, flask_client):
        """THE §5.3 acceptance, end-to-end: a command the server aimed at
        Bob's registered browser must never appear in ANY poll response
        authenticated as Alice. The queue has no anonymous or unaddressed
        delivery mode."""
        alice = self._make_token(user_id=ALICE, name='e2e-alice')
        bob = self._make_token(user_id=BOB, name='e2e-bob')
        # Both extensions register over the real route with their own tokens.
        assert self._poll(flask_client, alice, 'alice-client').status_code == 200
        assert self._poll(flask_client, bob, 'bob-client').status_code == 200

        from lib.browser.queue import send_browser_command
        box = {}

        def _send():
            box['out'] = send_browser_command(
                'get_cookies', {'url': 'https://example.test'},
                timeout=5, client_id='bob-client', owner_user_id=BOB)

        t = threading.Thread(target=_send, daemon=True)
        t.start()
        time.sleep(0.2)  # let the enqueue land before Alice polls

        # Alice's addressed poll: nothing for her.
        ra = self._poll(flask_client, alice, 'alice-client')
        assert ra.status_code == 200
        assert ra.get_json()['commands'] == [], (
            'cross-tenant leak to an addressed poll: %r' % ra.get_json())
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

    def test_removed_get_commands_transport_is_not_available(
            self, flask_client):
        """There is one poll transport; the retired GET endpoint stays gone."""
        token = self._make_token(user_id=ALICE, name='e2e-no-get')
        resp = flask_client.get(
            '/api/browser/commands?clientId=alice-client',
            headers={'X-Bridge-Secret': token}, scope_base=self.PEER)
        assert resp.status_code == 404

    def test_token_without_bridge_scope_rejected_over_http(
            self, flask_client):
        """Invariant pin: a valid but scope-less key never reaches the bridge."""
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

    def test_every_queue_call_site_carries_owner_user_id(self):
        calls = self._queue_call_sites()
        assert calls, 'ratchet blind: no queue call sites found in routes/browser.py'
        missing = [name for name, kws in calls if 'owner_user_id' not in kws]
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
