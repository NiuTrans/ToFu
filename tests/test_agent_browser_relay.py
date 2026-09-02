"""Browser-assisted desktop transport for SSO-fronted Codelab URLs."""

import json
import threading
import time

import pytest
import requests

from lib.desktop_agent._browser_relay import BrowserRelay, RelayResponse, \
    origin_of

pytestmark = pytest.mark.unit


def test_origin_of_keeps_origin_and_drops_proxy_path():
    assert origin_of(
        'https://abc-vscode.example.com/proxy/15000/') == \
        'https://abc-vscode.example.com'
    assert origin_of('javascript:alert(1)') == ''


def _start_relay():
    relay = BrowserRelay(
        lambda: ['https://abc-vscode.example.com/proxy/15000/'],
        port_start=15280, port_end=15289)
    assert relay.start()
    return relay, 'http://127.0.0.1:%d' % relay.port


def test_loopback_http_rejects_an_unrelated_web_origin():
    relay, base = _start_relay()
    try:
        response = requests.get(base + '/v1/status', timeout=2,
                                headers={'Origin': 'https://evil.example'})
        assert response.status_code == 403
        ok = requests.get(
            base + '/v1/status', timeout=2,
            headers={'Origin': 'https://abc-vscode.example.com'})
        assert ok.status_code == 200
        assert ok.json()['kind'] == 'tofu-agent-browser-relay'
        assert ok.headers['Access-Control-Allow-Origin'] == \
            'https://abc-vscode.example.com'
    finally:
        relay.close()


def test_browser_round_trip_returns_response_shape_without_cookies():
    relay, base = _start_relay()
    origin = {'Origin': 'https://abc-vscode.example.com'}
    try:
        # The status probe marks the browser fresh, enabling the agent side.
        assert requests.get(base + '/v1/status', headers=origin,
                            timeout=2).ok
        box = {}

        def agent_request():
            box['response'] = relay.request(
                'https://abc-vscode.example.com/proxy/15000/api/desktop/poll',
                {'results': [], 'agent': {'agent_id': 'a'}},
                {'X-Bridge-Secret': 'tofu_live_test',
                 'Cookie': 'must-never-cross'}, timeout=3)

        thread = threading.Thread(target=agent_request)
        thread.start()
        take = requests.get(base + '/v1/take', headers=origin, timeout=2)
        assert take.status_code == 200
        job = take.json()
        assert job['headers'] == {'X-Bridge-Secret': 'tofu_live_test'}
        assert 'Cookie' not in json.dumps(job)
        returned = requests.post(
            base + '/v1/result', headers={**origin,
                                          'Content-Type': 'application/json'},
            json={'id': job['id'], 'status': 200,
                  'body': '{"commands":[]}'}, timeout=2)
        assert returned.status_code == 200
        thread.join(timeout=2)
        assert not thread.is_alive()
        response = box['response']
        assert response.status_code == 200
        assert response.json() == {'commands': []}
    finally:
        relay.close()


def test_dynamic_origin_gate_follows_reconnect():
    urls = ['https://one.example/proxy/15000']
    relay = BrowserRelay(lambda: urls, port_start=15380, port_end=15389)
    assert relay.start()
    base = 'http://127.0.0.1:%d/v1/status' % relay.port
    try:
        assert requests.get(base, headers={'Origin': 'https://one.example'},
                            timeout=2).status_code == 200
        urls[:] = ['https://two.example/proxy/15000']
        # No service restart: the callback is evaluated per request.
        assert requests.get(base, headers={'Origin': 'https://one.example'},
                            timeout=2).status_code == 403
        assert requests.get(base, headers={'Origin': 'https://two.example'},
                            timeout=2).status_code == 200
    finally:
        relay.close()


def test_timed_out_job_cannot_be_completed_late():
    relay, _base = _start_relay()
    try:
        relay.note_browser()
        assert relay.request('https://abc-vscode.example.com/x', {}, {},
                             timeout=0.05) is None
        # Drain the stale queue id; it is skipped instead of leaking to page.
        started = time.monotonic()
        assert relay.take(timeout=0.08) is None
        assert time.monotonic() - started < 0.3
    finally:
        relay.close()


