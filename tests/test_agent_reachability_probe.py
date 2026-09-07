"""tests/test_agent_reachability_probe.py — the proxy-URL root fixes
(owner incident 2026-08-03).

A connect line minted while browsing through an SSO-fronted gateway
carries an address an AGENT can never use: the edge 401s every request
before Tofu sees it, polls never arrive, and both ends stayed silent —
the panel said only "未运行" and the agent's log cried "bridge auth
failed" (the wrong half of the line). Four layers, four pins:

  1. ``_host_reachability`` classifies the mint-context host so the
     panel can warn at mint time;
  2. ``probe_server`` lets the connect dialog verify a pasted line at
     paste time (GET /api/health — the one open endpoint);
  3. ``is_tofu_error_envelope`` lets the poll loop tell Tofu's 401
     (wrong secret) from a gateway's 401 (wrong URL);
  4. ``run_agent`` fires the on_status transitions the tray link line
     renders — including 'proxy', the state that would have diagnosed
     the incident in seconds.
"""

from __future__ import annotations

import threading

import pytest
import requests

pytestmark = pytest.mark.unit


# ── 1. mint-context host classification ─────────────────────────────

def test_host_reachability_loopback_shapes():
    from routes.api_v1.desktop import _host_reachability
    assert _host_reachability('127.0.0.1:15000') == 'loopback'
    assert _host_reachability('localhost:15000') == 'loopback'
    assert _host_reachability('localhost') == 'loopback'
    assert _host_reachability('[::1]:15000') == 'loopback'
    assert _host_reachability('') == 'loopback'


def test_host_reachability_private_lan():
    from routes.api_v1.desktop import _host_reachability
    assert _host_reachability('10.128.175.30:15000') == 'private'
    assert _host_reachability('192.168.1.10:15000') == 'private'
    assert _host_reachability('172.16.0.5') == 'private'


def test_host_reachability_public_hostname_is_the_proxy_case():
    from routes.api_v1.desktop import _host_reachability
    # The exact incident host: a cloud-IDE preview proxy behind SSO.
    assert _host_reachability(
        '5665bc99-279b-vscode-zw05.mlp.internal.example.com') == 'public'
    assert _host_reachability('tofu.example.com') == 'public'
    assert _host_reachability('8.8.8.8:15000') == 'public'


# ── 2. paste-time probe ──────────────────────────────────────────────

class _Resp:
    def __init__(self, status, body=None, json_raises=False):
        self.status_code = status
        self._body = body
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError('no json')
        return self._body


def _patch_get(monkeypatch, fn):
    monkeypatch.setattr(requests, 'get', fn)


def test_probe_ok_requires_tofu_health_json(monkeypatch):
    from lib.desktop_agent._probe import probe_server
    _patch_get(monkeypatch, lambda *a, **k: _Resp(200, {'bootId': 'x'}))
    assert probe_server('http://127.0.0.1:15000/') == (True, '')


def test_probe_200_that_is_not_tofu(monkeypatch):
    from lib.desktop_agent._probe import probe_server
    _patch_get(monkeypatch, lambda *a, **k: _Resp(200, json_raises=True))
    assert probe_server('http://x/') == (False, 'not_tofu')
    _patch_get(monkeypatch, lambda *a, **k: _Resp(200, {'status': 'ok'}))
    assert probe_server('http://x/') == (False, 'not_tofu')


def test_probe_401_403_are_named_precisely(monkeypatch):
    from lib.desktop_agent._probe import probe_server
    # The measured SSO edge shape: {"error":"Unauthorized"} at 401.
    _patch_get(monkeypatch, lambda *a, **k: _Resp(401, {'error': 'Unauthorized'}))
    assert probe_server('https://proxy.example/') == (False, 'http_401')
    _patch_get(monkeypatch, lambda *a, **k: _Resp(403, {}))
    assert probe_server('https://proxy.example/') == (False, 'http_403')


