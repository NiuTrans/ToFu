"""tests/test_migrate_reaper_terminal_cleanup.py — the one-shot history cleanup
(pt_50c0ee26faac44fc) against a synthetic library.

Covers the four message classes end-to-end through ``run(db, apply=True)``:

  CLASS P   fr='stop' + reaper error  → error REMOVED (foreign death badge on
            a completed answer), finishReason untouched; mirror meta cleaned;
            tail sidecar (settings.lastMsgError) recomputed.
  CLASS R   fr='aborted' + reaper error → finishReason restamped 'error';
            error kept; mirror + owning task_results.metadata restamped;
            tail sidecar lastFinishReason restamped.
  CLASS OK  fr='error' + reaper error → byte-untouched.
  UNKNOWN   fr=None + reaper error → reported in the plan's unknown list,
            byte-untouched.

Plus the two migration-script invariants: DRY-RUN writes nothing, and a
second APPLY run is a no-op (idempotent).
"""

import json as _json
import threading
import time

import pytest

pytestmark = pytest.mark.unit

import tests._migrate_reaper_terminal_cleanup as mig


def _env(detail='Task made no progress for 1812 seconds and was terminated as wedged.'):
    from lib.error_envelope import make_envelope
    return make_envelope('worker_lost', detail=detail, model='m',
                         context='stuck-task-reaper', source='lib.tasks_pkg.manager')


def _seed(db, conv_id, messages, settings=None, mirror=True):
    from lib.database import json_dumps_pg
    from lib.database._core_schema import CONVERSATIONS, upsert
    now_ms = int(time.time() * 1000)
    row = {'id': conv_id, 'user_id': 1, 'title': 'mig-test',
           'messages': json_dumps_pg(messages), 'msg_count': len(messages),
           'created_at': now_ms, 'updated_at': now_ms}
    cols = ['id', 'user_id', 'title', 'messages', 'msg_count',
            'created_at', 'updated_at']
    if settings is not None:
        row['settings'] = _json.dumps(settings)
        cols.append('settings')
    upsert(db, CONVERSATIONS, row, insert_cols=cols, retry=True)
    if mirror:
        for i, m in enumerate(messages):
            meta = {k: v for k, v in m.items()
                    if k not in ('role', 'thinking')}
            db.execute(
                'INSERT INTO conversation_messages '
                '(conv_id, seq, msg_id, role, content, content_json, thinking, '
                ' translated_content, meta, created_at, updated_at) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (conv_id, str(i), m.get('_msgId') or f'mid-{i}', m.get('role'),
                 m.get('content') or '', '[]', '', '',
                 _json.dumps(meta, ensure_ascii=False), now_ms, now_ms))
    db.commit()


def _cleanup(db, conv_ids, task_ids=()):
    from lib.database import db_execute_with_retry
    for cid in conv_ids:
        db_execute_with_retry(db, 'DELETE FROM conversation_messages WHERE conv_id=?', (cid,))
        db_execute_with_retry(db, 'DELETE FROM conversations WHERE id=? AND user_id=1', (cid,))
    for tid in task_ids:
        db_execute_with_retry(db, 'DELETE FROM task_results WHERE task_id=?', (tid,))
    db.commit()


