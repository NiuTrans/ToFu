#!/usr/bin/env python3
"""Autopilot: yielding must not DESTROY, and only a HUMAN may preempt.

THE INCIDENT (conv ms3s8s0kjlvq18, 2026-07-28)
----------------------------------------------
Two gates read the SAME ``message_queue`` row and reached OPPOSITE verdicts in
the same second:

  06:45:54  the project brain queued a kickoff (``kind=workflow_step``)
  06:54:38  the VU itself marked that epic ``done`` via project_board_complete
  06:55:13  gate ① ``_has_pending_real_message`` → ``get_queue_depth`` (filter:
            ``kind != KIND_AUTOPILOT``) counted the row as "a human is waiting"
            → DISCARDED a finished 24-round / 15-minute VU reply
  06:55:13  gate ② ``dispatch_next_queued`` → ``_brain_kickoff_still_wanted``
            re-checked the board, saw ``done`` → discarded the row, spawned
            NOTHING

Net effect: the agent's question was never answered, the produced reply existed
only as a truncated log line, the armed marker was cleared (so crash-resume
would never revisit it) and a connected client held two dead task ids for 2h12m
(``SyncDrift STALLED age=7920s``). No red signal anywhere.

WHAT THIS SUITE PINS (owner-directed, 2026-07-28)
-------------------------------------------------
Behaviour, never implementation — per the charter's "assert the RESULT" rule.
Narrowing the kind check alone would have fixed that one instance and left the
CAUSE (two readers, two filter sets), so the assertions below are written
against the two invariants, not against the bug:

  1. ``test_stale_kickoff_does_not_destroy_vu_output`` — THE incident, replayed:
     a queued brain kickoff whose epic already finished must NOT be read as a
     reason to stand down, so the VU output survives as a real turn.
  2. ``test_dispatch_and_yield_gate_agree_on_the_same_row`` — the two readers
     must never disagree about one row again (the structural cause).
  3. ``test_machine_work_items_do_not_preempt`` — workflow / peer rows are
     machine work: they wait for the run to end.
  4. ``test_human_message_still_preempts`` — THE COMPLEMENT. Without it,
     "autopilot never yields to anyone" would also pass, which would bury a
     human under the loop — a worse bug than the one being fixed.
  5. ``test_yield_preserves_output_and_concludes_the_run`` — yielding to a
     human still preserves the produced reply in the SIDECAR and emits the
     terminal ``autopilot_run_concluded`` fact.
  6. ``test_preserved_reply_never_enters_conversation_history`` —
     ``conv.messages`` is the history sent UPSTREAM; an undelivered VU reply
     there would read back to the model as words the human actually said.
  7. ``test_yield_does_not_disarm_autopilot`` — yielding PAUSES; it must not
     silently turn the feature off.
  8. ``test_mid_flight_stop_reasons_are_incomplete`` — a cut-short run must
     never render as a clean conclusion.

NEUTER (run manually to prove these bite; each MUST turn the suite red):
  N1  ``has_pending_human_turn``: ``any(r['isHuman'] …)`` → ``bool(rows)``
      (i.e. revert to the old "any non-autopilot row" judgement)  → 1,2,3 red
  N2  ``_row_is_dispatchable``: ``return True`` unconditionally      → 1,2 red
  N3  ``_preserve_unsent_vu_and_conclude``: drop the ``_store_run_record``
      call                                                          → 5 red
  N4  ``_preserve_unsent_vu_and_conclude``: drop the
      ``_emit_run_concluded_event`` call                            → 5 red
  N5  ``has_pending_human_turn``: ``return False`` unconditionally   → 4 red
"""

import json
import time

import pytest

import lib.message_queue as mq
from lib.agent_verdict import is_incomplete_stop
from lib.tasks_pkg import autopilot as ap

pytestmark = pytest.mark.unit


def _cid():
    return f'test-yield-{time.time_ns()}'


def _db():
    mq._maybe_ensure_table()
    return mq.get_thread_db(mq.DOMAIN_CHAT)


def _queue_brain_kickoff(conv_id, board_task_id, project_path='/proj'):
    """Enqueue a brain kickoff exactly as ``dispatch_epic`` does."""
    return mq.enqueue_message(
        conv_id,
        {'text': 'go work this epic', 'boardTaskId': board_task_id,
         'timestamp': 1000},
        {'model': 'm', 'projectPath': project_path},
        kind=mq.KIND_WORKFLOW)['queueId']


def _board_says(monkeypatch, status):
    """Point the board lookup at a single epic with ``status``."""
    monkeypatch.setattr(
        'lib.conversations.project_board.read_board',
        lambda project_path: {'tasks': [{'id': 'pt_epic', 'status': status,
                                         'owner_conv_id': ''}]})


