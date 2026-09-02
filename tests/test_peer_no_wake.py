"""Peer-message wake=False (mailbox-only) — Codex ``trigger_turn`` port.

Pins the trigger_turn semantics borrowed from codex-rs agent_communication
(send_message = mailbox-only, followup_task = start a turn NOW):

  * ``send_peer_message(..., wake=False)`` flags the durable payload
    ``_peerNoWake`` and NEVER dispatches a fresh turn for an idle target at
    send time.
  * The heartbeat idle-drain (``drain_idle_peer_messages``) skips a conv whose
    pending peer rows are ALL no-wake; one wake-capable row justifies the turn.
  * Default ``wake=True`` behaviour is byte-identical to the pre-flag code.

Lightweight fakes only (no real DB): the sidecar queue client is stubbed.
"""

import json

import pytest

import lib.message_queue as mq
from lib.conversations import project_peer as pp

pytestmark = pytest.mark.unit
USER_ID = 1


# ── send_peer_message wake flag ───────────────────────────────────────


@pytest.fixture
def send_env(monkeypatch):
    """Neutralise the durable queue, live-task probe, drains, feed and audit.

    Returns a recorder dict capturing the enqueued payload + drain calls.
    """
    rec = {'payload': None, 'dispatched': [], 'twin': []}
    monkeypatch.setattr(
        pp, '_resolve_target_conv_id',
        lambda cid, *, user_id: (cid, ''),
    )
    monkeypatch.setattr(
        pp, '_live_drain_eligible_task',
        lambda cid, *, user_id: False,
    )
    monkeypatch.setattr(pp, 'audit_log', lambda *a, **k: None)

    def _fake_enqueue(conv_id, payload, config, kind=None, *, user_id):
        assert user_id == USER_ID
        rec['payload'] = payload
        return {'ok': True, 'queueId': 'q' + '1' * 31}

    def _fake_dispatch(conv_id, **kwargs):
        rec['dispatched'].append(conv_id)
        return 'task' + '0' * 28

    def _fake_twin_enqueue(key, value, *, priority=None, mode=None, extra=None):
        rec['twin'].append(value)

    monkeypatch.setattr(mq, 'enqueue_message', _fake_enqueue)
    monkeypatch.setattr(mq, 'dispatch_next_queued', _fake_dispatch)
    monkeypatch.setattr(
        'lib.conversations.project_feed.emit_project_event',
        lambda *a, **k: None)
    import lib.agent_inbox
    monkeypatch.setattr(lib.agent_inbox, 'enqueue', _fake_twin_enqueue)
    return rec


def test_wake_false_flags_payload_and_skips_send_time_drain(send_env):
    res = pp.send_peer_message('proj', 'a' * 32, 'b' * 32, 'fyi note',
                               wake=False, user_id=USER_ID)
    assert res.get('ok') is True
    assert send_env['payload'].get('_peerNoWake') is True
    # The wake=False contract: NO fresh turn is started for the idle target.
    assert send_env['dispatched'] == []


def test_wake_default_dispatches_idle_target_and_leaves_payload_unflagged(
        send_env):
    res = pp.send_peer_message(
        'proj', 'a' * 32, 'b' * 32, 'please confirm', user_id=USER_ID)
    assert res.get('ok') is True
    assert '_peerNoWake' not in send_env['payload']
    assert send_env['dispatched'] == ['b' * 32]


# ── _conv_has_wake_peer_row ───────────────────────────────────────────


@pytest.fixture
def sidecar_rows(monkeypatch):
    """Stub the sidecar queue client; ``rows`` is the mutable row list."""
    state = {'rows': []}

    class _FakeClient:
        def query(self, op, params):
            assert op == 'queue.list'
            assert params['user_id'] == USER_ID
            return list(state['rows'])

    monkeypatch.setattr(
        mq, '_queue_client', lambda **_kwargs: _FakeClient())
    return state


def _peer_row(conv_id, payload):
    return {'kind': mq.KIND_PEER_MSG, 'payload': json.dumps(payload)}


def test_wake_probe_all_no_wake(sidecar_rows):
    sidecar_rows['rows'] = [
        _peer_row('c' * 32, {'text': 'a', '_peerNoWake': True}),
        _peer_row('c' * 32, {'text': 'b', '_peerNoWake': True}),
    ]
    assert mq._conv_has_wake_peer_row(
        'c' * 32, user_id=USER_ID) is False


def test_wake_probe_one_wake_row_suffices(sidecar_rows):
    sidecar_rows['rows'] = [
        _peer_row('c' * 32, {'text': 'fyi', '_peerNoWake': True}),
        _peer_row('c' * 32, {'text': 'urgent'}),
    ]
    assert mq._conv_has_wake_peer_row(
        'c' * 32, user_id=USER_ID) is True


def test_wake_probe_unparseable_payload_counts_as_wake(sidecar_rows):
    # A row we cannot classify must not be silently demoted to mailbox-only.
    sidecar_rows['rows'] = [
        {'conv_id': 'c' * 32, 'kind': mq.KIND_PEER_MSG, 'payload': '{broken'},
    ]
    assert mq._conv_has_wake_peer_row(
        'c' * 32, user_id=USER_ID) is True


def test_wake_probe_storage_error_fails_open(monkeypatch):
    class _Boom:
        def query(self, op, params):
            raise RuntimeError('storage down')

    monkeypatch.setattr(
        mq, '_queue_client', lambda **_kwargs: _Boom())
    assert mq._conv_has_wake_peer_row(
        'c' * 32, user_id=USER_ID) is True


# ── drain_idle_peer_messages honours the flag ─────────────────────────


def test_idle_drain_skips_no_wake_only_conv(monkeypatch, sidecar_rows):
    sidecar_rows['rows'] = [
        _peer_row('c' * 32, {'text': 'fyi', '_peerNoWake': True}),
    ]
    monkeypatch.setattr(
        mq, 'list_conversations_with_pending_peer_messages',
        lambda: [{'convId': 'c' * 32, 'userId': USER_ID}],
    )
    dispatched = []
    monkeypatch.setattr(mq, 'dispatch_next_queued',
                        lambda cid, **k: dispatched.append(cid) or 't' * 32)
    assert mq.drain_idle_peer_messages() == []
    assert dispatched == []


def test_idle_drain_fires_when_a_wake_row_exists(monkeypatch, sidecar_rows):
    sidecar_rows['rows'] = [
        _peer_row('c' * 32, {'text': 'fyi', '_peerNoWake': True}),
        _peer_row('c' * 32, {'text': 'please confirm'}),
    ]
    monkeypatch.setattr(
        mq, 'list_conversations_with_pending_peer_messages',
        lambda: [{'convId': 'c' * 32, 'userId': USER_ID}],
    )
    dispatched = []
    monkeypatch.setattr(mq, 'dispatch_next_queued',
                        lambda cid, **k: dispatched.append(cid) or 't' * 32)
    spawned = mq.drain_idle_peer_messages()
    assert dispatched == ['c' * 32]
    assert spawned == ['t' * 32]
