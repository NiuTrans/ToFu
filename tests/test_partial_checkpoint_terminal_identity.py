#!/usr/bin/env python3
"""Regression: the PARTIAL checkpoint must never persist a terminal verdict
WITHOUT its identity anchor — the third (and most durable) source of the
duplicate-agent-bubble bug.

WHY
---
Two earlier batches closed the two SNAPSHOT transports (``/api/v1/chat/poll``
and the SSE ``state`` event) via ``lib/chat/terminal_gate.py``: a snapshot
reporting a non-terminal status may not advertise ``finishReason`` / ``usage`` /
``preset``. That gate governs what goes ON THE WIRE.

``_sync_partial_to_conversation`` is NOT a snapshot transport — it WRITES TO THE
DATABASE (``conversations.messages``). Its P1a block carries the finish verdict
onto the trailing assistant message as soon as ``task['finishReason']`` exists:

    if task.get('finishReason'):
        last_msg['finishReason'] = task['finishReason']

``lib/tasks_pkg/orchestrator/_finalize.py`` stamps ``task['finishReason']`` at
L843 but only flips ``task['status'] = 'done'`` at L954 — a **110-line window**
containing the BLOCKING ``_generate_tool_summary`` LLM call. The 5-second
checkpoint timer fires inside that window routinely, so the row lands with
``finishReason`` set while the turn is still generating.

THE COMPOUNDING DEFECT: that same path never wrote ``_taskId``. Measured on the
shipped source — only ``_sync_result_to_conversation`` (the TERMINAL sync)
stamps it. So the persisted row is::

    {"finishReason": "stop", "_taskId": <absent>}

and the frontend reducer ``assistantTailIsPriorTurn`` returns **True** for that
row even when asked about its OWN task: the identity arm cannot fire without
``_taskId``, and the reload-safe ``!!finishReason`` arm does. ``connectToTask``
then pushes a fresh placeholder, deltas move to it, the original bubble freezes
mid-sentence and BOTH render — and because this one is PERSISTED, a page reload
reproduces it from the database rather than merely from live state.

THE FIX (two parts, both asserted here)
---------------------------------------
1. **Reuse the shared gate.** The trigger becomes
   ``is_terminal_status(task['status']) or task['_finalize_started_at']``
   instead of a third hand-written timing assumption. The latch is stamped at
   L953 — AFTER the 110-line window and one line before the terminal flip — so
   it precisely admits the "finalize is genuinely underway" case the P1a block
   was written for, while excluding the window that mints the contradiction.
   (P1a's intent is preserved: a checkpoint that outlives a failed terminal
   persist still leaves a populated finish bar.)
2. **Atomicity.** When the verdict IS carried, ``_taskId`` is written in the
   same block. A terminal field without its identity anchor is precisely an
   "unrecognisable-as-its-own completed turn" row.

Run directly (DB-backed; see the project note about schema warm-up):
    python tests/test_partial_checkpoint_terminal_identity.py
"""

