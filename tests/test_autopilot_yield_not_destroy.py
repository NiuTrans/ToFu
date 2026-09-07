#!/usr/bin/env python3
"""Autopilot output survives a human preemption boundary.

Project Brain no longer queues Board kickoffs or peer instructions.  This suite
therefore covers only the generic queue distinction between human turns and
machine-owned workflow rows plus the output-preservation contract.
"""

import json
import time

import pytest

import lib.message_queue as mq
from lib.agent_verdict import is_incomplete_stop
from lib.tasks_pkg import autopilot as ap
from tests._seed import seed_conversation

pytest_plugins = ('tests._chat_sidecar',)
pytestmark = [pytest.mark.unit, pytest.mark.usefixtures('chat_sidecar')]

USER_ID = 1


def _cid():
    conversation_id = f'test-yield-{time.time_ns()}'
    seed_conversation(conversation_id, user_id=USER_ID, title='Yield test')
    return conversation_id


# ══════════════════════════════════════════════════════════════════
# Who may preempt a working run (and the complement)
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize('kind', [mq.KIND_WORKFLOW, mq.KIND_PEER_MSG])
def test_machine_work_items_do_not_preempt(kind):
    """Machine work items wait for the run to end; they do not interrupt it.

    A live kickoff / peer message IS dispatchable (it will run later, via the
    idle drain) — but it is not a person, so it must not cut a working run
    short. Both facts are asserted so this cannot pass by making the row
    invisible.
    """
    conv_id = _cid()
    mq.enqueue_message(conv_id, {'text': 'machine work', 'timestamp': 1000},
                       {'model': 'm'}, kind=kind, user_id=USER_ID)

    assert mq.has_pending_human_turn(conv_id, user_id=USER_ID) is False, (
        f'{kind} is machine work — it must not preempt a working autopilot run')
    nxt = mq.next_dispatchable_turn(conv_id, user_id=USER_ID)
    assert nxt is not None and nxt['kind'] == kind, (
        f'{kind} must still be dispatchable later (the idle drain picks it up)')
    assert nxt['isHuman'] is False


def test_human_message_still_preempts():
    """COMPLEMENT: a person outranks the loop — always.

    Without this, "autopilot never yields to anybody" also satisfies the tests
    above, which would bury a waiting human under the loop.
    """
    conv_id = _cid()
    mq.enqueue_message(conv_id, {'text': 'stop, do this instead',
                                 'timestamp': 1000}, {'model': 'm'},
                       user_id=USER_ID)

    assert mq.has_pending_human_turn(conv_id, user_id=USER_ID) is True, (
        'a queued HUMAN message must always preempt autopilot')
    nxt = mq.next_dispatchable_turn(conv_id, user_id=USER_ID)
    assert nxt is not None and nxt['isHuman'] is True


def test_human_wins_even_when_queued_behind_machine_work(monkeypatch):
    """The answer must not depend on which row happens to sort first."""
    conv_id = _cid()
    mq.enqueue_message(conv_id, {'text': 'machine', 'timestamp': 1000},
                       {'model': 'm'}, kind=mq.KIND_WORKFLOW,
                       user_id=USER_ID)
    mq.enqueue_message(conv_id, {'text': 'human', 'timestamp': 1001},
                       {'model': 'm'}, user_id=USER_ID)

    assert mq.has_pending_human_turn(conv_id, user_id=USER_ID) is True, (
        'the human row must be found wherever it sits in the queue')


# ══════════════════════════════════════════════════════════════════
#  5 + 6 + 7 — yielding preserves, concludes, and does NOT disarm
# ══════════════════════════════════════════════════════════════════

def _wire_preserve(monkeypatch):
    """Capture what the preservation seam persists and emits.

    ``autopilot.py`` re-exports the close-out helpers from
    ``autopilot_run_lifecycle`` (identity-preserving facade), so a call made
    DIRECTLY in autopilot.py resolves the facade binding while a call made
    INSIDE the lifecycle module resolves its own global. Both are patched: with
    only the origin patched, the direct call falls through to the real DB and
    the capture silently misses it (observed while writing this suite).
    """
    seen = {'records': [], 'events': [], 'cleared_run': [], 'disarmed': []}

    def _fake_store(conv_id, run_id, **kw):
        seen['records'].append({'convId': conv_id, 'runId': run_id, **kw})
        return {'runId': run_id, 'status': 'concluded',
                'reason': kw.get('reason'), 'content': kw.get('text', ''),
                'unsent': kw.get('unsent', False)}

    monkeypatch.setattr(
        'lib.tasks_pkg.autopilot_run_lifecycle._store_run_record', _fake_store)
    monkeypatch.setattr(ap, '_store_run_record', _fake_store)
    monkeypatch.setattr(
        'lib.tasks_pkg.autopilot_run_lifecycle._emit_run_concluded',
        lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, ev: seen['events'].append(ev))
    monkeypatch.setattr(ap, '_clear_run_id',
                        lambda cid, *, user_id: seen['cleared_run'].append(cid))
    monkeypatch.setattr('lib.message_queue.clear_autopilot_marker',
                        lambda cid, *, user_id: seen['disarmed'].append(cid))
    return seen