def test_probe_other_status_and_transport_failures(monkeypatch):
    from lib.desktop_agent._probe import probe_server
    _patch_get(monkeypatch, lambda *a, **k: _Resp(500, {}))
    assert probe_server('http://x/') == (False, 'http_500')

    def _timeout(*a, **k):
        raise requests.exceptions.ConnectTimeout()
    _patch_get(monkeypatch, _timeout)
    assert probe_server('http://x/') == (False, 'timeout')

    def _conn(*a, **k):
        raise requests.exceptions.ConnectionError()
    _patch_get(monkeypatch, _conn)
    assert probe_server('http://x/') == (False, 'unreachable')

    def _other(*a, **k):
        raise requests.RequestException('boom')
    _patch_get(monkeypatch, _other)
    assert probe_server('http://x/') == (False, 'error')


# ── 3. tofu-vs-gateway 401 classification ────────────────────────────

def test_tofu_error_envelope_vs_proxy_string_error():
    from lib.desktop_agent._probe import is_tofu_error_envelope
    # Tofu's api_error family: ok=false + error OBJECT.
    assert is_tofu_error_envelope(
        {'ok': False, 'error': {'code': 'unauthorized'}}) is True
    # The measured SSO proxy shape: error as a STRING.
    assert is_tofu_error_envelope({'error': 'Unauthorized'}) is False
    assert is_tofu_error_envelope({'ok': True}) is False
    assert is_tofu_error_envelope('not a dict') is False
    assert is_tofu_error_envelope(None) is False


# ── 4. run_agent's on_status transitions ─────────────────────────────

def _run_one_iteration(monkeypatch, tmp_path, resp):
    """Drive run_agent with a fake POST; stop after the first status."""
    from lib.desktop_agent import _run

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG',
                       str(tmp_path / 'agent_cfg.json'))
    monkeypatch.setattr(_run.requests, 'post', lambda *a, **k: resp)

    stop = threading.Event()
    seen = []

    def on_status(st):
        seen.append(st)
        stop.set()  # one transition is enough — exit at the next boundary

    _run.run_agent('http://fake/', {}, poll_interval=0.01,
                   bridge_secret='s', stop_event=stop, on_status=on_status)
    return seen


def test_run_agent_proxy_401_is_named_proxy_not_auth(monkeypatch, tmp_path):
    resp = _Resp(401, {'error': 'Unauthorized'})
    seen = _run_one_iteration(monkeypatch, tmp_path, resp)
    assert seen and seen[0]['state'] == 'proxy', seen


def test_run_agent_tofu_401_is_auth(monkeypatch, tmp_path):
    resp = _Resp(401, {'ok': False, 'error': {'code': 'unauthorized'}})
    seen = _run_one_iteration(monkeypatch, tmp_path, resp)
    assert seen and seen[0]['state'] == 'auth', seen


def test_run_agent_200_is_ok_and_other_status_is_http(monkeypatch, tmp_path):
    resp = _Resp(200, {'commands': []})
    seen = _run_one_iteration(monkeypatch, tmp_path, resp)
    assert seen and seen[0]['state'] == 'ok', seen

    resp = _Resp(503, {})
    seen = _run_one_iteration(monkeypatch, tmp_path, resp)
    assert seen and seen[0]['state'] == 'http' \
        and seen[0]['code'] == 503, seen


def test_run_agent_status_fires_on_transitions_not_per_poll(
        monkeypatch, tmp_path):
    """A 1 Hz poll must not mean a 1 Hz callback storm — the tray only
    needs to know when the verdict CHANGES."""
    from lib.desktop_agent import _run

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG',
                       str(tmp_path / 'agent_cfg.json'))
    monkeypatch.setattr(_run.requests, 'post',
                        lambda *a, **k: _Resp(200, {'commands': []}))

    stop = threading.Event()
    seen = []
    polls = {'n': 0}

    def on_status(st):
        seen.append(st)

    real_post = _run.requests.post

    def counting_post(*a, **k):
        polls['n'] += 1
        if polls['n'] >= 5:
            stop.set()
        return real_post(*a, **k)

    monkeypatch.setattr(_run.requests, 'post', counting_post)
    _run.run_agent('http://fake/', {}, poll_interval=0.001,
                   bridge_secret='s', stop_event=stop,
                   on_status=on_status)
    assert polls['n'] >= 5
    assert [s['state'] for s in seen] == ['ok'], seen
