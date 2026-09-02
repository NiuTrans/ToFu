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
1. ``lib/message_queue._preempt_vu_subtask_for_real_message`` — called from
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

NEUTER (manual A/B): deleting the ``_preempt_vu_subtask_for_real_message``
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

def _parent_task(conv_id: str) -> dict:
    return {
        'id': 'parent-' + uuid.uuid4().hex[:6],
        'convId': conv_id,
        '_userId': 1,
        'status': 'done',
        'aborted': False,
        'config': {},
        'messages': [
            {'role': 'user', 'content': 'objective please'},
            {'role': 'assistant', 'content': 'working on it'},
        ],
        'events': [],
        'events_lock': threading.Lock(),
    }


@_unit
def test_preempted_vu_returns_none_skipping_postprocessing(registry, monkeypatch):
    """A sub-task killed by real-message preemption must short-circuit to
    None — never feeding its partial reply through the verdict / segment
    pipeline (which would manufacture a synthetic user turn out of a corpse).
    Pre-fix this returns a {'text': ...} dict → RED."""
    import lib.tasks_pkg.autopilot as ap
    conv_id = _seeded_conversation_id()
    parent = _parent_task(conv_id)

    sub = {
        'id': 'vu-preempted-1', 'convId': conv_id, 'status': 'running',
        '_userId': 1,
        'aborted': True, '_abort_reason': 'real_message_preempts_vu',
        '_abort_timestamp': time.time(),
        'events': [], 'events_lock': threading.Lock(), 'config': {},
        'toolRounds': [],
    }
    registry(sub)
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.create_task', lambda *a, **k: sub)
    monkeypatch.setattr(
        'lib.tasks_pkg.orchestrator._turn._run_single_turn',
        lambda t, **k: {'content': 'partial vu reply', 'error': None,
                        'thinking': '', 'usage': {}, 'messages': []},
    )

    out = ap.run_virtual_user(parent, vu_msg_id='vu-msg-1')
    assert out is None, (
        'a preempted VU must return None so maybe_run_autopilot takes the '
        'deferral outcome (vu_cancel + queue dispatch), not a partial reply'
    )


@_unit
def test_creation_race_aborted_before_first_round(registry, monkeypatch):
    """A REAL message that landed between maybe_run_autopilot's eligibility
    check and create_task: the fresh sub-task must be aborted BEFORE its
    first round (the pre-flight check), or it would run the whole call."""
    import lib.tasks_pkg.autopilot as ap
    conv_id = _seeded_conversation_id()
    parent = _parent_task(conv_id)
    _enqueue_real(conv_id)   # message arrives BEFORE the VU sub-task exists

    sub = {
        'id': 'vu-race-1', 'convId': conv_id, 'status': 'running',
        '_userId': 1,
        'aborted': False,
        'events': [], 'events_lock': threading.Lock(), 'config': {},
        'toolRounds': [],
    }
    registry(sub)
    monkeypatch.setattr(
        'lib.tasks_pkg.manager.create_task', lambda *a, **k: sub)
    seen = {}

    def _fake_run(t, **k):
        seen['aborted_at_call'] = bool(t.get('aborted'))
        seen['abort_reason'] = t.get('_abort_reason')
        return {'content': '', 'error': None, 'thinking': '', 'usage': {},
                'messages': []}

    monkeypatch.setattr('lib.tasks_pkg.orchestrator._turn._run_single_turn', _fake_run)
    out = ap.run_virtual_user(parent, vu_msg_id='vu-msg-2')
    assert seen.get('aborted_at_call') is True, (
        'the creation-race message must abort the VU sub-task before round 1'
    )
    assert seen.get('abort_reason') == 'real_message_preempts_vu'
    assert out is None
