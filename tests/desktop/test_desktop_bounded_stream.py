"""Consuming desktop artifact streams retain bounded bytes and replay watermarks."""
import threading

import pytest

from lib.desktop import bridge
from lib.desktop_agent._runtime import Outbox

pytestmark = pytest.mark.unit


@pytest.fixture
def stream(monkeypatch):
    for name in ('command_queue', '_streams', '_agents'):
        monkeypatch.setattr(bridge, name, {})
    bridge.register_agent('agent', {'name': 'fixture'}, user_id='7')
    address = {'target_agent_id': 'agent', 'user_id': '7'}
    command_id, error = bridge.enqueue_desktop_command('paper_build', {}, **address,
        stream_limits={'bytes': 16, 'frames': 4, 'frame_bytes': 8})
    assert not error
    assert bridge.take_pending_commands(agent_id='agent', user_id='7')[0]['id'] == command_id
    return command_id, address


def upload(command_id, sequence, data='abcd', *, done=False, owner='7'):
    return bridge.resolve_streams([{'cmd_id': command_id, 'seq': sequence, 'stream': 'artifact',
                                   'data': data, 'done': done}], agent_id='agent', user_id=owner)


def test_stream_drains_contiguous_frames_and_deduplicates_after_consumption(stream):
    command_id, address = stream
    assert upload(command_id, 2) == 1
    assert bridge.consume_desktop_command(command_id, **address)['frames'] == []
    assert upload(command_id, 1) == 1
    assert len(bridge.consume_desktop_command(command_id, **address)['frames']) == 2
    assert bridge._streams[command_id]['bytes'] == 0
    assert upload(command_id, 2) == 0
    assert upload(command_id, 3, done=True, owner='8') == 0
    assert bridge.consume_desktop_command(command_id, **{**address, 'user_id': '8'}) is None
    assert upload(command_id, 3, done=True) == 1
    assert bridge.consume_desktop_command(command_id, **address)['stream_done']
    assert not bridge.release_desktop_command(command_id, **{**address, 'user_id': '8'})
    assert command_id in bridge.command_queue
    assert bridge.release_desktop_command(command_id, **address)
    assert command_id not in bridge._streams and command_id not in bridge.command_queue


def test_stream_pressure_fails_explicitly_without_growing_or_discarding_old_bytes(stream):
    command_id, address = stream
    assert upload(command_id, 1, '12345678') == 1
    assert upload(command_id, 2, '12345678') == 1
    assert upload(command_id, 3) == 0
    assert bridge._streams[command_id]['bytes'] == 16
    state = bridge.consume_desktop_command(command_id, **address)
    assert state['settled'] and 'bounds' in state['error']
    assert len(state['frames']) == 2


def test_invalid_unicode_frame_is_a_command_failure_and_does_not_break_the_poll(stream):
    command_id, address = stream
    assert upload(command_id, 1, '\ud800') == 0
    state = bridge.consume_desktop_command(command_id, **address)
    assert state['settled'] and state['error'] and state['frames'] == []


def test_cancellation_commands_retire_after_claim_or_expiry(stream, monkeypatch):
    _, address = stream
    command, error = bridge.enqueue_desktop_command('paper_build_cancel', {'cmd_id': 'cancelled'},
                                                     **address, ttl=5, fire_and_forget=True)
    assert not error
    assert any(row['id'] == command for row in bridge.take_pending_commands(agent_id='agent', user_id='7'))
    assert command not in bridge.command_queue
    command, error = bridge.enqueue_desktop_command('paper_build_cancel', {'cmd_id': 'cancelled'},
                                                     **address, ttl=5, fire_and_forget=True)
    assert not error
    now = bridge.time.time()
    monkeypatch.setattr(bridge.time, 'time', lambda: now + 6)
    bridge.take_pending_commands(agent_id='agent', user_id='7')
    assert command not in bridge.command_queue


def test_cancelled_artifact_producer_cannot_publish_a_new_spooled_frame():
    lock = threading.Condition(threading.RLock())
    box = Outbox(lock, threading.Event(), 1, 100)
    try:
        box.append({'first': True})
        box.append({'second': True})  # No count-capacity wait before cancellation.
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(RuntimeError, match='cancelled'):
            box.append({'third': True}, cancel=cancel)
        assert len(box._records) == 2 and list(box) == [{'first': True}]
    finally:
        box.close()
