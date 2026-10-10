#!/usr/bin/env python3
"""VU preemption: a REAL queued message aborts the in-flight autopilot VU call.

WHY (owner-ratified 2026-07-25, acceptance criterion "messages must start
IMMEDIATELY")
--------------------------------------------------------------------------------
The autopilot deferral (``_has_pending_real_message`` in
``maybe_run_autopilot``) fired only AFTER ``run_virtual_user`` returned — the
VU LLM call ran to completion first. Production incident 2026-07-25: the
user's message arrived mid-VU and waited TWO full rounds (94s + 74s) before
the deferral let the queue dispatch. That is not "immediately".

FIX (three parts)
-----------------
1. ``lib/message_queue._preempt_autonomous_work_for_real_message`` — called from
   ``enqueue_message`` for KIND_REAL rows ONLY (a human typing preempts;
   peer/workflow rows keep the cheap wait-for-completion deferral — their
   latency is not user-visible). Stamps ``aborted`` +
   ``_abort_reason='real_message_preempts_vu'`` + ``_abort_timestamp`` on
   the conv's live VU sub-task. The abort seam is PER-CHUNK in the SSE
   loop (lib/llm/stream.py:163-166) + per-round in the orchestrator, so
   the VU unwinds at the next chunk / round boundary — seconds, not
   minutes. (Honest bound: a preempt landing during a hung first-token
   wait still waits for that TTFT to resolve.)
2. ``run_virtual_user`` preemption branch — a sub-task killed this way
   returns None immediately (skipping verdict / segment post-processing of
   the partial reply); ``maybe_run_autopilot``'s existing None branch then
   emits AUTOPILOT_VU_CANCEL and the completion hook dispatches the queued
   turn. Distinct from a parent-Stop abort and from a transient VU error.
3. Post-creation pre-flight — a REAL message landing BETWEEN
   maybe_run_autopilot's eligibility check and create_task would otherwise
   wait out the whole call; the fresh sub-task is aborted before its first
   round, closing the creation race.

NEUTER (manual A/B): deleting the autonomous-work preemption
call from ``enqueue_message`` turns test 1 red; deleting the preemption
branch from ``run_virtual_user`` turns test 6 red (it would return a
partial-text dict instead of None).
"""

from __future__ import annotations

import threading
import time
import uuid

import pytest

pytestmark = pytest.mark.unit
pytest_plugins = ('tests._chat_sidecar',)

_unit = pytest.mark.unit


@pytest.fixture(autouse=True)
def _queue_storage_authority(chat_sidecar):
    """Exercise queue preemption through the production sidecar authority."""
    yield chat_sidecar


def _seeded_conversation_id() -> str:
    """Create the owner-scoped conversation required by queue authority."""
    from tests._seed import seed_conversation

    conversation_id = 'cv-' + uuid.uuid4().hex[:12]
    seed_conversation(
        conversation_id,
        user_id=1,
        title='VU preemption fixture',
        messages=[],
    )
    return conversation_id


@pytest.fixture
def registry():
    from tests.support.chat_tasks import chat_task_fixture_guard as tasks_lock, chat_task_registry as tasks
    owned = []

    def _put(task: dict) -> dict:
        with tasks_lock:
            tasks[task['id']] = task
        owned.append(task['id'])
        return task

    yield _put

    with tasks_lock:
        for tid in owned:
            tasks.pop(tid, None)


def _mk_vu(conv_id: str, *, status: str = 'running', aborted: bool = False) -> dict:
    return {
        'id': 'vu-' + uuid.uuid4().hex[:8],
        'convId': conv_id,
        '_userId': 1,
        'status': status,
        'aborted': aborted,
        '_vu_subtask': True,
        '_inline_messages': True,
        'events': [],
        'events_lock': threading.Lock(),
        'config': {},
    }


def _enqueue_real(conv_id: str):
    from lib.message_queue import enqueue_message
    return enqueue_message(conv_id, {'text': '真人消息', 'timestamp': 1},
                           {'model': 'test-model'}, user_id=1)


