#!/usr/bin/env python3
"""Checkpoint delta-coalescing — reload-equivalence proof.

``_sync_partial_to_conversation`` rewrites the WHOLE ``conversations.messages``
JSON blob on every partial checkpoint (O(conv-size)). ``CHECKPOINT_MIN_DELTA_CHARS``
coalesces a sub-threshold content/thinking delta: the expensive messages write
is WITHHELD until cumulative growth crosses the threshold, while the cheap
per-task ``task_results`` blob is still written every checkpoint and the
terminal sync always writes the full final content.

The correctness claim under test (the one the user demanded be PROVEN, not
asserted): for the skipped-delta case, a conversation reconstructed from the
authoritative store (``task_results`` — the live-tail source the reconnect /
poll-fallback path actually reads) is byte-identical to what a per-delta write
would have produced, and the ``conversations.messages`` mirror always CONVERGES
(it lags by < threshold mid-stream, never permanently).

These tests drive the REAL ``_sync_partial_to_conversation`` /
``checkpoint_task_partial`` against a REAL sqlite conversation row, then RELOAD
and DIFF — they do not merely assert that a write was skipped.
"""
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


def _seed_conv(db, conv_id, messages):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, CONVERSATIONS, {
        'id': conv_id, 'user_id': 1, 'title': 'ckpt-coalesce-test',
        'messages': json_dumps_pg(messages), 'msg_count': len(messages),
        'created_at': now_ms, 'updated_at': now_ms,
    }, insert_cols=['id', 'user_id', 'title', 'messages', 'msg_count',
                    'created_at', 'updated_at'], retry=True)
    db.commit()


def _read_messages(db, conv_id):
    row = db.execute(
        'SELECT messages FROM conversations WHERE id=? AND user_id=1',
        (conv_id,)).fetchone()
    return json.loads(row[0] or '[]') if row else None


def _read_task_results(db, task_id):
    row = db.execute(
        'SELECT content, thinking, status FROM task_results WHERE task_id=?',
        (task_id,)).fetchone()
    return row


def _mk_task(conv_id, content='', thinking=''):
    from lib.tasks_pkg.manager import (
        create_task, _conv_latest_task, _conv_latest_task_lock)
    task = create_task(conv_id, [{'role': 'user', 'content': 'U1'}], {})
    task['content'] = content
    task['thinking'] = thinking
    with _conv_latest_task_lock:
        _conv_latest_task[conv_id] = task['id']
    return task


def _set_min_delta(n):
    """Set CHECKPOINT_MIN_DELTA_CHARS where the code that READS it lives.
    ``manager.py`` became a package: ``_sync.py`` imported the constant by
    value (``from ._state import CHECKPOINT_MIN_DELTA_CHARS``), so assigning
    only ``manager.CHECKPOINT_MIN_DELTA_CHARS`` (the facade copy) never changes
    what ``_sync_partial_to_conversation`` sees. Set it on the read site
    (``_sync``) plus the facade + ``_state`` so every binding agrees. Returns
    the prior ``_sync`` value for restore."""
    import lib.tasks_pkg.manager as _mgr
    import lib.tasks_pkg.manager._sync as _sync
    import lib.tasks_pkg.manager._state as _st
    prev = _sync.CHECKPOINT_MIN_DELTA_CHARS
    _sync.CHECKPOINT_MIN_DELTA_CHARS = n
    _mgr.CHECKPOINT_MIN_DELTA_CHARS = n
    _st.CHECKPOINT_MIN_DELTA_CHARS = n
    return prev


def _set_blob_cap(n):
    """Set the large-conversation checkpoint cap at every imported binding."""
    import lib.tasks_pkg.manager as _mgr
    import lib.tasks_pkg.manager._sync as _sync
    import lib.tasks_pkg.manager._state as _st
    prev = _sync.CHECKPOINT_CONV_BLOB_MAX_BYTES
    _sync.CHECKPOINT_CONV_BLOB_MAX_BYTES = n
    _mgr.CHECKPOINT_CONV_BLOB_MAX_BYTES = n
    _st.CHECKPOINT_CONV_BLOB_MAX_BYTES = n
    return prev


def _cleanup(db, conv_id, task_id):
    from lib.database import db_execute_with_retry
    from lib.tasks_pkg.manager import _conv_latest_task, _conv_latest_task_lock
    with _conv_latest_task_lock:
        _conv_latest_task.pop(conv_id, None)
    db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (conv_id,))
    db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (task_id,))
    db.commit()