# ══════════════════════════════════════════════════════════════════
#  1 + 2 — THE incident, and the structural cause behind it
# ══════════════════════════════════════════════════════════════════

def test_stale_kickoff_does_not_destroy_vu_output(monkeypatch):
    """A kickoff whose epic already finished is NOT someone to stand down for.

    The exact 06:55:13 state: one queued brain kickoff, its epic already
    ``done``. Before the fix this made autopilot throw away a completed VU
    reply while the dispatcher discarded the row and spawned nothing.
    """
    conv_id = _cid()
    _queue_brain_kickoff(conv_id, 'pt_epic')
    _board_says(monkeypatch, 'done')

    assert mq.has_pending_human_turn(conv_id) is False, (
        'a stale brain kickoff must NOT read as "a human is waiting" — that '
        'reading is what destroyed a finished VU turn on 2026-07-28')
    assert mq.next_dispatchable_turn(conv_id) is None, (
        'nothing here will become a turn, so there is nobody to yield to')


def test_dispatch_and_yield_gate_agree_on_the_same_row(monkeypatch):
    """The two readers must never reach opposite verdicts on one row again.

    This is the CAUSE, asserted directly: whatever the dispatcher would do with
    the head row, the yield gate must agree. Drives the real dispatch path so
    the agreement is observed end-to-end, not asserted about a helper.
    """
    conv_id = _cid()
    _queue_brain_kickoff(conv_id, 'pt_epic')
    _board_says(monkeypatch, 'done')

    # What the yield gate believes.
    gate_sees_a_turn = mq.next_dispatchable_turn(conv_id) is not None

    # What the dispatcher actually DOES (stub the spawn pipeline).
    spawned = []
    monkeypatch.setattr(mq, '_append_user_msg_with_cas',
                        lambda db, cid, msg: True)
    monkeypatch.setattr(
        'lib.tasks_pkg.conv_message_builder.build_api_messages_from_db',
        lambda cid, cfg: [{'role': 'user', 'content': 'x'}])
    monkeypatch.setattr(
        'lib.tasks_pkg.create_task',
        lambda cid, msgs, cfg: {'id': 't-1', 'convId': cid, 'status': 'running',
                                'config': cfg, 'created_at': time.time()})
    monkeypatch.setattr('lib.tasks_pkg.spawn_task',
                        lambda task: spawned.append(task['id']))
    mq.dispatch_next_queued(conv_id)
    dispatcher_made_a_turn = bool(spawned)

    assert gate_sees_a_turn == dispatcher_made_a_turn, (
        f'the two queue readers DISAGREED about the same row '
        f'(yield gate saw a turn={gate_sees_a_turn}, dispatcher spawned='
        f'{dispatcher_made_a_turn}) — that disagreement IS the bug')


# ══════════════════════════════════════════════════════════════════
#  3 + 4 — who may preempt a working run (and the complement)
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
                       {'model': 'm'}, kind=kind)

    assert mq.has_pending_human_turn(conv_id) is False, (
        f'{kind} is machine work — it must not preempt a working autopilot run')
    nxt = mq.next_dispatchable_turn(conv_id)
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
                                 'timestamp': 1000}, {'model': 'm'})

    assert mq.has_pending_human_turn(conv_id) is True, (
        'a queued HUMAN message must always preempt autopilot')
    nxt = mq.next_dispatchable_turn(conv_id)
    assert nxt is not None and nxt['isHuman'] is True


def test_human_wins_even_when_queued_behind_machine_work(monkeypatch):
    """The answer must not depend on which row happens to sort first."""
    conv_id = _cid()
    mq.enqueue_message(conv_id, {'text': 'machine', 'timestamp': 1000},
                       {'model': 'm'}, kind=mq.KIND_WORKFLOW)
    mq.enqueue_message(conv_id, {'text': 'human', 'timestamp': 1001},
                       {'model': 'm'})

    assert mq.has_pending_human_turn(conv_id) is True, (
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
                        lambda cid: seen['cleared_run'].append(cid))
    monkeypatch.setattr('lib.message_queue.clear_autopilot_marker',
                        lambda cid: seen['disarmed'].append(cid))
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
    task = {'id': 'task-abc12345', 'convId': 'conv-1', 'config': {}}

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
    monkeypatch.setattr(ap, '_append_vu_message_to_conv',
                        lambda *a, **k: appended.append(a))

    ap._preserve_unsent_vu_and_conclude(
        {'id': 'task-abc12345', 'convId': 'conv-1', 'config': {}},
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
        {'id': 'task-abc12345', 'convId': 'conv-1', 'config': {}},
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