# ── Bootstrap mode: the unattached agent's zero-config attach channel ──
# While no attachment is configured there is no origin to gate on, so
# /v1/status and /v1/attach open to any browser origin — the page that
# served the download must be able to find and provision the agent. The
# attach policy itself (origin-owns-a-route) lives in _push_attach.py; the
# broker owns the gate, the throttle, and the status codes.

def _start_attach_relay(attached=False, handler=None, allowed=None):
    state = {'attached': attached}
    calls = []

    def _default_handler(payload, origin):
        calls.append({'payload': payload, 'origin': origin})
        if handler is not None:
            return handler(payload, origin)
        return True, 'attached', 'http://10.0.0.1:15000', 'direct'

    relay = BrowserRelay(
        lambda: list(allowed if attached else []),
        port_start=15480, port_end=15489,
        attach_handler=_default_handler,
        attach_state=lambda: state['attached'])
    assert relay.start()
    return relay, 'http://127.0.0.1:%d' % relay.port, state, calls


def test_bootstrap_status_is_open_and_reports_unattached():
    """The page's discovery probe must find an UNATTACHED broker from the
    page's own origin — which the agent cannot know in advance."""
    relay, base, _state, _calls = _start_attach_relay(attached=False)
    try:
        r = requests.get(base + '/v1/status', timeout=2,
                         headers={'Origin': 'https://some-tofu.example'})
        assert r.status_code == 200
        body = r.json()
        assert body['kind'] == 'tofu-agent-browser-relay'
        assert body['attached'] is False
        assert r.headers['Access-Control-Allow-Origin'] == \
            'https://some-tofu.example'
    finally:
        relay.close()


def test_bootstrap_keeps_the_job_verbs_on_the_strict_gate():
    """Only status/attach open in bootstrap; /v1/take must never leak a
    poll job to a page whose origin the agent does not (yet) know."""
    relay, base, _state, _calls = _start_attach_relay(attached=False)
    try:
        r = requests.get(base + '/v1/take', timeout=2,
                         headers={'Origin': 'https://evil.example'})
        assert r.status_code == 403
    finally:
        relay.close()


def test_bootstrap_attach_round_trip():
    relay, base, _state, calls = _start_attach_relay(attached=False)
    origin = {'Origin': 'https://tofu.example'}
    bundle = {'v': 1, 'kind': 'tofu-agent-attach', 'token': 'tofu_live_X',
              'candidates': ['http://10.0.0.1:15000'],
              'fallback_candidates': ['https://tofu.example']}
    try:
        r = requests.post(base + '/v1/attach',
                          headers={**origin,
                                   'Content-Type': 'application/json'},
                          json=bundle, timeout=2)
        assert r.status_code == 200
        body = r.json()
        assert body['accepted'] is True and body['transport'] == 'direct'
        assert body['url'] == 'http://10.0.0.1:15000'
        assert calls == [{'payload': bundle, 'origin': 'https://tofu.example'}]
    finally:
        relay.close()


def test_attach_refusals_map_to_status_codes():
    outcomes = iter([
        (False, 'already_attached', '', ''),
        (False, 'origin_mismatch', '', ''),
    ])
    relay, base, _state, _calls = _start_attach_relay(
        attached=False, handler=lambda p, o: next(outcomes))
    origin = {'Origin': 'https://tofu.example',
              'Content-Type': 'application/json'}
    try:
        r = requests.post(base + '/v1/attach', headers=origin,
                          json={'candidates': ['http://x:1']}, timeout=2)
        assert r.status_code == 409
        assert r.json()['reason'] == 'already_attached'
        # The broker throttles attempts — sleep past the 3 s window so the
        # second refusal is measured, not the throttle.
        relay._last_attach_at -= 4.0
        r = requests.post(base + '/v1/attach', headers=origin,
                          json={'candidates': ['http://x:1']}, timeout=2)
        assert r.status_code == 403
        assert r.json()['reason'] == 'origin_mismatch'
    finally:
        relay.close()


