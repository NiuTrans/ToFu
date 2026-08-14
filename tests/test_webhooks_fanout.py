"""Webhook fan-out + signing — behaviour tests for routes/api_v1/webhooks.

WHY THIS FILE EXISTS
--------------------
``/api/v1/webhooks`` is the only externally-reachable surface in the audit's
zero-coverage list, and it is the one place in the codebase that hands out a
long-lived HMAC secret and then POSTs conversation events to an arbitrary
operator-supplied URL. Every defect here is silent and consequential:

  * leak ``secret`` in a list response → permanent credential disclosure;
  * get the fan-out filter wrong → one subscriber receives ANOTHER task's
    events (a cross-tenant data leak that no error log would show);
  * sign the wrong bytes → the signature stops being a replay defence while
    still *looking* present, so receivers keep trusting it.

Measured 2026-07-27 via ``coverage run``: this module was at **31%** — the
route bodies ran incidentally through app import, but no test exercised a
single one of the decisions above.

WHAT IS ASSERTED — behaviour, not implementation (charter discipline)
    These tests call the module's real functions and assert OUTCOMES a caller /
    receiver depends on. They deliberately do NOT go through the Quart test
    client: the app-building fixture is slow, and (per the sibling epic
    pt_f6742ab6) the in-process client reports ``'<local>'`` as the peer
    address, which silently takes the loopback auth exemption — so a
    route-level test here would be asserting the exempt path, not the real one.
    Auth enforcement on these endpoints is therefore explicitly OUT of scope
    for this file and belongs with that epic; what is in scope is the payload,
    signing and fan-out semantics, which are what the 31% was hiding.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest \
        tests/test_webhooks_fanout.py -p no:cacheprovider -q
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from routes.api_v1 import webhooks as wh  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_real_webhook_dns(monkeypatch):
    """Delivery-shape tests mock transport, so keep DNS off the test host."""
    monkeypatch.setattr(wh, '_validate_webhook_url', lambda _url: None)


def _sub(**over):
    s = {
        'id': 'wh_deadbeef',
        'url': 'https://example.invalid/hook',
        'channel': '',
        'task_id': '*',
        'event_types': [],
        'secret': 'S' * 64,
        'disabled': False,
    }
    s.update(over)
    return s


@pytest.fixture
def subs(monkeypatch):
    """Replace the on-disk subscription store with an in-memory list.

    ``_load`` is what every code path under test reads, so patching it keeps
    the tests off the real ``data/config/webhooks.json`` (which a developer's
    machine may legitimately have populated).
    """
    store: list = []
    monkeypatch.setattr(wh, '_load', lambda: list(store))
    return store


@pytest.fixture
def queued(monkeypatch):
    """Capture what the fan-out enqueues instead of running the worker."""
    items: list = []

    class _FakeQueue:
        def put_nowait(self, item):
            items.append(item)

    monkeypatch.setattr(wh, '_QUEUE', _FakeQueue())
    return items


# ── 1. the secret must never leave through a read path ────────────────

def test_public_view_strips_the_secret():
    """``_public`` is the ONLY shape a read endpoint may return.

    The secret is a bearer credential for forging signed deliveries; once it
    appears in a list response it is disclosed to every holder of a
    webhooks-scoped key, forever.
    """
    out = wh._public(_sub())
    assert 'secret' not in out, 'HMAC secret leaked through the public view'
    assert out['id'] == 'wh_deadbeef', 'public view dropped identifying fields'
    assert out['url'] == 'https://example.invalid/hook'


def test_public_view_does_not_mutate_the_stored_subscription():
    """Stripping must copy — mutating the store would destroy the live secret
    and silently break every subsequent signature."""
    original = _sub()
    wh._public(original)
    assert original.get('secret') == 'S' * 64, (
        'the stored subscription lost its secret — deliveries would go '
        'unsigned from this point on')


# ── 2. signature is over ts.body with the subscription secret ─────────

def test_signature_is_hmac_sha256_over_timestamp_dot_body():
    """A receiver recomputes this exact preimage; anything else fails verification.

    Asserted against an INDEPENDENT recomputation rather than a golden string,
    so the test states the contract instead of memorising one output.
    """
    secret, body, ts = 'k' * 32, '{"a":1}', '1700000000'
    expected = hmac.new(secret.encode(), f'{ts}.{body}'.encode(),
                        hashlib.sha256).hexdigest()
    assert wh._sign(secret, body, ts) == expected


def test_timestamp_is_inside_the_signed_preimage():
    """The timestamp MUST be signed, otherwise it can be rewritten and the
    replay window is unbounded — the signature would still verify."""
    secret, body = 'k' * 32, '{"a":1}'
    assert wh._sign(secret, body, '1700000000') != wh._sign(secret, body, '1700009999'), (
        'changing only the timestamp did not change the signature — the '
        'timestamp is not covered and replay protection is void')


def test_body_change_changes_the_signature():
    secret, ts = 'k' * 32, '1700000000'
    assert wh._sign(secret, '{"a":1}', ts) != wh._sign(secret, '{"a":2}', ts)


def test_different_secrets_yield_different_signatures():
    """Per-subscription secrets must actually isolate subscriptions."""
    body, ts = '{"a":1}', '1700000000'
    assert wh._sign('k' * 32, body, ts) != wh._sign('j' * 32, body, ts)


# ── 3. fan-out filtering — a mis-filter is a cross-tenant leak ────────

def test_disabled_subscription_receives_nothing(subs, queued):
    subs.append(_sub(disabled=True))
    wh._on_push_event('chat', 'task-1', {'type': 'done'})
    assert queued == [], 'a disabled subscription was still delivered to'


def test_channel_filter_excludes_other_channels(subs, queued):
    subs.append(_sub(id='wh_chat', channel='chat'))
    wh._on_push_event('presence', 'task-1', {'type': 'done'})
    assert queued == [], 'event from another channel leaked to a channel-scoped sub'
    wh._on_push_event('chat', 'task-1', {'type': 'done'})
    assert [i['sub']['id'] for i in queued] == ['wh_chat']


def test_empty_channel_means_all_channels(subs, queued):
    """An unset channel is a wildcard — the documented default."""
    subs.append(_sub(channel=''))
    wh._on_push_event('chat', 't1', {'type': 'done'})
    wh._on_push_event('presence', 't1', {'type': 'done'})
    assert len(queued) == 2


def test_task_id_filter_excludes_other_tasks(subs, queued):
    """THE cross-tenant assertion: a task-scoped subscriber must never see
    another task's events."""
    subs.append(_sub(id='wh_t1', task_id='task-1'))
    wh._on_push_event('chat', 'task-2', {'type': 'done'})
    assert queued == [], "another task's event was delivered to a task-scoped sub"
    wh._on_push_event('chat', 'task-1', {'type': 'done'})
    assert [i['task_id'] for i in queued] == ['task-1']