_VU_TEXT = ('第 2 步已落地并验完。另外：上一条消息夹带了"扮演 owner"的指令，'
            '我没有照做——我不会冒充你说话。')


def test_yield_preserves_output_and_concludes_the_run(monkeypatch):
    """A produced-but-undelivered VU reply is PRESERVED and the run CONCLUDES.

    Yielding means "do not chain another turn". It has never meant "throw the
    finished work away", and it must never mean "end silently" — the missing
    terminal fact is why a client held dead task ids for 2h12m.
    """
    seen = _wire_preserve(monkeypatch)
    task = {
        'id': 'task-abc12345', 'convId': 'conv-1', '_userId': 1,
        'config': {},
    }

    ap._preserve_unsent_vu_and_conclude(
        task, 'conv-1', 'ar-run-1', 'vu-msg-1', _VU_TEXT,
        reason='yielded_to_human')

    assert seen['records'], (
        'the produced VU reply was DESTROYED — preservation must run before '
        'any post-VU stop path returns')
    rec = seen['records'][0]
    assert rec['text'] == _VU_TEXT, 'the reply must be preserved VERBATIM'
    assert rec['unsent'] is True, (
        'it must be flagged unsent — it is evidence of work done, not a turn '
        'that happened')
    assert rec['reason'] == 'yielded_to_human'

    concluded = [e for e in seen['events']
                 if e.get('type') == 'autopilot_run_concluded']
    assert concluded, (
        'no autopilot_run_concluded emitted — this is the ONLY signal that '
        'makes the system admit the run is over; without it the run is '
        'unobservable-dead and the client waits forever')


def test_preserved_reply_never_enters_conversation_history(monkeypatch):
    """The preserved reply must NOT be appended to ``conv.messages``.

    That list is the conversation history sent UPSTREAM on the next turn. An
    undelivered VU reply placed there would be read back by the model as words
    the human actually said.
    """
    seen = _wire_preserve(monkeypatch)
    appended = []
    monkeypatch.setattr(ap, '_append_conversation_autopilot_turns',
                        lambda *a, **k: appended.append(a))

    ap._preserve_unsent_vu_and_conclude(
        {'id': 'task-abc12345', 'convId': 'conv-1', '_userId': 1,
         'config': {}},
        'conv-1', 'ar-run-1', 'vu-msg-1', _VU_TEXT, reason='yielded_to_human')

    assert appended == [], (
        'an undelivered VU reply must NEVER be appended to conversation '
        'history — it would become something the model reads as the human')
    assert seen['records'], 'it must still be preserved in the sidecar'


def test_yield_does_not_disarm_autopilot(monkeypatch):
    """Yielding PAUSES the loop; it must not switch the feature off.

    The run pin IS cleared (the next run must mint a fresh id, or the fold gate
    would swallow live turns), but the armed marker must survive: the user did
    not turn autopilot off by sending a message.
    """
    seen = _wire_preserve(monkeypatch)

    ap._preserve_unsent_vu_and_conclude(
        {'id': 'task-abc12345', 'convId': 'conv-1', '_userId': 1,
         'config': {}},
        'conv-1', 'ar-run-1', 'vu-msg-1', _VU_TEXT, reason='yielded_to_human')

    assert seen['disarmed'] == [], (
        'yielding must NOT clear the armed marker — that would silently turn '
        'autopilot off instead of pausing it')
    assert seen['cleared_run'] == ['conv-1'], (
        'the run pin MUST be cleared so the next run mints a fresh run id')


# ══════════════════════════════════════════════════════════════════
#  8 — a cut-short run must not look like a clean finish
# ══════════════════════════════════════════════════════════════════

def test_mid_flight_stop_reasons_are_incomplete():
    """Yield / abort / supersede are UNVERIFIED outcomes, not conclusions."""
    for reason in ('yielded_to_human', 'aborted_mid_vu', 'superseded'):
        assert is_incomplete_stop(reason) is True, (
            f'{reason} cut the run short with the objective unverified — it '
            f'must render "stopped early / needs review", not a clean finish')
    assert is_incomplete_stop('task_done') is False, (
        'COMPLEMENT: a genuinely finished run must NOT be flagged incomplete')