def test_subthreshold_delta_withholds_messages_write_but_task_results_current():
    """A sub-threshold content delta is NOT written to conversations.messages,
    but the authoritative task_results blob IS current."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-sub'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    try:
        # First checkpoint: a big enough chunk to establish a baseline write.
        task['content'] = 'A' * 200
        mgr.checkpoint_task_partial(task)
        base_msgs = _read_messages(db, conv_id)
        assert base_msgs[-1]['content'] == 'A' * 200, 'baseline write should land'

        # Now a SMALL delta (< default 160): messages write must be WITHHELD.
        task['content'] = 'A' * 200 + 'B' * 10
        mgr.checkpoint_task_partial(task)

        msgs_after = _read_messages(db, conv_id)
        assert msgs_after[-1]['content'] == 'A' * 200, (
            'sub-threshold delta must NOT rewrite conversations.messages '
            f'(got {len(msgs_after[-1]["content"])} chars)')

        # But task_results (the authoritative live-tail store) IS current.
        tr = _read_task_results(db, task['id'])
        assert tr is not None and tr[0] == 'A' * 200 + 'B' * 10, (
            'task_results must carry the full current content every checkpoint')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_cumulative_growth_crosses_threshold_then_flushes():
    """Several sub-threshold deltas accumulate (measured against the unwritten
    row) and the messages write flushes once cumulative growth crosses N."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-cumulative'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    try:
        # Baseline write.
        task['content'] = 'X' * 100
        mgr.checkpoint_task_partial(task)
        assert _read_messages(db, conv_id)[-1]['content'] == 'X' * 100

        # Three 50-char drips. Each individual drip < 160, but they accumulate
        # against the UNWRITTEN row: 50, 100, 150 (all < 160 → withheld), then
        # a 4th makes 200 >= 160 → flush.
        for i, n in enumerate((50, 100, 150), start=1):
            task['content'] = 'X' * 100 + 'Y' * n
            mgr.checkpoint_task_partial(task)
            got = _read_messages(db, conv_id)[-1]['content']
            assert got == 'X' * 100, (
                f'drip {i} (cum {n} < 160) should still be withheld, got {len(got)}')

        task['content'] = 'X' * 100 + 'Y' * 200
        mgr.checkpoint_task_partial(task)
        got = _read_messages(db, conv_id)[-1]['content']
        assert got == 'X' * 100 + 'Y' * 200, (
            f'cumulative growth 200 >= 160 must flush, got {len(got)}')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_reload_equivalence_task_results_vs_per_delta_messages():
    """★ THE INVARIANT. Reconstruct a conversation from the authoritative
    task_results store for the coalesced-delta case, and prove it is
    byte-identical to a conversation built with per-delta messages writes
    (coalescing disabled). This is the reload-equivalence the design promises:
    the live-tail source used by the reconnect/poll path is unaffected by
    coalescing."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    db = get_thread_db(DOMAIN_CHAT)
    deltas = ['Hello ', 'world', ', this ', 'is ', 'a ', 'streamed ', 'answer.']

    def _run(conv_id, min_delta):
        _seed_conv(db, conv_id, [
            {'role': 'user', 'content': 'U1'},
            {'role': 'assistant', 'content': ''},
        ])
        task = _mk_task(conv_id)
        acc = ''
        orig = _set_min_delta(min_delta)
        try:
            for d in deltas:
                acc += d
                task['content'] = acc
                mgr.checkpoint_task_partial(task)
            tr = _read_task_results(db, task['id'])
            return tr[0], task['id']
        finally:
            _set_min_delta(orig)

    # Coalesced (default-ish large threshold → most deltas withheld from
    # messages) vs per-delta (threshold 0 → every delta written).
    tr_coalesced, tid1 = _run('ckpt-eq-coalesced', 160)
    tr_perdelta, tid2 = _run('ckpt-eq-perdelta', 0)
    try:
        full = ''.join(deltas)
        # The authoritative task_results content is byte-identical either way:
        # coalescing NEVER touches the task_results write.
        assert tr_coalesced == full, 'coalesced task_results lost content'
        assert tr_perdelta == full, 'per-delta task_results lost content'
        assert tr_coalesced == tr_perdelta, (
            'task_results reconstruction diverged between coalesced and '
            'per-delta — the reconnect/poll reload path would differ')
    finally:
        _cleanup(db, 'ckpt-eq-coalesced', tid1)
        _cleanup(db, 'ckpt-eq-perdelta', tid2)


def test_terminal_sync_converges_messages_to_full_content():
    """After coalescing withholds tail chars mid-stream, the terminal
    _sync_result_to_conversation writes the FULL final content to
    conversations.messages — so the mirror always converges, never lags
    permanently."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-converge'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    try:
        task['content'] = 'Z' * 100
        mgr.checkpoint_task_partial(task)
        # A small trailing delta that gets withheld from messages.
        task['content'] = 'Z' * 100 + 'tail'
        mgr.checkpoint_task_partial(task)
        assert _read_messages(db, conv_id)[-1]['content'] == 'Z' * 100, 'withheld'

        # Terminal sync writes the full content.
        task['finishReason'] = 'stop'
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        final = _read_messages(db, conv_id)[-1]['content']
        assert final == 'Z' * 100 + 'tail', (
            f'terminal sync must converge messages to full content, got {len(final)}')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_large_conversation_uses_task_owned_checkpoint_then_terminal_converges():
    """A huge history must not be read/re-written on every streaming tick."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-large-lean'
    db = get_thread_db(DOMAIN_CHAT)
    original = [
        {'role': 'user', 'content': 'U1', 'padding': 'p' * 20_000},
        {'role': 'assistant', 'content': ''},
    ]
    _seed_conv(db, conv_id, original)
    task = _mk_task(conv_id)
    prior_cap = _set_blob_cap(1024)
    try:
        task['content'] = 'durable partial' * 20
        mgr.checkpoint_task_partial(task)

        # Conversation mirror stays byte-equivalent: no huge blob rewrite.
        assert _read_messages(db, conv_id) == original
        tr = _read_task_results(db, task['id'])
        assert tr is not None and tr[0] == task['content']
        assert task.get('_largeConvCheckpointDeferredLogged') is True

        # A real terminal sync still performs the full convergence write.
        task['status'] = 'done'
        task['finishReason'] = 'stop'
        mgr._sync_result_to_conversation(task, mgr.build_result_meta(task))
        final = _read_messages(db, conv_id)
        assert final[-1]['content'] == task['content']
        assert final[-1]['finishReason'] == 'stop'
    finally:
        _set_blob_cap(prior_cap)
        _cleanup(db, conv_id, task['id'])


def test_small_conversation_keeps_direct_reload_checkpoint():
    """Below the cap, preserve the existing direct conversation mirror."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-small-direct'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    prior_cap = _set_blob_cap(1_000_000)
    try:
        task['content'] = 'S' * 200
        mgr.checkpoint_task_partial(task)
        assert _read_messages(db, conv_id)[-1]['content'] == 'S' * 200
        assert not task.get('_largeConvCheckpointDeferredLogged')
    finally:
        _set_blob_cap(prior_cap)
        _cleanup(db, conv_id, task['id'])