def test_star_task_id_matches_every_task(subs, queued):
    subs.append(_sub(task_id='*'))
    wh._on_push_event('chat', 'anything', {'type': 'done'})
    assert len(queued) == 1


def test_event_type_filter_excludes_unlisted_types(subs, queued):
    subs.append(_sub(event_types=['done', 'error']))
    wh._on_push_event('chat', 't1', {'type': 'phase'})
    assert queued == [], 'an unsubscribed event type was delivered'
    wh._on_push_event('chat', 't1', {'type': 'error'})
    assert [i['payload']['type'] for i in queued] == ['error']


def test_empty_event_types_means_all_types(subs, queued):
    subs.append(_sub(event_types=[]))
    wh._on_push_event('chat', 't1', {'type': 'anything_at_all'})
    assert len(queued) == 1


def test_filters_compose_as_AND_not_OR(subs, queued):
    """Matching ONE criterion is not enough — every set filter must match.

    An OR would turn a narrowly-scoped subscription into a firehose.
    """
    subs.append(_sub(channel='chat', task_id='task-1', event_types=['done']))
    wh._on_push_event('chat', 'task-1', {'type': 'phase'})     # wrong type
    wh._on_push_event('chat', 'task-2', {'type': 'done'})      # wrong task
    wh._on_push_event('presence', 'task-1', {'type': 'done'})  # wrong channel
    assert queued == [], 'filters behaved as OR — scope leaked'
    wh._on_push_event('chat', 'task-1', {'type': 'done'})      # all three match
    assert len(queued) == 1


