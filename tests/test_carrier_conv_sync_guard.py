#!/usr/bin/env python3
"""Carrier tasks must never write into ``conversations.messages``.

THE BUG THIS GUARDS (live artifact, conv ``ms0z3wedmvs5l9`` ``msgs[19]``)
------------------------------------------------------------------------
A headless assistant bubble with **no beginning and no end** rendered in the
middle of a conversation: ``role='assistant'``, ``content='['`` (ONE char),
no user turn of its own, and a finish bar carrying only the model name.

Provenance: ``content='['`` is the first streaming delta of the autopilot
virtual-user's ``[VU: TASK_DONE]`` sentinel. ``task_results`` for that
``_taskId`` holds the authoritative ``'[VU: TASK_DONE]'`` under a
``_vu_subtask`` carrier. So the VU CARRIER wrote its own sentinel into the
conversation as a real assistant message.

Why it happened — GUARD ASYMMETRY between the two conv-sync paths:

  ``_sync_result_to_conversation``  (terminal)      → HAD a carrier guard
  ``_sync_partial_to_conversation`` (5s checkpoint) → had NONE

The VU sub-task (autopilot.py) simultaneously ① runs under the REAL
``convId``, ② sets ``_inline_messages``/``_vu_subtask``, and ③ records
ITSELF as the conversation's latest task (pt_8dc03017 HB-1). ③ means the
partial path's freshness guard waves it through; with no carrier guard it
finds no reusable assistant slot (the tail is the VU's own *user* row) and
``_new_assistant_slot`` APPENDS a brand-new one.

"No end" is the same hole's second half: that row can never receive a
terminal sync (the terminal path's guard correctly rejects it), so its
content stays frozen at the first delta forever while the partial path's P1a
block stamps a few finish-bar fields piecemeal — a bar that can never
complete.

THE CONTRACT UNDER TEST
-----------------------
``is_carrier_task`` (``_registry.py``) is already the SINGLE SOURCE OF TRUTH
for "carrier, not user-visible work" — ``/api/chat/active``, the restart
guard and the sidebar all consult it. BOTH conv-sync paths must consult the
SAME predicate, so they can never drift again.

These tests assert the CLASS invariant ("a carrier must not materialise a row
in conversations.messages"), NOT a string check — a future carrier flag added
to the predicate is covered automatically. Coverage spans the whole carrier
class, not just the VU:

  * ``_vu_subtask`` + ``_inline_messages``  — autopilot VU (the live bug)
  * ``_inline_messages`` only               — api_v1/chat, api_v1/agent_run,
                                              compat_openai, compat_anthropic,
                                              tasks_pkg.entry (in-process facade)
  * ``_vu_subtask`` only                    — the drift the unified predicate
                                              closes on the TERMINAL path

Every guard has a NEUTER negative control: bypass the predicate and the ghost
row must come back.
"""
import json
import os
import sys
import threading
import time
import uuid

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────
# Fixtures — a REAL conversations row + REAL sync functions (no mocks of the
# code under test; only the registry's latest-task index is pinned).
# ─────────────────────────────────────────────────────────────────────────
def _seed_conv(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'carrier-guard-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_messages(db, conv_id):
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    if not row:
        return None
    return json.loads(row[0]) if isinstance(row[0], str) else row[0]


def _cleanup(db, conv_id, task_id):
    from lib.database import db_execute_with_retry
    from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
    with _conv_latest_task_lock:
        _conv_latest_task.pop(conv_id, None)
    db_execute_with_retry(
        db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
    db.commit()


# The EXACT live shape: the tail is the VU's own *user* row, so a partial sync
# cannot reuse an assistant slot and must either skip or append.
_LIVE_SHAPE = [
    {'role': 'user', 'content': 'the real question', '_msgId': 'u1'},
    {'role': 'assistant', 'content': 'PARENT FINAL ANSWER', '_msgId': 'a1',
     '_taskId': 'parent-task'},
    {'role': 'user', 'content': 'VU: keep going', '_msgId': 'vu1',
     '_isVirtualUser': True},
]


def _mk_carrier(conv_id, **flags):
    """A carrier task registered as the conv's LATEST (the pt_8dc03017 HB-1
    ordering) — so the freshness guard cannot be what saves us."""
    from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
    task = {
        'id': 'carrier-' + uuid.uuid4().hex[:12],
        'convId': conv_id,
        'content': '[',            # first delta of '[VU: TASK_DONE]'
        'thinking': '',
        'toolRounds': [],
        'status': 'running',
        'model': 'yuju-claude-opus-5-evaDaily',
        'provider_id': 'sankuai',
        'content_lock': threading.Lock(),
        'events': [], 'events_lock': threading.Lock(),
    }
    task.update(flags)
    with _conv_latest_task_lock:
        _conv_latest_task[conv_id] = task['id']
    return task


# Every shape the carrier predicate must cover — the CLASS, not just the VU.
_CARRIER_SHAPES = [
    pytest.param({'_vu_subtask': True, '_inline_messages': True}, id='vu-subtask'),
    pytest.param({'_inline_messages': True}, id='inline-holder'),
    pytest.param({'_vu_subtask': True}, id='vu-flag-only'),
]


# ─────────────────────────────────────────────────────────────────────────
# 1. PARTIAL path — the hole that produced the live ghost.
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('flags', _CARRIER_SHAPES)
def test_carrier_partial_sync_materialises_no_row(flags):
    """★ THE INVARIANT. No carrier shape may add a row to conversations.messages."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'carrier-partial-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _LIVE_SHAPE)
    task = _mk_carrier(conv_id, **flags)
    try:
        mgr._sync_partial_to_conversation(task)
        msgs = _read_messages(db, conv_id)
        assert len(msgs) == len(_LIVE_SHAPE), (
            f'carrier {flags} appended a GHOST row: '
            f'{[(m.get("role"), (m.get("content") or "")[:20]) for m in msgs]} — '
            'a carrier runs no user-visible turn and must never materialise a '
            'message in the conversation')
        assert [m.get('role') for m in msgs] == ['user', 'assistant', 'user']
    finally:
        _cleanup(db, conv_id, task['id'])


def test_vu_carrier_ghost_has_neither_beginning_nor_end():
    """Regression pinned to the REPORTED SYMPTOM, not to the mechanism.

    Drives two checkpoints exactly as the live stream did: the first delta,
    then the VU's terminal verdict. The row the bug produced is recognisable
    by BOTH halves of the complaint — content frozen at the first delta ("no
    end"), sitting directly after a virtual-user row so it answers no real
    question ("no beginning"). Neither may exist.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'carrier-ghost-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _LIVE_SHAPE)
    task = _mk_carrier(conv_id, _vu_subtask=True, _inline_messages=True)
    try:
        mgr._sync_partial_to_conversation(task)          # ckpt 1: first delta
        task['content'] = '[VU: TASK_DONE]'
        task['finishReason'] = 'stop'
        task['usage'] = {'prompt_tokens': 2, 'completion_tokens': 14}
        mgr._sync_partial_to_conversation(task)          # ckpt 2: P1a stamping
        msgs = _read_messages(db, conv_id)

        assert len(msgs) == 3, f'ghost row survived: {len(msgs)} rows'
        # The VU's sentinel must not leak into the conversation in ANY form.
        for m in msgs:
            assert 'VU: TASK_DONE' not in (m.get('content') or ''), (
                'the VU stop-sentinel leaked into a conversation message')
        assert not any(
            m.get('role') == 'assistant'
            and (m.get('_taskId') or '') == task['id']
            for m in msgs), 'a row attributed to the carrier task exists'
    finally:
        _cleanup(db, conv_id, task['id'])


def test_carrier_partial_sync_cannot_overwrite_an_existing_assistant_row():
    """A carrier must not CORRUPT an existing row either.

    When the tail happens to be a real assistant row, the un-guarded partial
    path would reuse it and overwrite a settled answer with the carrier's own
    streaming text — silent data loss rather than a visible ghost.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'carrier-overwrite-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', '_msgId': 'u1'},
        {'role': 'assistant', 'content': 'THE SETTLED ANSWER', '_msgId': 'a1'},
    ])
    task = _mk_carrier(conv_id, _inline_messages=True)
    task['content'] = 'carrier scratch text'
    try:
        mgr._sync_partial_to_conversation(task)
        msgs = _read_messages(db, conv_id)
        assert msgs[-1]['content'] == 'THE SETTLED ANSWER', (
            f'carrier overwrote a settled assistant answer with '
            f'{msgs[-1]["content"]!r}')
    finally:
        _cleanup(db, conv_id, task['id'])


# ─────────────────────────────────────────────────────────────────────────
# 2. TERMINAL path — same predicate, closing the _vu_subtask drift.
# ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize('flags', _CARRIER_SHAPES)
def test_carrier_terminal_sync_materialises_no_row(flags):
    """The terminal path must reject the SAME carrier class as the partial one.

    Before unification it matched only ``_inline_messages``, so a
    ``_vu_subtask``-only carrier slipped through — a latent divergence between
    two hand-written string checks. One shared predicate removes it.
    """
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'carrier-terminal-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _LIVE_SHAPE)
    task = _mk_carrier(conv_id, **flags)
    task['content'] = '[VU: TASK_DONE]'
    task['finishReason'] = 'stop'
    try:
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        msgs = _read_messages(db, conv_id)
        assert len(msgs) == len(_LIVE_SHAPE), (
            f'carrier {flags} wrote a row on the TERMINAL path: '
            f'{[(m.get("role"), (m.get("content") or "")[:20]) for m in msgs]}')
        assert task.get('_committedMsg') is None, (
            'a skipped carrier must not stamp _committedMsg — the done event '
            'would ship a dict that was never committed')
    finally:
        _cleanup(db, conv_id, task['id'])


# ─────────────────────────────────────────────────────────────────────────
# 3. REVERSE assertions — the guard must not silence genuine work.
# ─────────────────────────────────────────────────────────────────────────
def test_real_task_partial_sync_still_writes():
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'carrier-real-partial-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', '_msgId': 'u1'},
        {'role': 'assistant', 'content': '', '_msgId': 'a1'},
    ])
    task = _mk_carrier(conv_id)          # NO carrier flags → real work
    task['content'] = 'R' * 300
    try:
        mgr._sync_partial_to_conversation(task)
        msgs = _read_messages(db, conv_id)
        assert msgs[-1]['content'] == 'R' * 300, (
            'the carrier guard suppressed a REAL task\'s checkpoint — it must '
            'only reject carriers')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_real_task_terminal_sync_still_writes():
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'carrier-real-terminal-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'q', '_msgId': 'u1'},
        {'role': 'assistant', 'content': '', '_msgId': 'a1'},
    ])
    task = _mk_carrier(conv_id)
    task['content'] = 'THE REAL FINAL ANSWER'
    task['finishReason'] = 'stop'
    try:
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        msgs = _read_messages(db, conv_id)
        assert msgs[-1]['content'] == 'THE REAL FINAL ANSWER'
        assert task.get('_committedMsg') is not None, (
            'a real task must still stamp _committedMsg for the done event')
    finally:
        _cleanup(db, conv_id, task['id'])


# ─────────────────────────────────────────────────────────────────────────
# 4. NEUTER negative controls — bypass the predicate, the ghost returns.
# ─────────────────────────────────────────────────────────────────────────
def test_neuter_partial_guard_reproduces_the_ghost(monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.manager._sync as _sync

    conv_id = 'carrier-neuter-p-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _LIVE_SHAPE)
    task = _mk_carrier(conv_id, _vu_subtask=True, _inline_messages=True)
    # NEUTER: the predicate always says "not a carrier".
    monkeypatch.setattr(_sync, 'is_carrier_task', lambda t: False, raising=True)
    try:
        mgr._sync_partial_to_conversation(task)
        msgs = _read_messages(db, conv_id)
        assert len(msgs) == len(_LIVE_SHAPE) + 1, (
            'with the guard neutered the ghost row must reappear — otherwise '
            'this suite is not actually exercising the guard')
        ghost = msgs[-1]
        assert ghost['role'] == 'assistant' and ghost['content'] == '[', (
            f'neutered shape should match the live artifact, got {ghost!r}')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_neuter_terminal_guard_reproduces_the_write(monkeypatch):
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr
    import lib.tasks_pkg.manager._sync as _sync

    conv_id = 'carrier-neuter-t-' + uuid.uuid4().hex[:8]
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, _LIVE_SHAPE)
    task = _mk_carrier(conv_id, _vu_subtask=True, _inline_messages=True)
    task['content'] = '[VU: TASK_DONE]'
    task['finishReason'] = 'stop'
    monkeypatch.setattr(_sync, 'is_carrier_task', lambda t: False, raising=True)
    try:
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        msgs = _read_messages(db, conv_id)
        assert len(msgs) == len(_LIVE_SHAPE) + 1, (
            'with the guard neutered the terminal path must write the carrier '
            'row — proving the guard is load-bearing there too')
    finally:
        _cleanup(db, conv_id, task['id'])


# ─────────────────────────────────────────────────────────────────────────
# 5. SSOT static check — both paths must consult the SAME predicate, so a
#    future edit cannot reintroduce two hand-written string checks.
# ─────────────────────────────────────────────────────────────────────────
def test_both_conv_sync_paths_use_the_shared_predicate():
    import ast
    import inspect
    import lib.tasks_pkg.manager._sync as _sync

    src = inspect.getsource(_sync)
    tree = ast.parse(src)
    for fn in ('_sync_result_to_conversation', '_sync_partial_to_conversation'):
        node = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == fn)
        body = ast.get_source_segment(src, node)
        assert 'is_carrier_task' in body, (
            f'{fn} does not consult is_carrier_task — the two conv-sync paths '
            'must share ONE carrier predicate (lib/tasks_pkg/manager/_registry.py) '
            'or they will drift apart again, which is exactly how the VU '
            'sentinel got written into a conversation as a real message')


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