def _read_msgs(db, conv_id):
    row = db.execute('SELECT CAST(messages AS TEXT) FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    return _json.loads(row[0])


def _read_settings(db, conv_id):
    row = db.execute('SELECT CAST(settings AS TEXT) FROM conversations '
                     'WHERE id=? AND user_id=1', (conv_id,)).fetchone()
    return _json.loads(row[0] or '{}')


def _read_mirror_meta(db, conv_id, idx):
    row = db.execute('SELECT meta FROM conversation_messages '
                     'WHERE conv_id=? AND seq=?', (conv_id, str(idx))).fetchone()
    return _json.loads(row[0]) if row and row[0] else None


def test_full_cleanup_end_to_end_and_idempotent():
    from lib.database import DOMAIN_CHAT, get_thread_db
    db = get_thread_db(DOMAIN_CHAT)

    conv_p, conv_r, conv_ok, conv_u = ('cv-mig-p', 'cv-mig-r', 'cv-mig-ok', 'cv-mig-u')
    tid_r = 't-mig-r-task'
    # CLASS P — completed answer wearing a foreign reaper error (the tail).
    _seed(db, conv_p, [
        {'role': 'user', 'content': 'q', '_msgId': 'p-u'},
        {'role': 'assistant', 'content': 'the clean answer', '_msgId': 'p-a',
         'finishReason': 'stop', '_taskId': 't-mig-clean',
         'error': _env()},
    ], settings={'lastFinishReason': 'stop', 'lastMsgError': True})
    # CLASS R — reaped tombstone mislabeled aborted (the tail) + task_results row.
    _seed(db, conv_r, [
        {'role': 'user', 'content': 'q', '_msgId': 'r-u'},
        {'role': 'assistant', 'content': 'partial', '_msgId': 'r-a',
         'finishReason': 'aborted', '_taskId': tid_r,
         'error': _env()},
    ], settings={'lastFinishReason': 'aborted', 'lastMsgError': True})
    from lib.database._core_schema import TASK_RESULTS, upsert
    now_ms = int(time.time() * 1000)
    upsert(db, TASK_RESULTS, {
        'task_id': tid_r, 'conv_id': conv_r, 'content': 'partial', 'thinking': '',
        'error': _json.dumps(_env(), ensure_ascii=False), 'status': 'done',
        'metadata': _json.dumps({'finishReason': 'aborted', 'taskId': tid_r}),
        'created_at': now_ms, 'completed_at': now_ms,
    }, insert_cols=['task_id', 'conv_id', 'content', 'thinking', 'error',
                    'status', 'metadata', 'created_at', 'completed_at'], retry=True)
    # CLASS OK — correct tombstone; CLASS UNKNOWN — no finishReason.
    _seed(db, conv_ok, [
        {'role': 'user', 'content': 'q', '_msgId': 'ok-u'},
        {'role': 'assistant', 'content': '', '_msgId': 'ok-a',
         'finishReason': 'error', '_taskId': 't-mig-ok', 'error': _env()},
    ])
    _seed(db, conv_u, [
        {'role': 'user', 'content': 'q', '_msgId': 'u-u'},
        {'role': 'assistant', 'content': '', '_msgId': 'u-a',
         '_taskId': 't-mig-u', 'error': _env()},
    ])

    convs = [conv_p, conv_r, conv_ok, conv_u]
    try:
        # ── DRY-RUN first: plan complete, nothing written ──
        plan, unknown = mig.run(db, apply=False)
        assert {(c['conv_id'], c['cls']) for c in plan} == {
            (conv_p, 'P'), (conv_r, 'R')}
        assert [c['conv_id'] for c in unknown] == [conv_u]
        # the dry-run PRINT path consumes 'after' — pin its shape (a precedence
        # bug here crashed main() on the real library while run() stayed green):
        for c in plan:
            assert isinstance(c['after'], dict), f'{c["cls"]} after must be a dict'
            assert 'finishReason' in c['after'] and 'error' in c['after']
        assert _read_msgs(db, conv_p)[1].get('error'), 'dry-run must not write'
        assert _read_msgs(db, conv_r)[1].get('finishReason') == 'aborted'

        # ── APPLY ──
        plan, unknown = mig.run(db, apply=True, backup_path=None)

        # P: error gone from messages + mirror; finishReason kept; sidecar fixed.
        mp = _read_msgs(db, conv_p)[1]
        assert 'error' not in mp, f'P: foreign error must be removed, got {mp.get("error")}'
        assert mp.get('finishReason') == 'stop'
        assert mp.get('_taskId') == 't-mig-clean'
        assert 'error' not in (_read_mirror_meta(db, conv_p, 1) or {})
        sp = _read_settings(db, conv_p)
        assert sp.get('lastMsgError') is False, f'P tail sidecar must clear, got {sp}'
        assert sp.get('lastFinishReason') == 'stop'

        # R: finishReason restamped everywhere; error kept.
        mr = _read_msgs(db, conv_r)[1]
        assert mr.get('finishReason') == 'error'
        assert (mr.get('error') or {}).get('context') == 'stuck-task-reaper'
        assert (_read_mirror_meta(db, conv_r, 1) or {}).get('finishReason') == 'error'
        sr = _read_settings(db, conv_r)
        assert sr.get('lastFinishReason') == 'error'
        assert sr.get('lastMsgError') is True
        tr = db.execute('SELECT metadata FROM task_results WHERE task_id=?',
                        (tid_r,)).fetchone()
        assert _json.loads(tr[0]).get('finishReason') == 'error', (
            'the poll-fallback row must agree with the cleaned message')

        # OK + UNKNOWN: byte-untouched.
        mok = _read_msgs(db, conv_ok)[1]
        assert mok.get('finishReason') == 'error' and mok.get('error')
        mu = _read_msgs(db, conv_u)[1]
        assert mu.get('finishReason') is None and mu.get('error')

        # ── IDEMPOTENT: second apply finds nothing ──
        plan2, unknown2 = mig.run(db, apply=True, backup_path=None)
        assert plan2 == [], f'second run must be a no-op, got {plan2}'
        assert [c['conv_id'] for c in unknown2] == [conv_u]
    finally:
        _cleanup(db, convs, [tid_r])


def test_classify_boundaries():
    """Pure classifier: every finishReason maps to exactly one action."""
    err = {'context': 'stuck-task-reaper'}
    assert mig.classify_message({'finishReason': 'stop', 'error': err})[0] == 'P'
    assert mig.classify_message({'finishReason': 'aborted', 'error': err})[0] == 'R'
    assert mig.classify_message({'finishReason': 'error', 'error': err})[0] == 'OK'
    assert mig.classify_message({'error': err})[0] == 'UNKNOWN'
    assert mig.classify_message({'finishReason': 'stop'})[0] is None
    assert mig.classify_message({'finishReason': 'stop',
                                 'error': {'context': 'other'}})[0] is None