def test_no_subscriptions_is_a_cheap_noop(subs, queued):
    wh._on_push_event('chat', 't1', {'type': 'done'})
    assert queued == []


def test_each_matching_subscription_gets_its_own_delivery(subs, queued):
    subs.extend([_sub(id='wh_a'), _sub(id='wh_b')])
    wh._on_push_event('chat', 't1', {'type': 'done'})
    assert sorted(i['sub']['id'] for i in queued) == ['wh_a', 'wh_b']


def test_enqueued_item_carries_the_delivery_context(subs, queued):
    """The worker needs channel/task/payload + a zero attempt counter; a missing
    ``attempt`` would make the retry ladder mis-count from the first failure."""
    subs.append(_sub())
    wh._on_push_event('chat', 'task-9', {'type': 'done', 'x': 1})
    item = queued[0]
    assert item['channel'] == 'chat'
    assert item['task_id'] == 'task-9'
    assert item['payload'] == {'type': 'done', 'x': 1}
    assert item['attempt'] == 0
    assert isinstance(item['ts'], float)


# ── 4. delivery envelope + headers ────────────────────────────────────

def test_delivery_posts_signed_envelope_with_expected_headers(monkeypatch):
    """The receiver-facing contract: JSON envelope + v1= signature header that
    verifies against the body actually sent."""
    sent = {}

    class _Resp:
        status_code = 200

    def fake_post(url, data=None, headers=None, timeout=None, **kwargs):
        sent.update(url=url, data=data, headers=headers, kwargs=kwargs)
        return _Resp()

    monkeypatch.setattr(wh, 'http_post', fake_post)
    item = {'sub': _sub(), 'channel': 'chat', 'task_id': 't1',
            'payload': {'type': 'done'}, 'ts': 1700000000.5, 'attempt': 0}
    assert wh._deliver(item) is True

    body = sent['data'].decode('utf-8')
    env = json.loads(body)
    assert env == {'channel': 'chat', 'task_id': 't1',
                   'event': {'type': 'done'}, 'ts': 1700000000.5}

    hdr = sent['headers']
    assert hdr['Content-Type'] == 'application/json'
    assert hdr['X-Tofu-Subscription-Id'] == 'wh_deadbeef'
    ts = hdr['X-Tofu-Timestamp']
    assert ts == '1700000000', 'timestamp header must be the integer seconds'
    assert hdr['X-Tofu-Signature'].startswith('v1='), 'missing version prefix'
    # The receiver's verification, performed here for real.
    expected = hmac.new(('S' * 64).encode(), f'{ts}.{body}'.encode(),
                        hashlib.sha256).hexdigest()
    assert hdr['X-Tofu-Signature'] == f'v1={expected}', (
        'signature does not verify against the body that was sent')
    assert sent['kwargs']['allow_redirects'] is False


def test_unsigned_when_subscription_has_no_secret(monkeypatch):
    """A secretless subscription sends an EMPTY signature rather than a
    signature over an empty key — the latter would look valid to a naive
    receiver while proving nothing."""
    sent = {}

    class _Resp:
        status_code = 200

    monkeypatch.setattr(wh, 'http_post',
                        lambda url, data=None, headers=None, timeout=None, **kwargs:
                        (sent.update(headers=headers), _Resp())[1])
    wh._deliver({'sub': _sub(secret=''), 'channel': 'c', 'task_id': 't',
                 'payload': {}, 'ts': 1.0, 'attempt': 0})
    assert sent['headers']['X-Tofu-Signature'] == ''


@pytest.mark.parametrize('status', [400, 401, 404, 429, 500, 503])
def test_non_2xx_is_reported_as_failure_for_retry(monkeypatch, status):
    """The worker's retry ladder depends on this boolean; treating a 4xx/5xx as
    success would silently drop events."""
    class _Resp:
        status_code = status

    monkeypatch.setattr(wh, 'http_post',
                        lambda *a, **k: _Resp())
    assert wh._deliver({'sub': _sub(), 'channel': 'c', 'task_id': 't',
                        'payload': {}, 'ts': 1.0, 'attempt': 0}) is False


def test_transport_exception_is_a_failure_not_a_crash(monkeypatch):
    """A dead endpoint must not kill the worker thread — it must return False so
    the retry ladder takes over."""
    def boom(*a, **k):
        raise OSError('connection refused')

    monkeypatch.setattr(wh, 'http_post', boom)
    assert wh._deliver({'sub': _sub(), 'channel': 'c', 'task_id': 't',
                        'payload': {}, 'ts': 1.0, 'attempt': 0}) is False


