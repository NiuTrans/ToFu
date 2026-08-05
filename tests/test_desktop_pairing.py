"""tests/test_desktop_pairing.py — the pairing-code mint + exchange
contract (docs/DESKTOP_AGENT_DIST_DESIGN.md §11, slice P1).

Pinned against the live ``lib.desktop.pairing`` store and the two routes:

  * ``POST /api/v1/desktop/pair-code`` (authenticated) mints a 6-digit
    one-time code (300 s TTL, one-shot, 3-attempt lockout).
  * ``POST /api/desktop/pair`` (NO auth — the code IS the credential)
    consumes the code and returns an ``agents:bridge`` token for the
    code's minting user.

Contract:
  1. a code mints and is listable as pending;
  2. the RIGHT code consumes ONCE and mints a bridge key bound to its
     user (the poll is then authorized as that user);
  3. the code is one-shot (second consume fails);
  4. a wrong code 3× does not reveal the real code (lockout returns the
     same 409; the real code stays valid for the right guess);
  5. an expired code fails (NEUTER on the TTL);
  6. /pair-code REQUIRES auth and binds to the caller's user_id (a
     code minted by user B authorizes a poll as user B, not user A —
     RWA P4a user-scope);
  7. the mint route returns 401 without auth;
  8. LAN discovery: the responder starts, answers a probe with its url,
     ignores garbage, stops cleanly.

Run:  pytest tests/test_desktop_pairing.py -q -p no:napari -o addopts=
"""

from __future__ import annotations

import socket
import time

import pytest

from lib.desktop import pairing as pairing_mod

# Private-mode auth so unauthenticated /api/v1/* calls 401 (the contract
# being pinned here) — same convention as test_api_v1_integration.py.
pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('private')]


# ── key helpers ───────────────────────────────────────────────────────

def _bearer(user_id='u-pair'):
    """A Bearer token carrying *user_id* (loopback synthetic principal)."""
    from lib.api_keys import create_key
    row, token = create_key(name=f'test-{user_id}', scopes=['chat'],
                             user_id=user_id)
    return {'Authorization': f'Bearer {token}'}, token, row['id']


def _clear_state():
    from lib.desktop.pairing import _STORE, _STORE_LOCK
    with _STORE_LOCK:
        _STORE.clear()
    from lib import api_keys
    api_keys._cache.clear()
    api_keys._cache_loaded = False


def _mint(cli, auth):
    """Mint a pairing code via the panel route; returns the code string."""
    r = cli.post('/api/v1/desktop/pair-code', headers=auth)
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()['code']


# ── 1-7: the code lifecycle via the two routes ────────────────────────

class TestPairingLifecycle:
    """End-to-end: mint → consume → one-shot → lockout → expiry."""

    def setup_method(self):
        _clear_state()

    def test_1_code_mints_and_is_pending(self, flask_client):
        auth, _, _ = _bearer('u-alice')
        r = flask_client.post('/api/v1/desktop/pair-code', headers=auth)
        assert r.status_code == 201, r.get_data(as_text=True)
        body = r.get_json()
        code = body['code']
        assert len(code) == 6 and code.isdigit()
        assert body['ttl'] == pairing_mod._CODE_TTL_S
        # The code shows up in the caller's pending list.
        assert any(p['code'] == code for p in body['pending'])
        self._code = code

    def test_2_consume_mints_a_bridge_key_for_the_codes_user(self,
                                                              flask_client):
        auth, _, _ = _bearer('u-alice')
        code = _mint(flask_client, auth)
        r = flask_client.post('/api/desktop/pair', json={
            'code': code, 'name': 'office-pc', 'platform': 'windows'})
        assert r.status_code == 201, r.get_data(as_text=True)
        body = r.get_json()
        assert body['scopes'] == ['agents:bridge']
        assert body['user_id'] == 'u-alice'
        assert body['token'].startswith('tofu_live_')
        # The minted token authorizes a poll scoped to u-alice.
        poll = flask_client.post(
            '/api/desktop/poll',
            json={'results': [], 'streams': [],
                  'agent': {'agent_id': 'x', 'name': 'office-pc',
                            'platform': 'windows', 'capabilities': {},
                            'share_roots': []}},
            headers={'X-Bridge-Secret': body['token']})
        assert poll.status_code == 200

    def test_3_code_is_one_shot(self, flask_client):
        auth, _, _ = _bearer('u-alice')
        code = _mint(flask_client, auth)
        assert self._consume(flask_client, code).status_code == 201
        # Second consume of the SAME code fails.
        assert self._consume(flask_client, code).status_code == 409

    def test_4_wrong_code_does_not_kill_the_real_one(self, flask_client):
        auth, _, _ = _bearer('u-alice')
        real_code = _mint(flask_client, auth)
        # Three wrong guesses.
        for _ in range(3):
            r = self._consume(flask_client, '000000')
            assert r.status_code == 409
        # The real code STILL works (lockout is per-code, and '000000' !=
        # the real code so it was never attempted).
        assert self._consume(flask_client, real_code).status_code == 201

    def test_5_expired_code_fails(self, flask_client, monkeypatch):
        auth, _, _ = _bearer('u-alice')
        # NEUTER the TTL so the minted code is already expired.
        monkeypatch.setattr(pairing_mod, '_CODE_TTL_S', -1)
        code = _mint(flask_client, auth)
        time.sleep(0.05)
        assert self._consume(flask_client, code).status_code == 409

    def test_6_mint_requires_auth_and_scopes_to_caller(self, flask_client):
        # No bearer → 401 on the mint route.
        r = flask_client.post('/api/v1/desktop/pair-code')
        assert r.status_code == 401

        # A code minted by user B authorizes a poll as user B.
        auth_b, _, _ = _bearer('u-bob')
        code = self._mint(flask_client, auth_b)
        body = self._consume_ok(flask_client, code)
        assert body['user_id'] == 'u-bob'

    # ── helpers ──────────────────────────────────────────────────────

    def _mint(self, cli, auth):
        return _mint(cli, auth)

    def _consume(self, cli, code):
        return cli.post('/api/desktop/pair', json={'code': code})

    def _consume_ok(self, cli, code):
        r = self._consume(cli, code)
        assert r.status_code == 201, r.get_data(as_text=True)
        return r.get_json()