import json as _json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _seed_conv(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'partial-terminal-identity',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms, 'search_text': '',
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at', 'search_text'], retry=True)
    db.commit()


def _read_tail(db, conv_id):
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    msgs = _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return msgs[-1] if msgs else {}


def _cleanup(db, *conv_ids):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(
            db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    db.commit()


def _run_partial(conv_id, task_fields):
    """Seed a conv with a live assistant tail, run the REAL partial sync."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    from lib.tasks_pkg import manager as _mgr

    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'question', 'timestamp': 1,
         '_msgId': 'u-1'},
        {'role': 'assistant', 'content': '', 'thinking': '',
         'toolRounds': [], 'timestamp': 2, '_msgId': 'a-1'},
    ])
    task = {
        'id': 'tk-partial-identity', 'convId': conv_id,
        'content': 'partial answer so far', 'thinking': '',
        'toolRounds': [], 'model': 'claude-opus-5', 'status': 'running',
    }
    task.update(task_fields)
    _mgr._sync_partial_to_conversation(task)
    return _read_tail(db, conv_id), db


def test_midwindow_verdict_is_not_persisted_at_all():
    """THE DEFECT, asserted on the TIMING gate specifically.

    `finishReason` set, `status` still 'running', no finalize latch — i.e. a
    checkpoint landing inside the 110-line finalize window. The verdict must be
    WITHHELD outright, not merely accompanied by an anchor.

    WHY THIS IS STRICTER THAN "verdict ⇒ anchor" (and why the weaker form was
    not enough): a NEUTER that reverts the trigger to the original
    presence-only `if task.get('finishReason'):` still writes `_taskId`
    alongside, so a "verdict implies anchor" assertion stays GREEN and the
    timing gate looks decorative. Measured — that is exactly what happened on
    the first draft of this suite. The two halves of the fix must each be
    provable on their own, so this test pins the gate and
    `test_..._carried_with_identity` pins the atomicity.

    Withholding is also the semantically correct outcome here: during that
    window the turn genuinely has NOT settled, so any terminal field is a lie
    about live state — an anchored lie is still a lie, and it would still paint
    a settled finish bar via finish_info.js (`msg.finishReason || msg.usage`),
    which never consults the identity reducer at all.
    """
    conv_id = 'cv-partial-midwindow'
    tail, db = _run_partial(conv_id, {
        'status': 'running',          # terminal flip has NOT happened
        'finishReason': 'stop',       # ...but the verdict is already stamped
        'usage': {'input_tokens': 11},
        # '_finalize_started_at' deliberately ABSENT — it is stamped at L953,
        # i.e. AFTER this window.
    })
    try:
        assert not tail.get('finishReason'), (
            'the partial checkpoint PERSISTED finishReason=%r while the turn '
            'was still generating (status=running, finalize not started). The '
            'frontend reads a terminal field as "this turn settled": '
            'finish_info.js paints a settled finish bar on a live turn, and '
            'assistantTailIsPriorTurn mints a duplicate assistant bubble — '
            'durably, since this row is in the database.'
            % tail.get('finishReason'))
        assert not tail.get('usage'), (
            f'`usage` is a terminal signal too and must be withheld: {tail}')
        # Live progress must still be checkpointed — the whole point of the path.
        assert tail.get('content') == 'partial answer so far', (
            f'the partial checkpoint stopped persisting live content: {tail}')
    finally:
        _cleanup(db, conv_id)


def test_finalize_window_verdict_is_carried_with_identity():
    """COMPLEMENT (P1a's real purpose must survive): once finalize is genuinely
    underway — the `_finalize_started_at` latch is stamped, one line before the
    terminal flip — a checkpoint MUST still carry the verdict, so a failed
    terminal persist does not leave an empty finish bar. And it must carry
    `_taskId` with it."""
    conv_id = 'cv-partial-latched'
    tail, db = _run_partial(conv_id, {
        'status': 'running',
        'finishReason': 'stop',
        'usage': {'input_tokens': 11},
        '_finalize_started_at': time.time(),
    })
    try:
        assert tail.get('finishReason') == 'stop', (
            'P1a regressed: a checkpoint during a genuine finalize must still '
            f'carry the verdict (that is why the block exists): {tail}')
        assert tail.get('_taskId') == 'tk-partial-identity', (
            f'the verdict was carried WITHOUT its identity anchor: {tail}')
    finally:
        _cleanup(db, conv_id)


def test_terminal_status_verdict_is_carried_with_identity():
    """COMPLEMENT: a genuinely terminal task checkpointing late must carry both."""
    conv_id = 'cv-partial-terminal'
    tail, db = _run_partial(conv_id, {
        'status': 'done',
        'finishReason': 'stop',
        'usage': {'input_tokens': 11},
    })
    try:
        assert tail.get('finishReason') == 'stop', tail
        assert tail.get('_taskId') == 'tk-partial-identity', (
            f'terminal checkpoint wrote the verdict without _taskId: {tail}')
    finally:
        _cleanup(db, conv_id)


def test_plain_midstream_checkpoint_is_unchanged():
    """No verdict at all (the ordinary mid-stream case) → byte-identical to the
    pre-fix behaviour: content persists, no terminal fields appear."""
    conv_id = 'cv-partial-plain'
    tail, db = _run_partial(conv_id, {'status': 'running'})
    try:
        assert tail.get('content') == 'partial answer so far', tail
        assert not tail.get('finishReason'), (
            f'a checkpoint with no verdict must not invent one: {tail}')
    finally:
        _cleanup(db, conv_id)


def test_partial_sync_routes_through_the_shared_gate():
    """The timing rule must come from lib/chat/terminal_gate.py, not a third
    hand-written copy. Two transports already consume it; this is the third."""
    src = open(os.path.join(REPO, 'lib', 'tasks_pkg', 'manager', '_sync.py'),
               encoding='utf-8').read()
    assert 'terminal_gate' in src, (
        '_sync.py does not import the shared terminal gate — the P1a trigger '
        'is a third independent copy of the "is this turn really over?" '
        'timing assumption, which is exactly how the four metadata paths '
        'drifted before (see extract_task_meta docstring).')


def test_partial_docstring_matches_behaviour():
    """The docstring claimed terminal fields are 'withheld while mid-stream'
    while the code carried them on presence alone — the same false-comment
    class as finish_info.js. It must name the real condition."""
    from lib.tasks_pkg.manager import _sync as _sync_mod
    doc = _sync_mod._sync_partial_to_conversation.__doc__ or ''
    assert '_finalize_started_at' in doc or 'terminal_gate' in doc, (
        'the docstring still describes a withholding rule it does not '
        'implement; it must name the actual gate condition.')


def test_reconcile_interrupt_branch_is_never_reached_on_a_live_turn():
    """The 4th CANDIDATE mint site — adjudicated NOT a defect; premise pinned here.

    ``lib/conversations/reconcile.py`` has the same *shape* as the three real
    mint sites: ``classify_ghost_tail() == 'interrupt'`` stamps
    ``tail['finishReason'] = 'interrupted'`` and writes NO ``_taskId``.

    MEASURED: the PURE predicate DOES fire on a live turn's shape — a
    thinking-phase tail (empty content + streaming thinking + no settled round)
    classifies as 'interrupt', and ``reconcile_conversation_messages`` stamps it
    (see the companion test below). So the predicate ALONE is not the safety.

    What makes it safe is that all THREE callers sit structurally off the live
    path, verified individually:

      1. ``routes/conversations.py`` (GET / warm-open) — the prefetch helper
         returns before reconciliation when ``_conv_has_live_task`` is true;
         the main GET snapshots that same predicate in ``_is_live`` and passes
         ``allow_reconcile=not _is_live`` into the projected-row reader.  The
         reason is written down in-source: "a pending/running task's empty
         placeholder is indistinguishable from a ghost tail".
      2. ``lib/tasks_pkg/manager/_sync.py`` — called ONLY from
         ``_sync_result_to_conversation`` (the TERMINAL sync), inside its
         "nothing to persist" branch and behind a latest-task gate. At that
         point the task is settled by definition, so its own tail cannot still
         be streaming.
      3. ``lib/tasks_pkg/manager/_recovery.py`` — inside
         ``recover_stale_tasks_on_startup``; the turns it touches are dead by
         construction (their process is gone).

    Hence NO identity anchor is required here: the tail it stamps is never a
    live turn's own bubble, so ``assistantTailIsPriorTurn`` is never asked about
    it while that task is connecting. It is also semantically CORRECT —
    ``interrupted`` is the true terminal state of a turn whose process died.

    THIS TEST IS THE TRIPWIRE. The safety is caller-side, not predicate-side, so
    it is exactly the property a refactor breaks silently: moving the reconcile
    onto a hot path (a mid-stream self-heal, a checkpoint-time sweep) would
    resurrect the duplicate-bubble bug in its most durable form. Asserting the
    gates exist makes that move fail loudly instead.
    """
    conv_src = open(os.path.join(REPO, 'routes', 'conversations.py'),
                    encoding='utf-8').read()
    # Gate 1: both GET entry points must refuse to reconcile a live conv.  The
    # prefetch helper returns explicitly; the main GET threads one predicate
    # snapshot into the projected-row reader so it neither races nor evaluates
    # the task registry twice.
    assert 'if _conv_has_live_task(conv_id):' in conv_src, (
        'the prefetch live-task return around reconcile is gone.')
    assert '_is_live = _conv_has_live_task(conv_id)' in conv_src, (
        'the main GET no longer snapshots the live-task predicate.')
    assert 'allow_reconcile=not _is_live' in conv_src, (
        'the main GET projected-row reader can reconcile a live turn — a '
        'mid-stream reconcile can stamp finishReason=interrupted onto the '
        "LIVE turn's own tail (no _taskId), which reads back as a prior turn "
        'and mints a duplicate assistant bubble.')

    sync_src = open(os.path.join(REPO, 'lib', 'tasks_pkg', 'manager', '_sync.py'),
                    encoding='utf-8').read()
    # Gate 2: the settle-time reconcile must stay inside the TERMINAL sync.
    assert '_reconcile_orphan_placeholder_on_settle' in sync_src, (
        'the settle-time reconcile helper vanished — re-derive this premise.')
    _term_fn = sync_src.split('def _sync_result_to_conversation(')[-1]
    _partial_fn = sync_src.split('def _sync_partial_to_conversation(')[-1]
    assert '_reconcile_orphan_placeholder_on_settle(task)' in _term_fn, (
        'the settle-time reconcile is no longer called from the TERMINAL sync.')
    assert '_reconcile_orphan_placeholder_on_settle(task)' not in _partial_fn, (
        'the ghost reconcile is now called from the PARTIAL (mid-stream) '
        'checkpoint — that puts the un-anchored finishReason=interrupted stamp '
        'ON THE LIVE PATH. It must then be gated through '
        'lib/chat/terminal_gate.py and write _taskId atomically, exactly like '
        'the P1a block.')

    rec_src = open(os.path.join(REPO, 'lib', 'tasks_pkg', 'manager', '_recovery.py'),
                   encoding='utf-8').read()
    # Gate 3: the startup reconcile must stay in the startup recovery function.
    _startup_fn = rec_src.split('def recover_stale_tasks_on_startup(')[-1]
    assert 'reconcile_conversation_messages' in _startup_fn, (
        'the startup ghost reconcile moved out of recover_stale_tasks_on_startup '
        '— if it now runs anywhere a task can still be streaming, the '
        'un-anchored interrupted stamp is back on the live path.')


def test_reconcile_interrupt_predicate_does_fire_on_a_live_shape():
    """The other half of the premise, stated explicitly so nobody "simplifies"
    the caller gates away believing the predicate is self-guarding.

    It is NOT: a live thinking-phase tail classifies as 'interrupt'. Recording
    that measured fact here is what makes the gates above visibly load-bearing.
    """
    from lib.conversations.reconcile import classify_ghost_tail
    live_thinking_tail = {
        'role': 'assistant', 'content': '',
        'thinking': 'Let me analyse the logs...', 'toolRounds': [],
    }
    assert classify_ghost_tail(live_thinking_tail) == 'interrupt', (
        'classify_ghost_tail no longer fires on a live thinking-phase tail. If '
        'the predicate became live-aware on its own, the caller-side gates are '
        'now belt-and-braces rather than load-bearing — update the premise in '
        'test_reconcile_interrupt_branch_is_never_reached_on_a_live_turn.')


if __name__ == '__main__':
    from tests._standalone_guard import guard_standalone_db
    guard_standalone_db('test_partial_checkpoint_terminal_identity.__main__',
                        init_schema=True)
    for fn in (test_midwindow_verdict_is_not_persisted_at_all,
               test_finalize_window_verdict_is_carried_with_identity,
               test_terminal_status_verdict_is_carried_with_identity,
               test_plain_midstream_checkpoint_is_unchanged,
               test_partial_sync_routes_through_the_shared_gate,
               test_partial_docstring_matches_behaviour,
               test_reconcile_interrupt_branch_is_never_reached_on_a_live_turn,
               test_reconcile_interrupt_predicate_does_fire_on_a_live_shape):
        fn()
        print(f'  ✓ {fn.__name__}')
    print('all green')