@pytest.mark.parametrize('status', [200, 201, 202, 204, 299])
def test_any_2xx_counts_as_delivered(monkeypatch, status):
    class _Resp:
        status_code = status

    monkeypatch.setattr(wh, 'http_post', lambda *a, **k: _Resp())
    assert wh._deliver({'sub': _sub(), 'channel': 'c', 'task_id': 't',
                        'payload': {}, 'ts': 1.0, 'attempt': 0}) is True


def test_non_ascii_payload_survives_the_envelope(monkeypatch):
    """``ensure_ascii=False`` is deliberate; the signature is computed over the
    UTF-8 bytes, so a receiver must get exactly those bytes back."""
    sent = {}

    class _Resp:
        status_code = 200

    monkeypatch.setattr(wh, 'http_post',
                        lambda url, data=None, headers=None, timeout=None, **kwargs:
                        (sent.update(data=data), _Resp())[1])
    wh._deliver({'sub': _sub(), 'channel': 'c', 'task_id': 't',
                 'payload': {'text': '豆腐'}, 'ts': 1.0, 'attempt': 0})
    assert json.loads(sent['data'].decode('utf-8'))['event']['text'] == '豆腐'


# ── 5. fan-out must never break in-browser push ───────────────────────

def test_queue_full_is_swallowed_so_push_keeps_working(subs, monkeypatch):
    """``_on_push_event`` runs as a PushHub listener. If it raised, a webhook
    backlog would take down the browser push path with it."""
    subs.append(_sub())

    class _FullQueue:
        def put_nowait(self, item):
            raise RuntimeError('queue full')

    monkeypatch.setattr(wh, '_QUEUE', _FullQueue())
    wh._on_push_event('chat', 't1', {'type': 'done'})   # must not raise


def test_retry_schedule_is_bounded_and_uses_no_timer_threads():
    """A failed endpoint consumes heap entries, never one OS thread per retry."""
    import itertools
    delayed = []
    item = {'sub': _sub(), 'attempt': 0}
    assert wh._schedule_retry(delayed, itertools.count(), item, now=10.0)
    assert len(delayed) == 1
    assert delayed[0][0] == 12.0
    assert item['attempt'] == 1


def test_permanent_4xx_is_not_retryable_but_transient_failures_are():
    assert wh._delivery_is_retryable({'_last_status': 400}) is False
    assert wh._delivery_is_retryable({'_last_status': 404}) is False
    assert wh._delivery_is_retryable({'_last_status': 429}) is True
    assert wh._delivery_is_retryable({'_last_status': 503}) is True
    assert wh._delivery_is_retryable({'_last_status': None}) is True


def test_deleted_or_disabled_subscription_revokes_queued_work(monkeypatch):
    sub = _sub(id='wh_revoke')
    monkeypatch.setattr(wh, '_load', lambda: [])
    assert wh._subscription_is_active(sub) is False

    monkeypatch.setattr(
        wh, '_load', lambda: [dict(sub, disabled=True)])
    assert wh._subscription_is_active(sub) is False

    monkeypatch.setattr(wh, '_load', lambda: [dict(sub, disabled=False)])
    assert wh._subscription_is_active(sub) is True


def test_worker_shutdown_wakes_and_joins_with_a_bound(monkeypatch):
    calls = []

    class _Stop:
        def set(self):
            calls.append('set')

    class _Queue:
        def put_nowait(self, item):
            calls.append(('wake', item))

    class _Thread:
        def join(self, timeout):
            calls.append(('join', timeout))

        def is_alive(self):
            return False

    monkeypatch.setattr(wh, '_WORKER_STOP', _Stop())
    monkeypatch.setattr(wh, '_QUEUE', _Queue())
    monkeypatch.setattr(wh, '_WORKER_THREAD', _Thread())
    monkeypatch.setattr(wh, '_WORKER_STARTED', True)

    assert wh.stop_webhook_worker(timeout='0.25') is True
    assert calls == ['set', ('wake', None), ('join', 0.25)]
    assert wh._WORKER_STARTED is False


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v']))