# ── 8: LAN discovery responder ────────────────────────────────────────

class TestLanDiscovery:
    """The responder advertises its url to a broadcast probe, ignores
    everything else, and stops cleanly."""

    def test_responder_answers_probe_with_url(self):
        res = pairing_mod.LanDiscoveryResponder(
            url='http://192.168.9.9:15000', bind=('127.0.0.1', 0))
        assert res.start()
        port = res._sock.getsockname()[1]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            sock.sendto(b'NOT-A-PROBE', ('127.0.0.1', port))
            sock.sendto(pairing_mod._LAN_MAGIC, ('127.0.0.1', port))
            try:
                data, _ = sock.recvfrom(1024)
            except socket.timeout:
                data = b''
            assert data.endswith(b'http://192.168.9.9:15000'), data
        finally:
            sock.close()
            res.stop()

    def test_responder_silent_on_bad_magic(self):
        res = pairing_mod.LanDiscoveryResponder(
            url='http://192.168.9.9:15000', bind=('127.0.0.1', 0))
        assert res.start()
        port = res._sock.getsockname()[1]
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            sock.sendto(b'\x00' * 16, ('127.0.0.1', port))
            try:
                sock.recvfrom(1024)
                answered = True
            except socket.timeout:
                answered = False
            assert not answered, 'responder answered a bad-magic probe'
        finally:
            sock.close()
            res.stop()

    def test_start_is_idempotent_and_stop_cleans_up(self):
        res = pairing_mod.LanDiscoveryResponder(
            url='http://192.168.9.9:15000', bind=('127.0.0.1', 0))
        assert res.start()
        assert res.start()  # second call is a no-op
        res.stop()
        assert res._sock is None