# ────────────────────────── 1. the preempt trigger ──────────────────────────

@_unit
def test_real_enqueue_preempts_live_vu_subtask(registry):
    """THE FIX: a KIND_REAL enqueue aborts the conv's live VU sub-task with
    the preemption reason stamp."""
    conv_id = _seeded_conversation_id()
    vu = registry(_mk_vu(conv_id))
    # A normal worker task must NOT be touched.
    worker = registry({'id': 'worker-1', 'convId': conv_id, '_userId': 1,
                       'status': 'running',
                       'events': [], 'events_lock': threading.Lock(), 'config': {}})

    res = _enqueue_real(conv_id)
    assert res.get('queueId'), 'enqueue itself must succeed'
    assert vu.get('aborted') is True, 'real message must abort the in-flight VU'
    assert vu.get('_abort_reason') == 'real_message_preempts_vu'
    assert vu.get('_abort_timestamp'), 'abort timestamp must be stamped'
    assert not worker.get('aborted'), 'the preemption must NEVER touch worker tasks'


@_unit
def test_real_enqueue_preempts_flow_managed_goal_root_only(registry):
    """The single new Goal owner yields to a later durable human command."""
    conv_id = _seeded_conversation_id()
    goal = registry({
        'id': 'goal-root-' + uuid.uuid4().hex[:8],
        'convId': conv_id,
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        '_flow_managed': True,
        'flow_mode': True,
        'events': [],
        'events_lock': threading.Lock(),
        'config': {'autopilot': True},
    })
    custom_flow = registry({
        'id': 'custom-flow-' + uuid.uuid4().hex[:8],
        'convId': conv_id,
        '_userId': 1,
        'status': 'running',
        'aborted': False,
        '_flow_managed': True,
        'flow_mode': True,
        'events': [],
        'events_lock': threading.Lock(),
        'config': {'flowId': 'keep-running'},
    })

    _enqueue_real(conv_id)

    assert goal['aborted'] is True
    assert goal['_abort_reason'] == 'superseded_by_human'
    assert not custom_flow.get('aborted')


@_unit
def test_peer_enqueue_does_not_preempt(registry):
    """Peer messages keep the cheap wait-for-completion deferral — their
    latency is not user-visible, so killing a paid VU call for them is
    waste. KIND_PEER_MSG must NOT preempt."""
    from lib.message_queue import KIND_PEER_MSG, enqueue_message
    conv_id = _seeded_conversation_id()
    vu = registry(_mk_vu(conv_id))
    enqueue_message(conv_id, {'text': 'peer note', '_peerMessage': True},
                    {'model': 'test-model'}, kind=KIND_PEER_MSG, user_id=1)
    assert not vu.get('aborted'), 'peer message must not preempt the VU'


@_unit
def test_workflow_enqueue_does_not_preempt(registry):
    from lib.message_queue import KIND_WORKFLOW, enqueue_message
    conv_id = _seeded_conversation_id()
    vu = registry(_mk_vu(conv_id))
    enqueue_message(conv_id, {'text': 'brain kickoff'}, {'model': 'test-model'},
                    kind=KIND_WORKFLOW, user_id=1)
    assert not vu.get('aborted'), 'workflow kickoff must not preempt the VU'


@_unit
def test_no_vu_is_noop(registry):
    conv_id = _seeded_conversation_id()
    res = _enqueue_real(conv_id)
    assert res.get('queueId'), 'enqueue works fine with no VU around'


@_unit
def test_preempt_skips_terminal_or_already_aborted_vu(registry):
    conv_id = _seeded_conversation_id()
    done_vu = registry(_mk_vu(conv_id, status='done'))
    gone_vu = registry(_mk_vu(conv_id, aborted=True))
    _enqueue_real(conv_id)
    assert not done_vu.get('_abort_reason'), 'a settled VU is not re-stamped'
    assert gone_vu.get('_abort_reason') != 'real_message_preempts_vu', (
        'an already-aborted VU keeps its original reason'
    )


# ───────────────── 2. run_virtual_user routing on a preempted sub-task ─────────────────
