"""Exactly-once contract for the background-command injection lane.

A detached ``run_command`` completion is dual-written: a durable
``message_queue`` row (the delivery authority — settlement drain, dispatch,
and startup orphan-redispatch all guarantee delivery from it) plus a volatile
``agent_inbox`` twin (``mode='background-command'``, tagged with the row's
``queueId``) that a still-running turn drains at its next round boundary.
Both races collapse to exactly-once:

  * FORWARD (inbox drains first): the post-LLM deferred flush confirms
    consumption, emits the BACKGROUND_COMMAND_INJECT chip, accumulates the
    display-only ``_bgCommandInjects`` sidecar, and deletes the durable row.
  * REVERSE (turn ends first): ``dispatch_next_queued`` pops the durable row
    as a fresh turn and ``consume_peer`` drops the now-redundant twin.

An abort before the flush leaves the durable row untouched → late delivery,
never zero.

Run::

    python -m pytest tests/test_background_command_injection_lane.py -v
"""

from __future__ import annotations

import pathlib

import pytest

from lib import agent_inbox
from lib.tasks_pkg.handlers import _background_command as background
from lib.tasks_pkg.orchestrator._deferred_inbox_flush import (
    flush_deferred_peer_and_steer,
)
from lib.tasks_pkg.orchestrator._swarm_inbox import drain_and_inject_inbox

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[1]
MESSAGE_QUEUE_PY = ROOT / 'lib' / 'message_queue.py'

CONV = 'conv-bg-1'
PAYLOAD = (
    '<background-command id="bg_1" status="completed">\n'
    '$ pytest\n3 passed\n[exit code: 0]\n</background-command>'
)


@pytest.fixture(autouse=True)
def _clean_inbox():
    agent_inbox.reset_for_test()
    yield
    agent_inbox.reset_for_test()


def _task():
    return {
        'id': 'task-1',
        'convId': CONV,
        '_userId': 7,
        'aborted': False,
        'toolRounds': [],
    }


def _enqueue_twin(queue_id='q-1'):
    agent_inbox.enqueue(
        CONV,
        PAYLOAD,
        priority='next',
        mode='background-command',
        extra={'queueId': queue_id, 'commandId': 'bg_1', 'command': 'pytest'},
    )


def test_completion_dual_writes_durable_row_and_inbox_twin(monkeypatch):
    enqueued = []

    monkeypatch.setattr(
        'lib.message_queue.enqueue_message',
        lambda conv_id, message, config, kind, *, user_id: (
            enqueued.append((conv_id, message, kind, user_id))
            or {'queueId': 'q-1'}),
    )
    monkeypatch.setattr(
        'lib.message_queue.dispatch_next_queued',
        lambda conv_id, *, user_id: None)

    background._queue_completion(
        task=_task(),
        config={'model': 'test'},
        command_id='bg_1',
        command='pytest',
        result='$ pytest\n3 passed\n[exit code: 0]',
    )

    assert enqueued[0][0] == CONV
    assert enqueued[0][1]['_backgroundCommand'] == 'bg_1'
    # Restart-recoverability pin: the authority row must use the
    # dispatchable workflow kind so startup orphan-redispatch picks it up.
    from lib.message_queue import KIND_WORKFLOW
    assert enqueued[0][2] == KIND_WORKFLOW
    twin = agent_inbox.drain(CONV, modes=['background-command'])
    assert len(twin) == 1
    assert twin[0]['queueId'] == 'q-1'
    assert twin[0]['commandId'] == 'bg_1'
    assert twin[0]['command'] == 'pytest'
    assert '<background-command id="bg_1"' in twin[0]['value']


def test_round_boundary_drain_stashes_bgcmd_lane_without_swarm_chip():
    _enqueue_twin()
    task = _task()
    messages = [{'role': 'user', 'content': 'go'}]

    drain_and_inject_inbox(task=task, messages=messages, round_num=0,
                           tid='task-1')

    injected = messages[-1]
    assert injected['role'] == 'user'
    assert injected.get('_isInboxInject') is True
    assert '<background-command' in injected['content']
    pending = task.get('_bgcmd_inject_pending') or []
    assert [item['queueId'] for item in pending] == ['q-1']
    # Regression pin: the swarm drain must EXCLUDE the background-command
    # lane — a twin folded into _swarm_items would emit the wrong chip and
    # skip the durable-row de-dup (double delivery).
    assert '_inboxInjects' not in task


def test_flush_confirms_consumption_and_deletes_durable_row(monkeypatch):
    dedup_calls = []
    events = []
    monkeypatch.setattr(
        'lib.message_queue.dedup_inbox_durable_rows',
        lambda conv_id, queue_ids, *, user_id: dedup_calls.append(
            (conv_id, list(queue_ids), user_id)))
    import lib.tasks_pkg.orchestrator._deferred_inbox_flush as flush_mod
    monkeypatch.setattr(
        flush_mod, 'append_event',
        lambda task, event: events.append(event))

    task = _task()
    task['_bgcmd_inject_pending'] = [
        {'queueId': 'q-1', 'commandId': 'bg_1', 'value': PAYLOAD},
    ]

    flush_deferred_peer_and_steer(task, round_num=2, tid='task-1')

    assert '_bgcmd_inject_pending' not in task
    sidecar = task['_bgCommandInjects']
    assert len(sidecar) == 1
    assert sidecar[0]['round'] == 3
    assert sidecar[0]['count'] == 1
    assert sidecar[0]['previews'] == [
        {'commandId': 'bg_1', 'text': PAYLOAD[:1200]},
    ]
    assert dedup_calls == [(CONV, ['q-1'], 7)]
    assert len(events) == 1
    assert events[0]['type'] == 'background_command_inject'
    assert events[0]['roundNum'] == 3
    assert events[0]['count'] == 1


def test_drain_alone_never_deletes_the_durable_row(monkeypatch):
    """Abort-before-flush (never-zero): the drain only stashes; the durable
    row survives for fresh-turn redelivery."""
    dedup_calls = []
    monkeypatch.setattr(
        'lib.message_queue.dedup_inbox_durable_rows',
        lambda *args, **kwargs: dedup_calls.append((args, kwargs)))

    _enqueue_twin()
    task = _task()
    messages = [{'role': 'user', 'content': 'go'}]
    drain_and_inject_inbox(task=task, messages=messages, round_num=0,
                           tid='task-1')

    assert dedup_calls == []
    assert task.get('_bgcmd_inject_pending')


def test_reverse_race_consume_peer_drops_the_bgcmd_twin():
    _enqueue_twin(queue_id='q-9')

    removed = agent_inbox.consume_peer(CONV, ['q-9'])

    assert removed == 1
    assert agent_inbox.peek(CONV) == 0


def test_dispatch_reverse_race_guard_covers_background_command():
    """The dispatch-time twin-drop in message_queue must trigger on the
    ``_backgroundCommand`` payload marker, not only on peer messages."""
    src = MESSAGE_QUEUE_PY.read_text()
    assert 'payload.get("_peerMessage") or payload.get("_backgroundCommand")' \
        in src