def test_terminal_delta_is_never_withheld():
    """A terminal task (finishReason set) must write even a tiny delta —
    coalescing only applies to in-flight checkpoints."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-terminal'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    try:
        task['content'] = 'Q' * 100
        mgr.checkpoint_task_partial(task)
        # Tiny delta, but the task is terminal → _sync_partial must write it.
        task['content'] = 'Q' * 100 + 'x'
        task['finishReason'] = 'stop'
        mgr._sync_partial_to_conversation(task)
        got = _read_messages(db, conv_id)[-1]['content']
        assert got == 'Q' * 100 + 'x', (
            f'terminal task must not withhold its delta, got {len(got)}')
    finally:
        _cleanup(db, conv_id, task['id'])


def test_disabled_threshold_writes_every_delta():
    """CHECKPOINT_MIN_DELTA_CHARS=0 restores legacy per-delta behaviour."""
    from lib.database import DOMAIN_CHAT, get_thread_db
    import lib.tasks_pkg.manager as mgr

    conv_id = 'ckpt-disabled'
    db = get_thread_db(DOMAIN_CHAT)
    _seed_conv(db, conv_id, [
        {'role': 'user', 'content': 'U1'},
        {'role': 'assistant', 'content': ''},
    ])
    task = _mk_task(conv_id)
    orig = _set_min_delta(0)
    try:
        task['content'] = 'M' * 100
        mgr.checkpoint_task_partial(task)
        task['content'] = 'M' * 100 + 'z'  # 1-char delta
        mgr.checkpoint_task_partial(task)
        got = _read_messages(db, conv_id)[-1]['content']
        assert got == 'M' * 100 + 'z', (
            f'threshold 0 must write every delta, got {len(got)}')
    finally:
        _set_min_delta(orig)
        _cleanup(db, conv_id, task['id'])


if __name__ == '__main__':
    raise SystemExit(pytest.main([__file__, '-v', '-p', 'no:napari']))