# ── 9: per-IP global failure budget (owner 2026-08-04) ──────────────
# The per-code 3-attempt lockout does NOT stop an attacker who keeps
# guessing NEW random codes — each attempt gets a fresh budget. The real
# boundary is the attacker's ATTEMPT RATE per IP: N failures inside the
# window → 429 BEFORE the code is even looked up.
class TestPairIpBudget:
    def setup_method(self):
        _clear_state()
        from lib.desktop.pairing import _IP_BLOCKED, _IP_FAILS, _STORE_LOCK
        with _STORE_LOCK:
            _IP_BLOCKED.clear()
            _IP_FAILS.clear()

    def _consume(self, cli, code, ip='1.2.3.4'):
        # Quart sets remote_addr from the ASGI scope's client tuple — the
        # same override the bridge-addressing suite uses.
        return cli.post('/api/desktop/pair', json={'code': code},
                        scope_base={'client': (ip, 5555)})

    def test_n_failures_trip_429_before_consume(self, flask_client):
        ip = '9.9.9.9'
        for _ in range(pairing_mod._IP_FAIL_BUDGET):
            r = self._consume(flask_client, '000000', ip=ip)
            assert r.status_code == 409
        # The next attempt — even a VALID code — is 429'd before lookup.
        auth, _, _ = _bearer('u-alice')
        code = _mint(flask_client, auth)
        r = self._consume(flask_client, code, ip=ip)
        assert r.status_code == 429, (
            f'a blocked IP must 429 before its code is consumed, got '
            f'{r.status_code}')

    def test_success_resets_the_ip_slate(self, flask_client):
        ip = '8.8.8.8'
        for _ in range(3):
            assert self._consume(flask_client, '000000',
                                 ip=ip).status_code == 409
        # One successful exchange clears the slate — a legit agent retrying
        # after a transient failure is not punished forever.
        auth, _, _ = _bearer('u-alice')
        code = _mint(flask_client, auth)
        assert self._consume(flask_client, code, ip=ip).status_code == 201
        # After the reset the budget is fresh again: more attempts allowed.
        for _ in range(3):
            assert self._consume(flask_client, '000000',
                                 ip=ip).status_code == 409

    def test_the_budget_is_per_ip_not_global(self, flask_client):
        for _ in range(pairing_mod._IP_FAIL_BUDGET):
            self._consume(flask_client, '000000', ip='5.5.5.5')
        # That IP is blocked…
        auth, _, _ = _bearer('u-alice')
        code = _mint(flask_client, auth)
        assert self._consume(flask_client, code, ip='5.5.5.5') \
            .status_code == 429
        # …but a DIFFERENT IP exchanges the same code fine.
        assert self._consume(flask_client, code, ip='6.6.6.6') \
            .status_code == 201


# ── 10: production wiring (owner review 2026-08-03; default-ON 2026-08-04) ──
# The responder class used to be instantiated ONLY by these tests — no
# caller in the server startup path meant rung B could never answer in
# production. maybe_start_responder is the wiring; server.py calls it.
# Since 2026-08-04 the responder is ON BY DEFAULT (owner: LAN pairing is
# the primary agent-attach flow and must not need an env var) — only an
# explicit TOFU_DESKTOP_LAN_DISCOVERY=0 or a loopback-only bind silences it.
class TestMaybeStartResponder:
    def _start(self, monkeypatch, environ, **kw):
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '192.168.1.50')
        res = pairing_mod.maybe_start_responder(
            15000, environ=environ, bind=('127.0.0.1', 0), **kw)
        assert res is not None
        try:
            assert res.url == 'http://192.168.1.50:15000'
            assert res._thread and res._thread.is_alive()
        finally:
            res.stop()

    def test_on_by_default(self, monkeypatch):
        # No env var at all → the responder STARTS (2026-08-04 default flip).
        self._start(monkeypatch, {})

    def test_zero_is_the_only_off_switch(self, monkeypatch):
        assert pairing_mod.maybe_start_responder(
            15000, environ={'TOFU_DESKTOP_LAN_DISCOVERY': '0'},
            bind=('127.0.0.1', 0)) is None

    def test_non_zero_values_do_not_disable(self, monkeypatch):
        # '', '1', 'yes', 'true' all leave the default-ON responder up.
        for v in ('', '1', 'yes', 'true'):
            self._start(monkeypatch, {'TOFU_DESKTOP_LAN_DISCOVERY': v})

    def test_loopback_bind_stays_silent(self, monkeypatch):
        # Advertising http://<lan-ip> while bound loopback-only would send
        # every discovering agent to a dead address — the responder must
        # refuse even though discovery is on.
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '192.168.1.50')
        for lb in ('127.0.0.1', 'localhost', '::1'):
            assert pairing_mod.maybe_start_responder(
                15000, environ={}, bind=('127.0.0.1', 0),
                bind_host=lb) is None

    def test_all_interfaces_bind_advertises(self, monkeypatch):
        self._start(monkeypatch, {}, bind_host='0.0.0.0')

    def test_starts_when_enabled_and_advertises_lan_url(self, monkeypatch):
        self._start(monkeypatch, {'TOFU_DESKTOP_LAN_DISCOVERY': '1'})

    def test_no_lan_ip_stays_silent(self, monkeypatch):
        monkeypatch.setattr(pairing_mod, 'lan_ip', lambda: '')
        assert pairing_mod.maybe_start_responder(
            15000, environ={}, bind=('127.0.0.1', 0)) is None