def test_attach_attempts_are_throttled():
    relay, base, _state, calls = _start_attach_relay(attached=False)
    origin = {'Origin': 'https://tofu.example',
              'Content-Type': 'application/json'}
    payload = {'candidates': ['http://x:1']}
    try:
        first = requests.post(base + '/v1/attach', headers=origin,
                              json=payload, timeout=2)
        second = requests.post(base + '/v1/attach', headers=origin,
                               json=payload, timeout=2)
        assert first.status_code == 200
        assert second.status_code == 429
        assert second.json()['reason'] == 'throttled'
        assert len(calls) == 1, 'the handler ran despite the throttle'
    finally:
        relay.close()


def test_an_attached_broker_refuses_foreign_origins_on_attach():
    """Once configured, the bootstrap openness closes: only the configured
    server's own page may push (the dead-route repair path)."""
    relay, base, _state, calls = _start_attach_relay(
        attached=True, allowed=['https://mine.example/proxy/15000/'])
    body = {'candidates': ['http://x:1']}
    try:
        evil = requests.post(
            base + '/v1/attach', json=body, timeout=2,
            headers={'Origin': 'https://evil.example',
                     'Content-Type': 'application/json'})
        assert evil.status_code == 403
        assert not calls, 'a foreign origin must never reach the handler'
        mine = requests.post(
            base + '/v1/attach', json=body, timeout=2,
            headers={'Origin': 'https://mine.example',
                     'Content-Type': 'application/json'})
        assert mine.status_code == 200
        assert len(calls) == 1
        # …and status now reports attached, so the page never pushes.
        st = requests.get(base + '/v1/status', timeout=2,
                          headers={'Origin': 'https://mine.example'})
        assert st.json()['attached'] is True
    finally:
        relay.close()


def test_a_handlerless_broker_answers_attach_with_404():
    """The page maps 404 to 'relay-only build — stop pushing'."""
    relay, base = _start_relay()
    try:
        r = requests.post(
            base + '/v1/attach', json={'candidates': ['http://x:1']},
            timeout=2,
            headers={'Origin': 'https://abc-vscode.example.com',
                     'Content-Type': 'application/json'})
        assert r.status_code == 404
    finally:
        relay.close()


def test_run_agent_prefers_live_browser_transport(monkeypatch, tmp_path):
    from lib.desktop_agent import _run

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))

    class FakeRelay:
        def browser_available(self):
            return True

        def request(self, url, payload, headers, timeout=0):
            assert url.endswith('/api/desktop/poll')
            assert headers['X-Bridge-Secret'] == 'secret'
            return RelayResponse(200, '{"commands":[]}')

    monkeypatch.setattr(
        _run.requests, 'post',
        lambda *a, **k: pytest.fail('direct request used despite live browser'))
    stop = threading.Event()
    seen = []

    def status(item):
        seen.append(item)
        stop.set()

    _run.run_agent('https://abc.example/proxy/15000', {},
                   poll_interval=0.001, bridge_secret='secret',
                   stop_event=stop, on_status=status,
                   browser_relay=FakeRelay())
    assert seen == [{'state': 'ok', 'transport': 'browser'}]


def test_gateway_401_does_not_drop_queued_command_results(
        monkeypatch, tmp_path):
    """The browser handoff often starts after one gateway 401.  Results
    offered on that rejected poll must be offered again, not discarded."""
    from lib.desktop_agent import _run

    monkeypatch.setenv('TOFU_DESKTOP_CONFIG', str(tmp_path / 'agent.json'))
    sent = []
    responses = [
        RelayResponse(200, json.dumps({'commands': [
            {'id': 'cmd-1', 'type': 'definitely_unknown', 'params': {}}
        ]})),
        RelayResponse(401, '{"error":"Unauthorized"}'),
        RelayResponse(200, '{"commands":[]}'),
    ]

    def post(_url, **kwargs):
        sent.append(kwargs['json'])
        return responses.pop(0)

    monkeypatch.setattr(_run.requests, 'post', post)
    monkeypatch.setattr(_run.time, 'sleep', lambda _seconds: None)
    stop = threading.Event()
    transitions = []

    def status(item):
        transitions.append(item['state'])
        if transitions == ['ok', 'proxy', 'ok']:
            stop.set()

    _run.run_agent('https://abc.example/proxy/15000', {},
                   poll_interval=0.001, bridge_secret='secret',
                   stop_event=stop, on_status=status)
    assert len(sent[1]['results']) == 1
    assert sent[2]['results'] == sent[1]['results']
